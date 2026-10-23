# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Stage 3 E2E — 모델 해소(model resolution) 회귀 가드.

커버하는 결함:
  1. ``/model/{id}/*`` 패스스루가 미등록/INACTIVE 모델에서 LookupError 를
     잡지 않아 글로벌 핸들러의 500 으로 나갔다 — /v1/messages 와 같은 404 계약.
  2. 상류(provider) 429 가 폴백 대상이 아니었다 — Bedrock ThrottlingException 은
     모델별 쿼터 부족 신호라 체인 후보가 서빙할 수 있다(CB 실패 카운트는 제외).
  3. 가용성 폴백 체인이 모든 스코프의 downgrade_policies 를 전역으로 병합한
     부팅 스냅샷이었다 — 팀 A 의 절감 규칙이 팀 B 의 failover 를 바꾸고,
     재시작 전 신규 규칙이 반영되지 않았다. 요청자 팀 규칙을 우선층으로 둔다.
  4. ``make_same_provider`` 의 deny-by-default 가 부팅 이후 등록된 alias 를
     영구 배제했다 — 후보 해소 계층(provider 화이트리스트)이 authoritative.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.routers.bedrock import _handle_bedrock
from app.services.fallback_loop import _CB_FAILURE_STATUSES, _FALLBACK_STATUSES
from app.services.fallback_resolver import FallbackResolver, make_same_provider
from app.services.router_service import ModelInactiveError
from app.schemas.domain import ProviderType


class _FakeRequest:
    """``_handle_bedrock`` 가 읽는 최소 표면 — scope["state"], app.state, body()."""

    def __init__(self, state: dict | None = None) -> None:
        self.scope = {"state": state or {}}
        self.app = MagicMock()
        self.app.state = MagicMock()

    async def body(self) -> bytes:
        return b"{}"


# ---------------------------------------------------------------------------
# 1. /model/* unknown model → 404 (was 500 via the global exception handler)
# ---------------------------------------------------------------------------

class TestBedrockPassthroughModelResolution404:
    @pytest.mark.asyncio
    async def test_unknown_model_returns_404_not_500(self, monkeypatch):
        monkeypatch.setattr(
            "app.routers.bedrock._router_service.resolve_bedrock_model",
            AsyncMock(side_effect=LookupError("Model alias 'bogus' not found")),
        )
        resp = await _handle_bedrock(
            _FakeRequest({"_session_factory": None, "_redis": None}),
            "bogus-model",
            "invoke",
            stream=False,
        )
        assert resp.status_code == 404
        assert "not_found" in resp.body.decode()

    @pytest.mark.asyncio
    async def test_inactive_model_returns_404_not_500(self, monkeypatch):
        # INACTIVE 는 운영자 kill switch — 조용한 치환도, 500 도 아니라 거절.
        monkeypatch.setattr(
            "app.routers.bedrock._router_service.resolve_bedrock_model",
            AsyncMock(side_effect=ModelInactiveError("Model 'x' is inactive")),
        )
        resp = await _handle_bedrock(
            _FakeRequest({"_session_factory": None, "_redis": None}),
            "x",
            "converse",
            stream=False,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_wrong_provider_alias_returns_404(self, monkeypatch):
        # /v1/responses 전용 alias 를 /model/* 에 넣으면 provider 화이트리스트
        # LookupError — 역시 500 이 아니라 404 다.
        monkeypatch.setattr(
            "app.routers.bedrock._router_service.resolve_bedrock_model",
            AsyncMock(side_effect=LookupError("Model alias 'codex-gpt' not found")),
        )
        resp = await _handle_bedrock(
            _FakeRequest({"_session_factory": None, "_redis": None}),
            "codex-gpt",
            "invoke",
            stream=False,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 1.5 /model/* passthrough honors model-level account routing (0049)
# ---------------------------------------------------------------------------

class TestBedrockPassthroughModelRouting:
    """회귀 방지: 패스스루도 ARN 명시 모델은 그 계정으로 — in-account 무음 폴백 금지."""

    def _routed_config(self):
        cfg = MagicMock()
        cfg.alias = "routed-model"
        cfg.provider_model_id = "us.anthropic.claude-sonnet-4-6"
        cfg.provider = ProviderType.BEDROCK
        cfg.account_role_arn = "arn:aws:iam::610156625835:role/gw-test"
        cfg.region = "ap-south-1"
        cfg.external_id = None
        return cfg

    @pytest.mark.asyncio
    async def test_model_route_without_provider_fails_closed_503(self, monkeypatch):
        """명시 모델 라우팅 + 브로커 부재 → 503 (in-account 무음 호출 금지)."""
        monkeypatch.setattr(
            "app.routers.bedrock._router_service.resolve_bedrock_model",
            AsyncMock(return_value=self._routed_config()),
        )
        req = _FakeRequest({"_session_factory": None, "_redis": None})
        req.app.state.bedrock_account_client_provider = None
        resp = await _handle_bedrock(req, "routed-model", "invoke", stream=False)
        assert resp.status_code == 503
        assert "service_unavailable" in resp.body.decode()

    @pytest.mark.asyncio
    async def test_model_route_uses_cross_account_adapter_no_fallback(self, monkeypatch):
        """모델 ARN → 크로스계정 어댑터 + fail-closed(fallback_client=None)."""
        monkeypatch.setattr(
            "app.routers.bedrock._router_service.resolve_bedrock_model",
            AsyncMock(return_value=self._routed_config()),
        )
        captured = {}

        class _FakeAdapter:
            def __init__(self, **kw):
                captured.update(kw)

            async def invoke(self, body, model_id, **kw):
                captured["model_id"] = model_id
                return 200, b"{}", {}, None

        provider = MagicMock()
        provider.get_client.return_value = MagicMock(name="xacct-client")
        req = _FakeRequest({"_session_factory": None, "_redis": None})
        req.app.state.bedrock_account_client_provider = provider
        monkeypatch.setattr("app.routers.bedrock.BedrockAdapter", _FakeAdapter)
        # 본문 로거는 이 테스트의 대상이 아니다 — None 으로 꺼둔다.
        monkeypatch.setattr(
            "app.routers.bedrock.resolve_body_logger", AsyncMock(return_value=None)
        )

        resp = await _handle_bedrock(req, "routed-model", "invoke", stream=False)
        # 크로스계정 어댑터가 만들어졌고 in-account 폴백을 붙이지 않았다.
        assert captured.get("client_resolver") is not None
        assert captured.get("fallback_client") is None
        # 대상 리전으로 CRIS 접두가 재작성됐다(us. → apac.).
        assert captured.get("model_id", "").startswith("apac.")
        # provider resolver가 대상 ARN/리전으로 호출된다.
        captured["client_resolver"]()
        provider.get_client.assert_called_once_with(
            "arn:aws:iam::610156625835:role/gw-test", "ap-south-1", None
        )


# ---------------------------------------------------------------------------
# 2. Upstream 429 is fallback-eligible but NOT a CB failure
# ---------------------------------------------------------------------------

class TestFallbackStatusSets:
    def test_429_is_fallback_eligible(self):
        assert 429 in _FALLBACK_STATUSES
        assert 502 in _FALLBACK_STATUSES
        assert 503 in _FALLBACK_STATUSES
        assert 504 in _FALLBACK_STATUSES

    def test_429_does_not_penalise_circuit(self):
        # ThrottlingException = 일시적 쿼터 — 회로를 열 이유가 아니다.
        assert 429 not in _CB_FAILURE_STATUSES
        assert 504 not in _CB_FAILURE_STATUSES
        assert _CB_FAILURE_STATUSES == frozenset({502, 503})


# ---------------------------------------------------------------------------
# 3. Team-scoped fallback chain wiring
# ---------------------------------------------------------------------------

class TestTeamScopedFallbackChain:
    def test_messages_loads_requester_team_rules(self):
        """messages() 는 예산 다운그레이드와 같은 로더로 요청자 팀 규칙을 읽는다 —
        그래야 정책 편집이 60s 캐시로 반영되고 다른 팀 규칙에 오염되지 않는다."""
        import inspect

        import app.routers.messages as m

        src = inspect.getsource(m.messages)
        assert "get_active_rules" in src
        assert "_team_chain" in src
        # 팀 층이 전역(스타트업) 층을 덮어쓴다.
        assert "fallback_resolver.chain" in src

    def test_team_chain_overrides_global_for_same_from_alias(self):
        global_chain = {"opus": "haiku", "sonnet": "haiku"}
        team_chain = {"opus": "sonnet"}  # 이 팀은 opus→sonnet 만 선언
        merged = {**global_chain, **team_chain}
        order = FallbackResolver(chain=merged).resolve(
            original="opus", allowed=None, same_provider=lambda a: True
        )
        assert order == ["opus", "sonnet", "haiku"]

    def test_team_without_rules_gets_global_layer(self):
        global_chain = {"opus": "sonnet"}
        merged = {**global_chain, **{}}  # 팀 규칙 없음 → 전역 층 그대로
        order = FallbackResolver(chain=merged).resolve(
            original="opus", allowed=None, same_provider=lambda a: True
        )
        assert order == ["opus", "sonnet"]

    def test_chain_origin_uses_resolved_alias(self):
        """클라이언트가 '[1m]' 변형이나 pmid 를 보내도 체인 조회는 해석된
        표준 alias 로 한다 — messages() 의 ``original`` 인자 검증."""
        import inspect

        import app.routers.messages as m

        src = inspect.getsource(m.messages)
        assert "model_config.alias or model_alias" in src
        assert "original=_chain_origin" in src

    def test_prof_bound_before_provider_branch(self):
        """``_rewrite(pmid, cfg=후보)`` 가 Mantle 분기에서도 ``_prof`` 를 읽는다 —
        else(Bedrock) 안에만 두면 Mantle 폴백 후보 순회에서 NameError 가 난다.
        분기 전 명시 초기화 계약."""
        import inspect

        import app.routers.messages as m

        src = inspect.getsource(m.messages)
        assert src.index("_prof = None") < src.index(
            "if decision.provider == ProviderType.BEDROCK_MANTLE"
        )


# ---------------------------------------------------------------------------
# 4. same_provider — admit unknown, deny known-different
# ---------------------------------------------------------------------------

class TestSameProviderPredicate:
    def test_post_boot_alias_admitted(self):
        pred = make_same_provider({"opus": ProviderType.BEDROCK}, ProviderType.BEDROCK)
        assert pred("brand-new-alias") is True

    def test_known_cross_provider_denied(self):
        pred = make_same_provider(
            {"openmodel-x": ProviderType.OPENMODEL}, ProviderType.BEDROCK
        )
        assert pred("openmodel-x") is False
