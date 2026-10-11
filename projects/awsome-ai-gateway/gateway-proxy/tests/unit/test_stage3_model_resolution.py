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
