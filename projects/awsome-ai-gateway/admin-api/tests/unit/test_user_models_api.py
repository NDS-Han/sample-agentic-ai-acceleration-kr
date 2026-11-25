# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""GET /admin/analytics/user-models — 기간 검증, 정렬·페이지·검색, 캐시 격리 (DB 없음).

하이브리드==raw·DST·스코프의 실 PG 검증은 tests/regression/test_user_models_realpg.py.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.core.exceptions import DateRangeTooLongError, ValidationError
from app.models.auth import UserRole
from app.routers import analytics as an
from app.services import user_model_usage as umu


# ── 기간 검증 — /admin/analytics 와 같은 규칙 ─────────────────────────────────

def test_resolve_range_month_and_custom():
    r = umu.resolve_range("2026-02", None, None)
    assert (str(r.start), str(r.end), r.label) == ("2026-02-01", "2026-02-28", "2026-02")
    r = umu.resolve_range("2026-12", None, None)
    assert str(r.end) == "2026-12-31"
    r = umu.resolve_range("2026-11", "2026-01-01", "2026-12-31")  # custom 이 period 를 이긴다
    assert r.label == "2026-01-01~2026-12-31"


@pytest.mark.parametrize("args", [
    ("2026-11", "2026-11-01", None),       # 한쪽만 → 400
    ("2026-11", None, "2026-11-05"),
    ("2026-13", None, None),               # 형식
    ("2026-11", "2026-11-05", "2026-11-01"),  # 역전
    ("2026-11", "2026-02-30", "2026-03-01"),  # 달력
    (None, None, None),
])
def test_resolve_range_rejects_with_400(args):
    with pytest.raises(ValidationError) as ei:
        umu.resolve_range(*args)
    assert not isinstance(ei.value, DateRangeTooLongError)


def test_resolve_range_too_long_is_422():
    umu.resolve_range(None, "2025-01-01", "2026-01-01")  # 366일 — 허용(윤년 포함 상한)
    with pytest.raises(DateRangeTooLongError):
        umu.resolve_range(None, "2025-01-01", "2026-01-02")


# ── 정렬·페이지·검색·Other ────────────────────────────────────────────────────

def _payload():
    def u(i, name, team, buckets):
        return [f"00000000-0000-4000-a000-00000000000{i}", name, f"{name.lower()}@x.io", team,
                buckets]

    return {
        "v": 1, "timezone": "Asia/Kolkata", "start_date": "2026-11-01",
        "end_date": "2026-11-30", "period": "2026-11",
        "top_models": [["mA", "Model A", 1000], ["mB", None, 500]],
        "other_models": [["mZ", None, 10]],
        "users": [
            u(1, "Ann", "Core", [["mA", 100, 10, 5, 1, 1.5, 3], [None, 4, 1, 0, 0, 0.1, 1]]),
            u(2, "bob", "Edge", [["mB", 300, 50, 0, 0, 0.75, 2]]),
            u(3, "Cy", None, [["mA", 50, 5, 900, 100, 3.0, 9], ["mB", 1, 1, 0, 0, 0.01, 1],
                              [None, 2, 2, 0, 0, 0.02, 1]]),
        ],
    }


def test_rows_cells_other_totals_and_max_cell():
    r = umu.page_from_payload(_payload())
    assert [m["model"] for m in r["top_models"]] == ["mA", "mB"]
    assert r["other_models"][0]["model"] == "mZ"
    by = {row["name"]: row for row in r["rows"]}
    ann = by["Ann"]
    assert set(ann["cells"]) == {"mA"}                     # mB 안 씀 → 키 없음(UI 는 —)
    assert ann["cells"]["mA"] == {"input_tokens": 100, "output_tokens": 10,
                                  "cache_read_tokens": 5, "cache_write_tokens": 1,
                                  "total_tokens": 116, "cost_usd": 1.5, "requests": 3}
    assert ann["other"]["total_tokens"] == 5
    assert ann["total"]["total_tokens"] == 121 and ann["total"]["requests"] == 4
    assert by["bob"]["other"] is None
    # 히트 스케일 — 상위 모델 셀만, 전 사용자 기준
    assert r["max_cell"] == {"total": 1055, "input": 300, "output": 50, "cache": 1000}


@pytest.mark.parametrize("sort,direction,want", [
    ("total", "desc", ["Cy", "bob", "Ann"]),
    ("total", "asc", ["Ann", "bob", "Cy"]),
    ("input", "desc", ["bob", "Ann", "Cy"]),
    ("output", "desc", ["bob", "Ann", "Cy"]),
    ("cache", "desc", ["Cy", "Ann", "bob"]),
    ("cost", "desc", ["Cy", "Ann", "bob"]),
    ("name", "asc", ["Ann", "bob", "Cy"]),             # 대소문자 무시
    ("name", "desc", ["Cy", "bob", "Ann"]),
    ("model:mB", "desc", ["bob", "Cy", "Ann"]),        # 안 쓴 사용자는 뒤
    ("model:mB", "asc", ["Cy", "bob", "Ann"]),         # 방향과 무관하게 뒤
    ("other", "desc", ["Ann", "Cy", "bob"]),
    ("model:gone", "desc", ["Ann", "bob", "Cy"]),      # 모르는 모델 → 400 아님, 결정적 순서
])
def test_sorting(sort, direction, want):
    r = umu.page_from_payload(_payload(), sort=sort, direction=direction)
    assert [row["name"] for row in r["rows"]] == want


def test_paging_and_search():
    r = umu.page_from_payload(_payload(), page=2, page_size=2)
    assert [row["name"] for row in r["rows"]] == ["Ann"] and r["total_users"] == 3
    r = umu.page_from_payload(_payload(), q="EDGE")              # 팀 이름
    assert [row["name"] for row in r["rows"]] == ["bob"] and r["total_users"] == 1
    r = umu.page_from_payload(_payload(), q="ann@x")             # 이메일
    assert r["total_users"] == 1
    r = umu.page_from_payload(_payload(), q="nobody")
    assert r["rows"] == [] and r["total_users"] == 0
    # 검색해도 히트 스케일은 스코프 전체 기준 그대로(페이지마다 색이 바뀌지 않게)
    assert r["max_cell"]["total"] == 1055


@pytest.mark.parametrize("bad", ["", "tokens", "model:", "name;drop"])
def test_bad_sort_is_400(bad):
    with pytest.raises(ValidationError):
        umu.page_from_payload(_payload(), sort=bad)


# ── 라우터: 캐시 격리 — ADMIN·scope=all 만, 키 = 범위·앱·스코프 ────────────────

class _Redis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, k):
        return self.store.get(k)

    async def setex(self, k, ttl, v):
        self.store[k] = v
        self.ttls[k] = ttl


def _request(redis):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis)))


def _user(role):
    return SimpleNamespace(role=role, team_id=uuid.uuid4(), user_id=uuid.uuid4())


@pytest.fixture
def calls(monkeypatch):
    from app.core import read_cache

    read_cache.local_lru.clear()
    seen: list[tuple] = []

    async def _fetch(session, rng, scope_ids, client):
        seen.append((rng.start.isoformat(), rng.end.isoformat(), scope_ids, client))
        return _payload()

    async def _scope(actor, scope, session):
        if actor.role == UserRole.TEAM_LEADER:
            return None, [actor.team_id]
        return (None, None) if scope == "all" else (None, [uuid.UUID(scope[5:])])

    monkeypatch.setattr(umu, "fetch_scope_payload", _fetch)
    monkeypatch.setattr("app.services.analytics_service._resolve_scope", _scope)
    # "오늘" 고정 — 2026-11 은 진행 중인 달(짧은 TTL), 2026-10 은 지난 달(긴 TTL).
    monkeypatch.setattr("app.services.usage_rollup.reporting_today",
                        lambda tz=None: __import__("datetime").date(2026, 11, 24))
    yield seen
    read_cache.local_lru.clear()


async def _get(redis, user, **kw):
    params = dict(period="2026-11", start_date=None, end_date=None, scope="all", client=None,
                  sort="total", dir="desc", page=1, page_size=10, q=None)
    params.update(kw)
    return await an.get_user_models(_request(redis), user=user, session=None, **params)


async def test_admin_global_is_cached_and_sort_page_q_are_served_from_cache(calls):
    from app.core.config import get_settings

    redis = _Redis()
    admin = _user(UserRole.ADMIN)
    await _get(redis, admin)
    r = await _get(redis, admin, sort="name", dir="asc", page=1, page_size=2, q="a")
    assert len(calls) == 1, "정렬·페이지·검색이 DB 를 다시 읽었다"
    assert r["total_users"] == 1
    (key,) = redis.store
    assert redis.ttls[key] == get_settings().ANALYTICS_CACHE_TTL_SEC  # 오늘 포함 → 짧게
    assert "global" in key and "2026-11-01~2026-11-30" in key and key.endswith(":all")
    json.loads(redis.store[key])  # 직렬화 가능
    await _get(redis, admin, period="2026-10")
    past = next(k for k in redis.store if "2026-10-01" in k)
    assert redis.ttls[past] == get_settings().ANALYTICS_CACHE_TTL_PAST_SEC  # 지난 달 → 길게


async def test_page_requests_reuse_the_parsed_payload_without_redis_round_trip(calls):
    """3 MB 페이로드를 페이지마다 다시 파싱하지 않는다 — 프로세스 로컬 LRU 가 적중한다."""
    redis = _Redis()
    admin = _user(UserRole.ADMIN)
    await _get(redis, admin)
    gets = []
    orig = redis.get

    async def _spy(k):
        gets.append(k)
        return await orig(k)

    redis.get = _spy
    await _get(redis, admin, page=2, page_size=1)
    assert gets == [] and len(calls) == 1


async def test_cache_key_separates_range_client_and_custom(calls):
    redis = _Redis()
    admin = _user(UserRole.ADMIN)
    await _get(redis, admin)
    await _get(redis, admin, client="cowork")
    await _get(redis, admin, start_date="2026-11-01", end_date="2026-11-15")
    await _get(redis, admin, period="2026-10")
    assert len(calls) == 4 and len(redis.store) == 4


async def test_cache_key_carries_the_resolved_team_set(calls):
    """리더는 전사 캐시를 읽지 않는다 — 키의 범위 조각이 해석된 팀 집합이다.
    같은 팀 집합(ADMIN 의 team:T 와 T 의 리더)은 공유해도 된다(같은 데이터를 볼 권한)."""
    redis = _Redis()
    admin = _user(UserRole.ADMIN)
    await _get(redis, admin)                                # 전사 캐시가 채워진 상태에서
    leader = _user(UserRole.TEAM_LEADER)
    await _get(redis, leader)                               # 리더는 전사 엔트리를 읽지 않는다
    assert calls[-1][2] == [leader.team_id]
    await _get(redis, admin, scope=f"team:{uuid.uuid4()}")  # 다른 팀 — 또 다른 키
    assert len(calls) == 3 and len(redis.store) == 3
    await _get(redis, admin, scope=f"team:{leader.team_id}")  # 리더와 같은 팀 집합 — 적중
    assert len(calls) == 3


async def test_router_rejects_one_sided_range_and_bad_sort(calls):
    with pytest.raises(ValidationError):
        await _get(_Redis(), _user(UserRole.ADMIN), start_date="2026-11-01")
    with pytest.raises(ValidationError):
        await _get(_Redis(), _user(UserRole.ADMIN), sort="bogus")
    with pytest.raises(DateRangeTooLongError):
        await _get(_Redis(), _user(UserRole.ADMIN), start_date="2024-01-01",
                   end_date="2026-01-01")
    assert calls == []


def test_old_by_user_model_endpoint_is_gone():
    paths = {r.path for r in an.router.routes}
    assert "/admin/analytics/usage/by-user-model" not in paths
    assert {"/admin/analytics/user-models", "/admin/analytics/user-models/{user_id}"} <= paths
