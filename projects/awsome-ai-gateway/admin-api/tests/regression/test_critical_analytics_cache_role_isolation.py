# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: /admin/analytics 캐시가 role 격리를 깨뜨리지 않아야 한다.

`GET /admin/analytics` 는 `require_admin_or_team_leader` 이고, **같은 파라미터가 행위자에
따라 다른 데이터를 뜻한다** — `scope='all'` 이 ADMIN 에겐 전사(GLOBAL), TEAM_LEADER 에겐
본인(이 리드하는) 팀들이다(`analytics_service._resolve_scope`).

그래서 캐시 키에 요청 문자열(scope='all')만 넣으면 이런 유출이 난다:

    1. ADMIN 이 GET /admin/analytics?period=2026-09&group_by=team&scope=all
       → 키 `analytics:2026-09:team:all` 에 **전사** 응답 저장
    2. TTL 안에 TEAM_LEADER 가 **바이트 단위로 동일한** 요청
       → 캐시 적중, 전사 데이터를 그대로 수신

wave 2 선택(종전: TEAM_LEADER·team: 범위는 캐시 안 함 → 리더 화면은 매번 raw 스캔):
**해석된 팀 집합**을 키에 넣는다. 범위 해석(_resolve_scope — 권한 검사 포함)이 캐시보다 먼저
돌고, 키의 범위 조각은 ADMIN 전사='global', 그 밖='teams=<정렬된 uuid 목록>' 이다. 응답은
(기간·group_by·client·팀 집합)만의 함수이므로 같은 팀 집합을 볼 권한이 있는 두 행위자는 같은
응답을 공유해도 된다. 이 파일은 그 불변식을 행동으로 고정한다.
"""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path

import pytest

from app.core import read_cache
from app.models.auth import UserRole
from app.routers import analytics as an

SOURCE = Path(an.__file__)


@pytest.fixture(autouse=True)
def _fresh_local_cache():
    read_cache.local_lru.clear()
    yield
    read_cache.local_lru.clear()


# ──────────────────────────────────────────────────────────────────────────────
# 0) 공허성 대조군 — 범위 해석이 캐시 키보다 먼저, 키는 해석된 집합에서
# ──────────────────────────────────────────────────────────────────────────────


def test_scope_is_resolved_before_the_cache_key_is_built():
    src = inspect.getsource(an.get_analytics)
    i_resolve = src.index("_resolve_scope(")
    i_key = src.index("_analytics_cache_key(")
    assert i_resolve < i_key, "캐시 키가 권한 검사(범위 해석)보다 먼저 만들어진다"
    assert "scope_ids=scope_ids" in src[i_key:], "키가 해석된 팀 집합을 받지 않는다"


def test_cache_key_carries_the_resolved_team_set():
    a, b = uuid.uuid4(), uuid.uuid4()
    k_global = an._analytics_cache_key(period="2026-09", group_by="team", client="all")
    k_ab = an._analytics_cache_key(period="2026-09", group_by="team", client="all",
                                   scope_ids=[a, b])
    k_ba = an._analytics_cache_key(period="2026-09", group_by="team", client="all",
                                   scope_ids=[b, a])
    k_a = an._analytics_cache_key(period="2026-09", group_by="team", client="all",
                                  scope_ids=[a])
    assert "global" in k_global and "global" not in k_ab
    assert k_ab == k_ba, "같은 팀 집합은 순서와 무관하게 같은 키"
    assert len({k_global, k_ab, k_a}) == 3, "전사 / 두 팀 / 한 팀 은 서로 다른 키"
    # 응답을 바꾸는 group_by, period, client, custom 구간은 각각 다른 키
    assert k_global != an._analytics_cache_key(period="2026-09", group_by="user", client="all")
    assert k_global != an._analytics_cache_key(period="2026-08", group_by="team", client="all")
    assert k_global != an._analytics_cache_key(period="2026-09", group_by="team", client="codex")
    assert k_global == an._analytics_cache_key(period="2026-09", group_by="team", client=None)
    assert k_global != an._analytics_cache_key(period="2026-09", group_by="team", client=None,
                                               start_date="2026-09-01", end_date="2026-09-10")


def test_the_leaky_key_shape_is_not_used():
    """참조 구현이 처음 배포한 유출 키 형태가 재도입되지 않았는지."""
    src = SOURCE.read_text(encoding="utf-8")
    assert 'f"analytics:{period}:{group_by}:{scope}"' not in src


# ──────────────────────────────────────────────────────────────────────────────
# 1) 행동 증명
# ──────────────────────────────────────────────────────────────────────────────


class _SpyRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.gets: list[str] = []
        self.sets: list[str] = []

    async def get(self, key):
        self.gets.append(key)
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.sets.append(key)
        self.store[key] = value

    async def set(self, key, value, nx=False, px=None):
        return True

    async def eval(self, *a):
        return 1


class _Req:
    def __init__(self, redis, svc) -> None:
        state = type("S", (), {})()
        state.redis = redis
        state.analytics_service = svc
        self.app = type("A", (), {"state": state})()


class _Svc:
    """해석된 범위에 따라 다른 본문을 돌려주는 대역 — 유출이 나면 눈에 보인다."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_analytics(self, session, *, scope_ids, **_kw):
        self.calls += 1
        return {"secret": "ORG_WIDE" if scope_ids is None
                else "TEAMS:" + ",".join(sorted(str(t) for t in scope_ids))}


def _actor(role: UserRole, team_id=None):
    return type("U", (), {"role": role, "team_id": team_id, "user_id": uuid.uuid4()})()


@pytest.fixture
def leader_teams(monkeypatch):
    """TEAM_LEADER 가 리드하는 팀 — _leader_allowed_team_ids 를 고정한다(DB 없이)."""
    teams: dict[uuid.UUID, set[uuid.UUID]] = {}

    async def _allowed(actor, session):
        return teams.get(actor.user_id, {actor.team_id} if actor.team_id else set())

    import app.services.analytics_service as svc_mod

    monkeypatch.setattr(svc_mod, "_leader_allowed_team_ids", _allowed)
    return teams


async def _get(req, user, scope="all", group_by="team"):
    return await an.get_analytics(
        req, period="2026-09", group_by=group_by, scope=scope, client=None,
        start_date=None, end_date=None, user=user, session=None,
    )


@pytest.mark.asyncio
async def test_team_leader_never_reads_the_admin_cache_entry(leader_teams):
    """⚠️ 핵심 — ADMIN 이 캐시를 채운 뒤 TEAM_LEADER 가 같은 요청을 해도 전사 데이터가 안 간다."""
    redis, svc = _SpyRedis(), _Svc()
    req = _Req(redis, svc)
    admin_out = await _get(req, _actor(UserRole.ADMIN))
    assert admin_out["secret"] == "ORG_WIDE"
    assert redis.sets, "ADMIN 응답이 캐시되지 않았다 — 캐시가 아예 동작하지 않는 것"

    team = uuid.uuid4()
    tl_out = await _get(req, _actor(UserRole.TEAM_LEADER, team_id=team))
    assert tl_out["secret"] == f"TEAMS:{team}", f"TEAM_LEADER 가 다른 범위 응답을 받았다: {tl_out}"
    assert svc.calls == 2, "TEAM_LEADER 요청이 ADMIN 캐시 엔트리를 적중했다"
    assert all("global" not in k for k in redis.sets[1:])


@pytest.mark.asyncio
async def test_two_leaders_of_different_teams_never_share(leader_teams):
    redis, svc = _SpyRedis(), _Svc()
    req = _Req(redis, svc)
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    a = await _get(req, _actor(UserRole.TEAM_LEADER, team_id=t1))
    b = await _get(req, _actor(UserRole.TEAM_LEADER, team_id=t2))
    assert a["secret"] == f"TEAMS:{t1}" and b["secret"] == f"TEAMS:{t2}"
    assert svc.calls == 2


@pytest.mark.asyncio
async def test_leader_with_extra_led_team_gets_a_different_key(leader_teams):
    """소속 팀만 같고 리드하는 팀 집합이 다른 두 리더 — 범위가 다르므로 공유하지 않는다."""
    redis, svc = _SpyRedis(), _Svc()
    req = _Req(redis, svc)
    home, extra = uuid.uuid4(), uuid.uuid4()
    plain = _actor(UserRole.TEAM_LEADER, team_id=home)
    wide = _actor(UserRole.TEAM_LEADER, team_id=home)
    leader_teams[wide.user_id] = {home, extra}
    a = await _get(req, plain)
    b = await _get(req, wide)
    assert a["secret"] == f"TEAMS:{home}"
    assert b["secret"] == "TEAMS:" + ",".join(sorted(str(t) for t in (home, extra)))
    assert svc.calls == 2


@pytest.mark.asyncio
async def test_leader_of_foreign_team_is_rejected_before_cache(leader_teams):
    """남의 팀 scope 요청은 캐시를 조회하기 **전에** 403 — 캐시가 권한 검사를 우회하지 않는다."""
    from app.core.exceptions import ForbiddenError

    redis, svc = _SpyRedis(), _Svc()
    req = _Req(redis, svc)
    await _get(req, _actor(UserRole.ADMIN), scope=f"team:{uuid.uuid4()}")  # 캐시를 채워 둔다
    with pytest.raises(ForbiddenError):
        await _get(req, _actor(UserRole.TEAM_LEADER, team_id=uuid.uuid4()),
                   scope=f"team:{uuid.uuid4()}")
    assert len(redis.gets) == 1, "거부된 요청이 캐시를 조회했다"


@pytest.mark.asyncio
async def test_same_team_scope_is_shared_between_admin_and_its_leader(leader_teams):
    """같은 팀 집합 = 같은 데이터 = 공유 가능(적중률 근거). ADMIN 의 team:X 와 X 의 리더."""
    redis, svc = _SpyRedis(), _Svc()
    req = _Req(redis, svc)
    team = uuid.uuid4()
    a = await _get(req, _actor(UserRole.ADMIN), scope=f"team:{team}")
    b = await _get(req, _actor(UserRole.TEAM_LEADER, team_id=team))
    assert a == b and svc.calls == 1


@pytest.mark.asyncio
async def test_admin_global_request_is_cached_and_hits(leader_teams):
    redis, svc = _SpyRedis(), _Svc()
    req = _Req(redis, svc)
    a = await _get(req, _actor(UserRole.ADMIN), group_by="model")
    b = await _get(req, _actor(UserRole.ADMIN), group_by="model")
    assert a == b
    assert svc.calls == 1, f"두 번째 요청이 캐시를 타지 않았다 (calls={svc.calls})"


# ──────────────────────────────────────────────────────────────────────────────
# 2) 캐시 직렬화 — 적중 응답이 첫 응답과 같은 JSON 객체여야 한다
# ──────────────────────────────────────────────────────────────────────────────
#
# 서비스는 pydantic 모델(AnalyticsResponse)을 돌려준다. 그걸 json.dumps(..., default=str)
# 로 저장하면 모델 통째가 str() repr 문자열로 들어가, 두 번째 요청이 JSON 객체가 아니라
# 문자열을 받는다 — admin-ui 는 cost_summary 가 undefined 라 분석 화면이 깨진다.


class _Model(__import__("pydantic").BaseModel):
    period: str
    total_cost_usd: float


@pytest.mark.asyncio
async def test_cache_round_trip_of_a_pydantic_response_is_a_dict():
    redis = _SpyRedis()

    async def compute():
        return _Model(period="2026-09", total_cost_usd=1.5)

    first = await read_cache.cached(redis, "k", compute, ttl=30)
    got = await read_cache.cached(redis, "k", compute, ttl=30)
    assert first == got == {"period": "2026-09", "total_cost_usd": 1.5}, got


@pytest.mark.asyncio
async def test_cache_get_treats_a_non_object_entry_as_a_miss():
    """이미 문자열로 잘못 저장된 항목(예전 형식)은 돌려주지 말고 다시 계산하게 한다."""
    redis = _SpyRedis()
    redis.store["k"] = '"period=\'2026-09\' total_cost_usd=1.5"'
    calls = []

    async def compute():
        calls.append(1)
        return {"ok": True}

    assert await read_cache.cached(redis, "k", compute, ttl=30) == {"ok": True}
    assert calls == [1]
