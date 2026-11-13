# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""FR-4.6 enforcement 레이어의 CPM/CPH pre-reserve 통합 유닛 테스트."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    AuthType,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
)
from app.services.rate_limit_enforcement import _estimate_cost, enforce_rate_limits


def _model(input_per_1k: str = "0.003", output_per_1k: str = "0.015") -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id="anthropic.claude-sonnet-4-6",
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="",
        pricing=ModelPricingSchema(
            input_per_1k=Decimal(input_per_1k),
            output_per_1k=Decimal(output_per_1k),
        ),
        status=ModelStatus.ACTIVE,
    )


def test_estimate_cost_uses_input_and_max_output():
    cost = _estimate_cost(_model(), estimated_input=2000, max_output=1000)
    # input: 2000/1000 * 0.003 = 0.006
    # output: 1000/1000 * 0.015 = 0.015
    assert cost == Decimal("0.021000")


@pytest.mark.asyncio
async def test_enforce_cpm_exceeded_returns_429(monkeypatch, mock_redis):
    """CPM 초과 시 429 JSONResponse (RPM/TPM은 통과 가정)."""
    from app.schemas.domain import RateLimitResult

    # RPM 통과 mock — committed_keys 를 실제 값으로 넣어야 뒤 스코프 거절 시
    # undo_rpm 에 그 키가 전달되는지 검증할 수 있다(기본 [] 는 early-return 이라
    # 되돌리기 계약 회귀를 못 잡는다 — SWE-2 7차 #6).
    rpm_committed = ["rl:rpm:u:u-1:{k1}:m:1"]
    async def _rpm_ok(*args, **kwargs):
        return RateLimitResult(
            allowed=True, remaining=-1, limit=-1, committed_keys=rpm_committed
        )

    async def _tpm_ok(*args, **kwargs):
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    # CPM 거부 mock
    async def _cost_fail(*args, **kwargs):
        from app.schemas.domain import CostLimitResult

        return CostLimitResult(
            allowed=False,
            scope="USER",
            limit_type="cpm",
            limit=Decimal("0.5"),
            remaining=Decimal("0"),
            retry_after=60,
            reserved_cost=Decimal("0"),
        )

    from app.services import rate_limit_enforcement as enf

    undo_calls: list[list[str]] = []

    async def _undo_spy(_self, _redis, _descriptors, _req_id, *, committed_keys=None):
        undo_calls.append(list(committed_keys or []))

    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_rpm", _rpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_tpm", _tpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "reserve_cost", _cost_fail)
    monkeypatch.setattr(enf.RateLimitService, "undo_rpm", _undo_spy)

    # config loader — USER CPM=0.5 설정
    async def _load_limits(**_kwargs):
        from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits

        return AllScopeLimits(
            user=ScopeLimits(rpm=1000, tpm=1_000_000, cpm=Decimal("0.5"), cph=None),
            team=ScopeLimits(),
            global_=ScopeLimits(),
        )

    monkeypatch.setattr(enf, "load_all_scope_limits", _load_limits)

    auth = AuthContext(
        user_id="u-1",
        user_name="u",
        team_id="t-1",
        dept_id="d-1",
        roles=["USER"],
        auth_type=AuthType.VIRTUAL_KEY,
    )
    state: dict = {}

    resp = await enforce_rate_limits(
        redis=mock_redis,
        auth_context=auth,
        model_config=_model(),
        body={"max_tokens": 1000},
        state=state,
        request_id="req-1",
    )

    assert resp is not None
    assert resp.status_code == 429
    body = json.loads(bytes(resp.body).decode())
    assert body["error"]["code"] == "user_cpm_exceeded"
    assert body["error"]["scope"] == "USER"
    assert body["error"]["limit_type"] == "cpm"
    assert resp.headers["Retry-After"] == "60"
    # state에 cost_reserved 주입되지 않음 (거부됨)
    assert "rate_limit_state" not in state
    # 되돌리기 계약 — CPM 거절 시 RPM 은 이번 호출이 커밋한 키만 되돌린다.
    assert undo_calls == [rpm_committed]


@pytest.mark.asyncio
async def test_enforce_cpm_pass_injects_cost_reserved(monkeypatch, mock_redis):
    """CPM 통과 시 state['rate_limit_state']['cost_reserved']에 예약액 주입."""
    from app.schemas.domain import CostLimitResult, RateLimitResult

    async def _rpm_ok(*args, **kwargs):
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def _tpm_ok(*args, **kwargs):
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def _cost_ok(*args, **kwargs):
        return CostLimitResult(allowed=True, reserved_cost=Decimal("0.021"))

    from app.services import rate_limit_enforcement as enf

    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_rpm", _rpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_tpm", _tpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "reserve_cost", _cost_ok)

    async def _load_limits(**_kwargs):
        from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits

        return AllScopeLimits(
            user=ScopeLimits(cpm=Decimal("1.0")),
            team=ScopeLimits(),
            global_=ScopeLimits(),
        )

    monkeypatch.setattr(enf, "load_all_scope_limits", _load_limits)

    auth = AuthContext(
        user_id="u-1",
        user_name="u",
        team_id="t-1",
        dept_id="d-1",
        roles=["USER"],
        auth_type=AuthType.VIRTUAL_KEY,
    )
    state: dict = {}
    resp = await enforce_rate_limits(
        redis=mock_redis,
        auth_context=auth,
        model_config=_model(),
        body={"max_tokens": 500},
        state=state,
        request_id="req-2",
    )

    assert resp is None
    assert state["rate_limit_state"]["cost_reserved"] == Decimal("0.021")


@pytest.mark.asyncio
async def test_enforce_cost_eval_exception_spawns_unwind(monkeypatch, mock_redis):
    """reserve_cost 가 예외를 던지면(RPM+TPM 커밋 후) catch-all 언와인드가
    커밋분을 되돌린다 — 9차 리뷰(try 밖 탈출 시 창 TTL 까지 고아 예약).

    언와인드는 추적 태스크로 spawn 되므로 drain 후 호출을 단정한다.
    """
    from app.schemas.domain import RateLimitResult
    from app.services.budget_service import _BACKGROUND_TASKS

    rpm_committed = ["rl:rpm:u:u-1:{k1}:m:1"]

    async def _rpm_ok(*args, **kwargs):
        # 실서비스 계약 — 결과의 committed_keys 와 committed_out 양쪽을 채운다
        # (committed_out 은 예외 경로의 부분 커밋 노출용, 10차 리뷰).
        if kwargs.get("committed_out") is not None:
            kwargs["committed_out"].extend(rpm_committed)
        return RateLimitResult(
            allowed=True, remaining=-1, limit=-1, committed_keys=rpm_committed
        )

    async def _tpm_ok(*args, **kwargs):
        # 실제 계약 — 통과 스코프는 committed_out 에 append 돼 호출자가
        # 언와인드 대상을 안다. 비워 두면 되돌릴 것이 없어 검증이 무의미해진다.
        descriptors = args[2] if len(args) > 2 else kwargs.get("descriptors") or []
        if kwargs.get("committed_out") is not None and descriptors:
            kwargs["committed_out"].append(descriptors[0])
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def _cost_boom(*args, **kwargs):
        raise RuntimeError("loop-external failure")

    settle_calls: list = []
    undo_calls: list = []

    async def _settle_spy(_self, _redis, descriptors, reserved, actual, *, reserved_at=None):
        settle_calls.append((list(descriptors), reserved, actual))

    async def _undo_spy(_self, _redis, _descriptors, _req_id, *, committed_keys=None):
        undo_calls.append(list(committed_keys or []))

    from app.services import rate_limit_enforcement as enf

    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_rpm", _rpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_tpm", _tpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "reserve_cost", _cost_boom)
    monkeypatch.setattr(enf.RateLimitService, "settle_tpm", _settle_spy)
    monkeypatch.setattr(enf.RateLimitService, "undo_rpm", _undo_spy)

    async def _load_limits(**_kwargs):
        from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits

        return AllScopeLimits(
            user=ScopeLimits(rpm=1000, tpm=1_000_000, cpm=Decimal("1.0")),
            team=ScopeLimits(),
            global_=ScopeLimits(),
        )

    monkeypatch.setattr(enf, "load_all_scope_limits", _load_limits)

    auth = AuthContext(
        user_id="u-1",
        user_name="u",
        team_id="t-1",
        dept_id="d-1",
        roles=["USER"],
        auth_type=AuthType.VIRTUAL_KEY,
    )
    state: dict = {}

    with pytest.raises(RuntimeError, match="loop-external"):
        await enforce_rate_limits(
            redis=mock_redis,
            auth_context=auth,
            model_config=_model(),
            body={"max_tokens": 1000},
            state=state,
            request_id="req-3",
        )

    assert "rate_limit_state" not in state
    # spawn 된 언와인드 완료 대기
    pending = [t for t in _BACKGROUND_TASKS if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    # TPM 커밋분(reserved→0)과 RPM 슬롯이 되돌려졌어야 한다.
    assert len(settle_calls) == 1 and settle_calls[0][2] == 0
    assert undo_calls == [rpm_committed]


@pytest.mark.asyncio
async def test_rpm_committed_out_exposes_partial_commit(monkeypatch, mock_redis):
    """check_multi_scope_rpm 이 eval 도중 취소돼도 committed_out 에는
    그 시점까지 커밋된 키가 남아 catch-all 이 되돌릴 수 있다(10차 리뷰 —
    예전엔 try 밖이라 부분 커밋이 고아였다).
    """
    from app.services.rate_limit_service import RateLimitService

    committed: list[str] = []
    svc = RateLimitService()
    # descriptors 는 enforce 내부에서 만드는 ScopeDescriptor — 서비스 단위로
    # committed_out 계약만 검증한다(실패 주입은 _guarded_eval 몽키패치로).
    from app.services.rate_limit_scope import ScopeDescriptor, RateLimitScope

    desc = [
        ScopeDescriptor(scope=RateLimitScope.USER, scope_id="u-1",
                        model_alias="m", rpm_limit=10, tpm_limit=None),
        ScopeDescriptor(scope=RateLimitScope.TEAM, scope_id="t-1",
                        model_alias="m", rpm_limit=10, tpm_limit=None),
    ]

    calls = 0

    async def _boom_eval(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            # eval 도중 취소 — except Exception(fail-mode 정책)은 통과 못 하고
            # 전파되는 BaseException 경로가 committed_out 노출이 필요한 실제
            # 시나리오다(10차 리뷰).
            raise asyncio.CancelledError()
        # eval 성공 형태 — added=[1] 로 이번 호출이 새로 커밋한 것으로 표시.
        return json.dumps({"allowed": True, "remaining": 9, "limit": 10,
                           "added": [1]})

    import app.services.rate_limit_service as svc_mod
    monkeypatch.setattr(svc_mod, "_guarded_eval", _boom_eval)
    # 스크립트 로더는 이 단위 테스트에 미로드 — eval 자체는 위 mock 이 흉내낸다.
    monkeypatch.setattr(
        svc_mod.LuaScriptLoader, "get", staticmethod(lambda _name: "fake-script")
    )

    with pytest.raises(asyncio.CancelledError):
        await svc.check_multi_scope_rpm(
            mock_redis, desc, request_id="req-x", committed_out=committed
        )
    # 첫 스코프는 eval 성공(ZADD) 후 두 번째에서 죽었다 — 커밋분이 노출돼야 한다.
    assert len(committed) == 1
    assert committed[0] == "{USER:u-1:m}:rpm"


@pytest.mark.asyncio
async def test_cost_committed_out_exposes_partial_commit(monkeypatch, mock_redis):
    """reserve_cost 도 같은 계약 — eval 도중 취소 시 부분 커밋 튜플 노출."""
    from app.services.rate_limit_service import RateLimitService
    import app.services.rate_limit_service as svc_mod

    committed: list = []
    svc = RateLimitService()

    calls = 0

    async def _boom_eval(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            # eval 도중 취소 — except Exception(fail-mode 정책)은 통과 못 하고
            # 전파되는 BaseException 경로가 committed_out 노출이 필요한 실제
            # 시나리오다(10차 리뷰).
            raise asyncio.CancelledError()
        return json.dumps({"allowed": True})

    monkeypatch.setattr(svc_mod, "_guarded_eval", _boom_eval)
    monkeypatch.setattr(
        svc_mod.LuaScriptLoader, "get", staticmethod(lambda _name: "fake-script")
    )

    with pytest.raises(asyncio.CancelledError):
        await svc.reserve_cost(
            mock_redis,
            user_id="u-1",
            estimated_cost=Decimal("0.5"),
            user_cpm_limit=Decimal("1.0"),
            user_cph_limit=None,
            team_id="t-1",
            team_cpm_limit=Decimal("1.0"),
            team_cph_limit=None,
            committed_out=committed,
        )
    assert len(committed) == 1
    assert committed[0][0] == "USER"
