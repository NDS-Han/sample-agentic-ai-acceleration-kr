# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R23 r1 회귀 — 예약 정산 소유권과 마커 만료 복구.

S5a ``release_reservations`` 가 ``state.pop`` 으로 정산 소유권을 원자적으로 주장한다 —
    예전 ``get`` … 환불 … ``pop`` 순서는 늦은 drain ``finalize`` 와 같은 상태를
    공유할 수 있어 TPM/CPM 이중 환불·월예산 실비 유실이 가능했다.

S5b ``budget_settle.lua`` — 마커가 TTL 만료로 사라진 뒤 정산이 오면 ``:done``
    툼스톤이 없고 호출자가 예약치를 알면 예약치만큼 델타를 복구한다.
    복구 없으면 actual > reserved 인 요청이 그 달 내내 과소 계상된다.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 실 Lua 스크립트를 파일에서 직접 읽어 fakeredis(lupa) 에서 실행한다 —
# LuaScriptLoader 를 거치면 다른 테스트의 "-- mock" 주입에 오염된다.
_SCRIPTS_DIR = Path(__file__).parents[2] / "src" / "app" / "redis_scripts"
_SETTLE_LUA = (_SCRIPTS_DIR / "budget_settle.lua").read_text(encoding="utf-8")
_RECONCILE_LUA = (_SCRIPTS_DIR / "budget_reconcile.lua").read_text(
    encoding="utf-8"
)


def _resv_spec(scope="user", uid="u1", period="2026-10", req="req-1",
               client=None, reserved="10.00"):
    """budget_service.reserve_budget 이 만드는 스펙과 같은 키 모양."""
    if client:
        return {
            "scope": "client",
            "usage_key": f"budget:user:{{{uid}}}:{client}:{period}",
            "config_key": f"budget:config:user:{{{uid}}}:{client}",
            "marker_key": f"budget:pending:client:{uid}:{client}:{req}",
            "resvsum_key": f"budget:resvsum:user:{{{uid}}}:{client}:{period}",
            "idx_key": f"budget:pendingidx:client:{uid}",
            "idx_field": f"{client}:{req}",
            "fallback": "",
            "reserved": reserved,
        }
    return {
        "scope": scope,
        "usage_key": f"budget:{scope}:{{{uid if scope == 'user' else 't1'}}}:{period}",
        "config_key": f"budget:config:{scope}:{{{uid if scope == 'user' else 't1'}}}",
        "marker_key": f"budget:pending:{scope}:{{{uid if scope == 'user' else 't1'}}}:{req}",
        "resvsum_key": f"budget:resvsum:{scope}:{{{uid if scope == 'user' else 't1'}}}:{period}",
        "idx_key": f"budget:pendingidx:{scope}:{{{uid if scope == 'user' else 't1'}}}",
        "idx_field": req,
        "fallback": "",
        "reserved": reserved,
    }


@pytest.mark.asyncio
async def test_release_reservations_pops_state_before_settling():
    """pop-first — finalize 와 경쟁해도 둘 중 하나만 정산한다."""
    from app.services.fallback_loop import release_reservations

    auth = MagicMock()
    auth.user_id = "u1"
    auth.team_id = "t1"
    state = {
        "rate_limit_state": {
            "tpm_descriptors": [{"key": "k"}],
            "tpm_reserved": 100,
            "budget_reservation": [_resv_spec()],
        }
    }
    redis = MagicMock()

    with patch(
        "app.services.rate_limit_service.RateLimitService.settle_tpm",
        new=AsyncMock(),
    ) as settle_tpm, patch(
        "app.services.budget_service.BudgetService.settle_budget",
        new=AsyncMock(),
    ) as settle_budget:
        await release_reservations(redis=redis, state=state, auth_context=auth)
        # 두 번째 호출은 상태가 이미 pop 됐으므로 no-op — 이중 환불이 없다.
        await release_reservations(redis=redis, state=state, auth_context=auth)

    assert state.get("rate_limit_state") is None
    assert settle_tpm.await_count == 1
    assert settle_budget.await_count == 1


@pytest.mark.asyncio
async def test_settle_recovers_when_marker_expired(fake_redis):
    """마커 만료(툼스톤 없음) + 호출자 예약치 제공 → 예약치만큼 델타 복구.

    예약 10, 실비 15 → 카운터는 15 가 돼야 한다. 복구 없으면 예약치 10 이
    남아 그 달 내내 $5 과소 계상이었다(R23 r1 S5). 복구 근거는 인덱스 필드의
    생존 — reconcile/rebuild 가 걷어낸 예약은 필드가 지워져 발동하지 않는다.
    """
    spec = _resv_spec(uid="u-rec", req="req-expired", reserved="10.00")
    # 예약이 실제로 실행됐다가 마커만 TTL 만료된 상태 — usage/resvsum 은
    # 예약치 10 을 들고 있고 인덱스 필드는 아직 청소되지 않았다.
    await fake_redis.set(spec["usage_key"], "10")
    await fake_redis.set(spec["resvsum_key"], "10")
    await fake_redis.hset(spec["idx_key"], spec["idx_field"], "req-expired")

    raw = await fake_redis.eval(
        _SETTLE_LUA, 5,
        spec["usage_key"], spec["config_key"], spec["marker_key"],
        spec["resvsum_key"], spec["idx_key"],
        "15.00", "", spec["idx_field"], spec["reserved"],
    )
    result = json.loads(raw)
    assert result["settled"] is True
    assert result["recovered"] is True
    assert float(await fake_redis.get(spec["usage_key"])) == pytest.approx(15.0)
    assert float(await fake_redis.get(spec["resvsum_key"])) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_settle_after_reconcile_sweep_applies_actual_only(fake_redis):
    """reconcile 이 고아 예약을 걷어낸 뒤 늦은 settle — 실비만 적용(R23 r2 S-3).

    실 Lua 재현: reserve 5(used 55) → 마커 소실 → reconcile(orphan=5,
    used 50, resvsum 0, 인덱스 청소) → settle(actual 3) → 기대 53.
    복구가 무조건 예약치를 빼면 used=48 resvsum=-5 로 이중 차감이었다.
    """
    spec = _resv_spec(uid="u-swept", req="req-swept", reserved="5.00")
    await fake_redis.set(spec["usage_key"], "50")
    await fake_redis.set(spec["resvsum_key"], "0")
    # 인덱스 필드도 이미 청소된 상태 — HEXISTS=0

    raw = await fake_redis.eval(
        _SETTLE_LUA, 5,
        spec["usage_key"], spec["config_key"], spec["marker_key"],
        spec["resvsum_key"], spec["idx_key"],
        "3.00", "", spec["idx_field"], spec["reserved"],
    )
    result = json.loads(raw)
    assert result["settled"] is True
    assert float(await fake_redis.get(spec["usage_key"])) == pytest.approx(53.0)
    assert float(await fake_redis.get(spec["resvsum_key"])) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_settle_refund_after_failed_reserve_is_noop(fake_redis):
    """reserve 가 서버에서 실행되지 않은 환불 settle(0) — 차감 없음(R23 r2 S-2).

    마커도 인덱스 필드도 없는데 호출자 예약치를 그대로 빼면 받지도 않은
    예약이 차감된다. 다른 진행 중 예약이 resvsum 을 채워도 인덱스 필드가
    없으면 생존 증거가 없다.
    """
    spec = _resv_spec(uid="u-never", req="req-never", reserved="5.00")
    await fake_redis.set(spec["usage_key"], "50")
    await fake_redis.set(spec["resvsum_key"], "20")  # 다른 진행 중 예약

    raw = await fake_redis.eval(
        _SETTLE_LUA, 5,
        spec["usage_key"], spec["config_key"], spec["marker_key"],
        spec["resvsum_key"], spec["idx_key"],
        "0", "", spec["idx_field"], spec["reserved"],
    )
    result = json.loads(raw)
    assert result["settled"] is True
    assert float(await fake_redis.get(spec["usage_key"])) == pytest.approx(50.0)
    assert float(await fake_redis.get(spec["resvsum_key"])) == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_settle_noop_after_done_tombstone(fake_redis):
    """정상 정산 뒤 재정산은 툼스톤을 보고 no-op — 이중 적용이 없다."""

    spec = _resv_spec(uid="u-done", req="req-done", reserved="10.00")
    await fake_redis.set(spec["marker_key"], "10")
    await fake_redis.set(spec["usage_key"], "10")
    await fake_redis.set(spec["resvsum_key"], "10")

    script = _SETTLE_LUA
    args = (
        spec["usage_key"], spec["config_key"], spec["marker_key"],
        spec["resvsum_key"], spec["idx_key"],
    )
    first = json.loads(
        await fake_redis.eval(script, 5, *args, "15.00", "", spec["idx_field"], "10")
    )
    assert first["settled"] is True
    assert float(await fake_redis.get(spec["usage_key"])) == pytest.approx(15.0)

    second = json.loads(
        await fake_redis.eval(script, 5, *args, "15.00", "", spec["idx_field"], "10")
    )
    assert second["settled"] is False  # 툼스톤 — 재적용 안 함
    assert float(await fake_redis.get(spec["usage_key"])) == pytest.approx(15.0)


@pytest.mark.asyncio
async def test_reconcile_hdel_dead_idx_fields_so_settle_is_exact(fake_redis):
    """시나리오 E(R23 r3 S-1) — 동시 예약 scope 에서 dead 인덱스 필드가
    남으면 settle 복구가 다른 요청의 라이브 resvsum 을 함께 차감한다.

    reserve A=5 + B2=7 (usage 62, resvsum 12) → A 마커만 소실 →
    reconcile: dead 필드 HDEL + resvsum=7 + orphan 되돌림(usage 57) →
    settle A(3): 필드 없음 → 실비만(60) → settle B2(7): 60/0.
    인덱스를 안 지우면 settle A 가 min(5, resvsum 7)=5 를 차감해
    최종 55/-5 로 과소 계상됐다.
    """
    spec_a = _resv_spec(uid="u-conc", req="req-A", reserved="5.00")
    spec_b = _resv_spec(uid="u-conc", req="req-B2", reserved="7.00")
    usage_key, resvsum_key = spec_a["usage_key"], spec_a["resvsum_key"]
    idx_key = spec_a["idx_key"]

    await fake_redis.set(usage_key, "62")
    await fake_redis.set(resvsum_key, "12")
    await fake_redis.hset(idx_key, spec_a["idx_field"], "5")
    await fake_redis.hset(idx_key, spec_b["idx_field"], "7")
    await fake_redis.set(spec_b["marker_key"], "7")  # B2 마커만 생존

    # reconcile — usage/resvsum/idx + 라이브 마커 스냅샷 + db/snap/prefix
    await fake_redis.eval(
        _RECONCILE_LUA, 4,
        usage_key, resvsum_key, idx_key, spec_b["marker_key"],
        "50", "12", "budget:pending:user:{u-conc}:",
    )
    assert await fake_redis.hexists(idx_key, spec_a["idx_field"]) in (0, False)
    assert float(await fake_redis.get(resvsum_key)) == pytest.approx(7.0)
    assert float(await fake_redis.get(usage_key)) == pytest.approx(57.0)

    # settle A — dead 필드가 지워졌으므로 실비만 적용된다
    result_a = json.loads(
        await fake_redis.eval(
            _SETTLE_LUA, 5,
            spec_a["usage_key"], spec_a["config_key"], spec_a["marker_key"],
            spec_a["resvsum_key"], spec_a["idx_key"],
            "3.00", "", spec_a["idx_field"], spec_a["reserved"],
        )
    )
    assert result_a["settled"] is True
    assert float(await fake_redis.get(usage_key)) == pytest.approx(60.0)
    assert float(await fake_redis.get(resvsum_key)) == pytest.approx(7.0)

    # settle B2 — 라이브 마커 정산
    result_b = json.loads(
        await fake_redis.eval(
            _SETTLE_LUA, 5,
            spec_b["usage_key"], spec_b["config_key"], spec_b["marker_key"],
            spec_b["resvsum_key"], spec_b["idx_key"],
            "7.00", "", spec_b["idx_field"], spec_b["reserved"],
        )
    )
    assert result_b["settled"] is True
    assert float(await fake_redis.get(usage_key)) == pytest.approx(60.0)
    assert float(await fake_redis.get(resvsum_key)) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_settle_tombstone_lives_outside_pending_namespace(fake_redis):
    """툼스톤은 budget:settled: 에 둔다 — pending 스캔의 재합산 방지(R23 r4 M-1).

    ``budget:pending:*`` 를 스캔하는 경로(reconcile/웜업 pending 합산/
    restore 스크립트)가 값이 실비인 ``:done`` 툼스톤을 라이브 마커로
    오인해 직전 4h의 정산 실비를 usage/resvsum 에 재합산했다(실측
    95/5 → 135/45). 해시태그는 마커와 동일해 Cluster 슬롯도 같다.
    """
    spec = _resv_spec(uid="u-tomb", req="req-tomb", reserved="10.00")
    await fake_redis.set(spec["marker_key"], "10")
    await fake_redis.set(spec["usage_key"], "10")
    await fake_redis.set(spec["resvsum_key"], "10")

    result = json.loads(
        await fake_redis.eval(
            _SETTLE_LUA, 5,
            spec["usage_key"], spec["config_key"], spec["marker_key"],
            spec["resvsum_key"], spec["idx_key"],
            "15.00", "", spec["idx_field"], spec["reserved"],
        )
    )
    assert result["settled"] is True

    settled = f"budget:settled:user:{{u-tomb}}:req-tomb"
    assert await fake_redis.get(settled) == "15.00"
    assert await fake_redis.ttl(settled) > 0
    # 구형 위치(budget:pending:...:done)에는 키가 없어야 한다
    assert await fake_redis.get(spec["marker_key"] + ":done") is None
    # 재정산은 툼스톤을 보고 no-op
    second = json.loads(
        await fake_redis.eval(
            _SETTLE_LUA, 5,
            spec["usage_key"], spec["config_key"], spec["marker_key"],
            spec["resvsum_key"], spec["idx_key"],
            "15.00", "", spec["idx_field"], spec["reserved"],
        )
    )
    assert second["settled"] is False


@pytest.mark.asyncio
async def test_reconcile_live_includes_index_not_scanned_markers(fake_redis):
    """스캔에 없는 인덱스 마커도 live 에 합산 — SCAN~스냅샷 창의 예약 보호
    (R23 r4 S-1).

    전역 SCAN 후·행별 스냅샷 전에 들어온 예약은 스캔 목록에 없지만
    인덱스에는 있다 — live 를 eval 안에서 인덱스∪스캔 합집합으로 계산해
    라이브 예약이 고아로 차감되지 않는다(실측: 62 → 55 과소 계상).
    """
    spec_a = _resv_spec(uid="u-gap", req="req-A", reserved="5.00")
    spec_b = _resv_spec(uid="u-gap", req="req-B2", reserved="7.00")
    usage_key, resvsum_key = spec_a["usage_key"], spec_a["resvsum_key"]
    idx_key = spec_a["idx_key"]

    await fake_redis.set(usage_key, "62")
    await fake_redis.set(resvsum_key, "12")
    await fake_redis.hset(idx_key, spec_a["idx_field"], "5")
    await fake_redis.hset(idx_key, spec_b["idx_field"], "7")
    await fake_redis.set(spec_a["marker_key"], "5")  # 둘 다 라이브
    await fake_redis.set(spec_b["marker_key"], "7")

    # 스캔 스냅샷은 req-B2 만 봤다(req-A 는 SCAN~스냅샷 사이에 도착) —
    # 그래도 인덱스 경유로 live=12 가 계산돼 orphan=0, 차감 없음.
    await fake_redis.eval(
        _RECONCILE_LUA, 4,
        usage_key, resvsum_key, idx_key, spec_b["marker_key"],
        "50", "12", "budget:pending:user:{u-gap}:",
    )
    assert float(await fake_redis.get(usage_key)) == pytest.approx(62.0)
    assert float(await fake_redis.get(resvsum_key)) == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_reconcile_client_scope_sums_only_own_app_fields(fake_redis):
    """client scope reconcile 은 자기 앱 필드만 live 에 합산한다(R23 r5 M-1).

    인덱스는 유저당 하나인데 카운터는 앱별 — claude-code 행을 교정할 때
    codex 예약까지 합산하면 다른 앱의 예약이 이 카운터에 부풀어
    오탐 HARD_BLOCK 이 생긴다(실측: 15/5 → 24/14).
    """
    spec = _resv_spec(uid="u-apps", req="req-cc", client="claude-code",
                      reserved="5.00")
    usage_key, resvsum_key = spec["usage_key"], spec["resvsum_key"]
    idx_key = spec["idx_key"]

    # 인덱스에는 claude-code(5) + codex(9) 예약이 섞여 있다
    await fake_redis.set(usage_key, "15")
    await fake_redis.set(resvsum_key, "5")
    await fake_redis.hset(idx_key, "claude-code:req-cc", "5")
    await fake_redis.hset(idx_key, "codex:req-cd", "9")
    await fake_redis.set(
        "budget:pending:client:u-apps:claude-code:req-cc", "5"
    )
    await fake_redis.set("budget:pending:client:u-apps:codex:req-cd", "9")
    # 다른 앱의 dead 필드 — 스윕은 자기 앱 필드로 한정된다: 이 필드를
    # 지우면 cowork 행이 reconcile 되기 전 늦게 도착하는 settle 의
    # HEXISTS 생존 증거가 사라져 예약 복구가 안 된다(R23 r6 N-1).
    await fake_redis.hset(idx_key, "cowork:req-dead", "3")
    # 같은 앱의 dead 필드 — 이것만 스윕된다
    await fake_redis.hset(idx_key, "claude-code:req-dead", "2")

    await fake_redis.eval(
        _RECONCILE_LUA, 4,
        usage_key, resvsum_key, idx_key,
        "budget:pending:client:u-apps:claude-code:req-cc",
        "10", "5", "budget:pending:client:u-apps:", "claude-code:",
    )
    # live=5(claude-code 만) → orphan=0 — codex 예약이 부풀지 않는다
    assert float(await fake_redis.get(usage_key)) == pytest.approx(15.0)
    assert float(await fake_redis.get(resvsum_key)) == pytest.approx(5.0)
    # 라이브 codex 필드·cowork dead 필드는 유지, 자기 앱 dead 필드만 스윕
    assert await fake_redis.hexists(idx_key, "codex:req-cd") in (1, True)
    assert await fake_redis.hexists(idx_key, "cowork:req-dead") in (1, True)
    assert await fake_redis.hexists(idx_key, "claude-code:req-dead") in (0, False)


@pytest.mark.asyncio
async def test_settle_noop_when_marker_and_reserved_unknown(fake_redis):
    """마커 소실 + 예약치 미상(구버전 스펙) → 안전하게 no-op, 툼스톤 없음."""

    spec = _resv_spec(uid="u-old", req="req-old")
    await fake_redis.set(spec["usage_key"], "10")
    await fake_redis.set(spec["resvsum_key"], "10")

    script = _SETTLE_LUA
    result = json.loads(
        await fake_redis.eval(
            script, 4,
            spec["usage_key"], spec["config_key"], spec["marker_key"],
            spec["resvsum_key"],
            "15.00", "", "", "",  # reserved 미상
        )
    )
    assert result["settled"] is False
    assert float(await fake_redis.get(spec["usage_key"])) == pytest.approx(10.0)
