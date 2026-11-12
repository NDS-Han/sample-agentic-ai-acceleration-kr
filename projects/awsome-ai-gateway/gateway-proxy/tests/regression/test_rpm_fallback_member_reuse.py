"""6차 리뷰 M1/m2 — 폴백 재입장이 RPM 멤버를 재사용할 때의 계약.

배경: enforce_rate_limits 가 폴백 후보마다 reservation_id(req#N)를 달리
쓰되 RPM 은 base request_id 를 유지한다(5차 M1 — 후보가 RPM 슬롯을 추가
소비하면 안 되므로). 그러면 rate_limit_check.lua 의 ZSET 멤버
(`request_id:<idx>`) 가 후보들 사이에 같아지는데, 되돌리기(undo_rpm)는
"이 호출이 새로 넣은 스코프"만 지워야 한다 — 멤버를 공유하는 후보의
undo 가 원래 시도의 슬롯까지 지우면, 원래 시도가 upstream 에 도달했는데도
RPM 카운트가 빠진다(실 Redis 재현으로 확인된 MAJOR).
"""

import json
from pathlib import Path

import fakeredis.aioredis as far
import pytest

from app.services.lua_loader import LuaScriptLoader
from app.services.rate_limit_scope import RateLimitScope, ScopeDescriptor
from app.services.rate_limit_service import RateLimitService


@pytest.fixture()
async def redis():
    import fakeredis.aioredis as far  # noqa: F811

    r = far.FakeRedis(decode_responses=True)
    LuaScriptLoader.load_all(
        Path(__file__).resolve().parents[2] / "src" / "app" / "redis_scripts"
    )
    yield r
    await r.aclose()


def _desc(alias: str = "*") -> ScopeDescriptor:
    return ScopeDescriptor(
        scope=RateLimitScope.USER,
        scope_id="u1",
        model_alias=alias,
        rpm_limit=3,
    )


@pytest.mark.asyncio
async def test_candidate_undo_keeps_original_rpm_slot(redis):
    """Opus 재현: 원 시도가 X:1 을 넣은 뒤 후보가 같은 request_id 로 재입장
    → 후보의 undo 가 X:1 을 지우면 안 된다."""
    svc = RateLimitService()
    key = "rl:{USER:u1:*}:rpm"  # build_rl_key 형태 — 실제 키는 아래에서 추출

    # 1차 시도 — 멤버 X:1 추가.
    r1 = await svc.check_multi_scope_rpm(redis, [_desc()], request_id="X")
    assert r1.allowed and r1.committed_keys, "원 시도는 슬롯을 새로 추가해야 한다"
    key = r1.committed_keys[0]
    assert await redis.zscore(key, "X:1") is not None

    # 후보 재입장 — 같은 request_id, 멤버 재사용이라 committed_keys 에서 빠진다.
    r2 = await svc.check_multi_scope_rpm(redis, [_desc()], request_id="X")
    assert r2.allowed
    assert key not in r2.committed_keys, (
        "멤버 재사용 스코프는 되돌리기 대상이 아니다 — 지우면 원 시도 슬롯 소실"
    )

    # 후보가 뒤 단계(TPM/비용/예산)에서 거절 → committed_keys 기준 undo.
    await svc.undo_rpm(redis, [_desc()], "X", committed_keys=r2.committed_keys)

    # 원 시도의 슬롯은 살아 있어야 한다 — upstream 에 도달한 요청의 카운트다.
    assert await redis.zscore(key, "X:1") is not None, (
        "후보 undo 가 원 시도의 RPM 슬롯을 지웠다"
    )
    assert await redis.zcard(key) == 1


@pytest.mark.asyncio
async def test_reentry_at_full_limit_not_self_rejected(redis):
    """m2: 한도가 꽉 찬 상태에서 같은 request_id 재입장은 자기 멤버를
    제외하고 검사해야 한다 — 자기 슬롯 때문에 429 되면 폴백이 막힌다."""
    svc = RateLimitService()
    key_holder = []

    # 남의 요청 2개 + 우리 요청 1개 = limit 3 꽉 참.
    for rid in ("a", "b", "X"):
        r = await svc.check_multi_scope_rpm(redis, [_desc()], request_id=rid)
        assert r.allowed, f"{rid} 거절됨"
        key_holder = r.committed_keys

    # 한도 꽉 참 상태에서 X 의 폴백 재입장 — 자기 멤버 제외 → 통과.
    r = await svc.check_multi_scope_rpm(redis, [_desc()], request_id="X")
    assert r.allowed, "자기 슬롯이 카운트에 포함돼 재입장이 거절됐다"

    # 정말로 새 요청이면 여전히 거절 — 제외는 자기 멤버 한정.
    r = await svc.check_multi_scope_rpm(redis, [_desc()], request_id="Y")
    assert not r.allowed and r.scope == "USER"


@pytest.mark.asyncio
async def test_reentry_after_window_expiry_counts_again(redis):
    """멤버가 윈도우 밖으로 밀려나면 재입장은 새 슬롯이다 — 재사용 판정이
    만료 멤버를 살아 있는 것으로 오인하면 영구 과소 계상."""
    svc = RateLimitService()
    r1 = await svc.check_multi_scope_rpm(redis, [_desc()], request_id="X")
    key = r1.committed_keys[0]

    # 멤버를 윈도우 밖(과거)으로 밀어 만료를 흉내낸다.
    await redis.zadd(key, {"X:1": 1})
    r2 = await svc.check_multi_scope_rpm(redis, [_desc()], request_id="X")
    assert r2.allowed and key in r2.committed_keys, (
        "만료된 멤버 재입장은 새 슬롯으로 세야 한다"
    )


@pytest.mark.asyncio
async def test_first_attempt_undo_still_removes_own_slot(redis):
    """원 시도가 뒤 단계에서 거절되면 자기 슬롯은 정상으로 지워진다 —
    계약을 좁혔다고 해서 원래의 정리가 깨지면 안 된다."""
    svc = RateLimitService()
    r = await svc.check_multi_scope_rpm(redis, [_desc()], request_id="X")
    assert r.allowed
    await svc.undo_rpm(redis, [_desc()], "X", committed_keys=r.committed_keys)
    assert await redis.zcard(r.committed_keys[0]) == 0
