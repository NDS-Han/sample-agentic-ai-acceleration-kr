# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""웹 검색 통제 — 월 검색 수 한도(사용자·팀 공용 풀) + 요청당 상한. **검색 수 기준**.

예산·HARD_BLOCK·CPM/CPH 와 완전히 별개다. 한도에 닿으면 web_search 도구만 빠지고
(soft block) 모델 호출은 그대로 간다 — 이 모듈은 어떤 경로에서도 요청을 거절하지 않는다.

## 판정 순서 (라우터)
global off(AgentCore 미배선) > app off(routing_profiles.web_search_enabled) >
월 한도 소진(soft block: 도구 미주입 + 모델 시스템 노트 + ``x-gateway-web-search:
monthly_limit_reached``) > 요청당 상한(루프 안에서 초과분은 실행하지 않고 오류 결과) > 허용.

## 정책 (budget.web_search_policies, migration 0059)
  - 사용자 월 한도 = USER 행 ?? ORG 행(사용자 1인 기본). TEAM 행의 월 한도는 **팀 공용
    풀**이라 사용자 기본값으로 상속하지 않는다 — 예산의 팀 풀 + 개인 한도와 같은 구조.
  - 팀 공용 풀 = TEAM 행(없으면 무제한). 사용자·팀 둘 다 걸리면 둘 다 확인한다.
  - 요청당 상한 = USER ?? TEAM ?? ORG ?? env ``web_search_max_searches_per_request``(5).
  - 프로세스 안 30초 TTL 캐시 — 관리자 변경은 최대 30초 뒤 반영된다.
  - DB 조회 실패 = 무제한 + env 상한(fail-open: 웹 검색 통제는 모델 호출을 막는 경계가 아니다).

## 카운터 (Redis, 리포팅 TZ 월)
  ``wsq:user:{<user_id>}:<YYYY-MM>`` / ``wsq:team:{<team_id>}:<YYYY-MM>`` (TTL 40일).
  - 검색 **직전** 범위별 원자 예약(check-and-INCR Lua, 키 1개씩 — Cluster 안전, hash tag 는
    예산 키와 같은 규칙). 사용자 → 팀 순서로 잡고 팀이 거절하면 사용자 몫을 되돌린다.
    AgentCore 호출이 실패하면 예약을 되돌린다 → 카운터 = 성공 검색 수 =
    usage_logs.web_search_count 와 같은 정의.
  - 범위별 예약이 원자적이라 동시 요청도 한도를 넘지 않는다(초과 0). 사용자 예약 후 팀
    거절 → 되돌리기 사이의 찰나에 같은 사용자의 다른 요청이 보수적으로 거절될 수는 있다.
  - 키가 없으면(새 달·축출·장애 복구) ``SUM(usage_logs.web_search_count)`` 로 재구성하되
    이미 있는 값과 **큰 쪽**을 남긴다(in-flight·워커 지연분 보존). Redis 가 통째로 날아가면
    워커가 아직 쓰지 않은 최근 검색만큼은 덜 셀 수 있다 — 그 이상은 아니다.
  - Redis 실패 = 검색 허용(fail-open, 로그).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text

logger = structlog.get_logger(__name__)

NOTICE_STATE_KEY = "web_search_notice"
NOTICE_HEADER = "x-gateway-web-search"
NOTICE_MONTHLY_LIMIT = "monthly_limit_reached"
COUNTER_TTL_S = 40 * 86400
POLICY_CACHE_TTL_S = 30.0
_POLICY_CACHE_MAX = 5000

#: 모델에게 주는 시스템 노트 — soft block 된 요청에만 붙는다.
MONTHLY_LIMIT_SYSTEM_NOTE = (
    "[Gateway notice] Web search is unavailable for the rest of this month: the monthly "
    "web search limit for this account or team has been reached. Answer without web "
    "search. If the user asks for current or online information, tell them the monthly "
    "web search limit was reached."
)

#: 범위 하나 — 한도 미만이면 INCR 하고 새 값, 아니면 -1. limit < 0 = 무제한(그래도 센다).
_RESERVE_LUA = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
local lim = tonumber(ARGV[1])
if lim >= 0 and cur >= lim then return -1 end
local v = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return v
"""
#: 예약 되돌리기 — 0 아래로 내려가지 않는다.
_RELEASE_LUA = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
if cur <= 0 then return 0 end
return redis.call('DECR', KEYS[1])
"""
#: 재구성 — DB 합계와 현재 값 중 큰 쪽을 남긴다(키 없음 = -1).
_REBUILD_LUA = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '-1')
local db = tonumber(ARGV[1])
if db > cur then
  redis.call('SET', KEYS[1], db, 'EX', ARGV[2])
  return db
end
return cur
"""

_POLICY_SQL = text(
    "SELECT scope, monthly_query_limit, per_request_cap FROM budget.web_search_policies "
    "WHERE scope = 'ORG' "
    "OR (scope = 'TEAM' AND scope_id = CAST(:team_id AS uuid)) "
    "OR (scope = 'USER' AND scope_id = CAST(:user_id AS uuid))"
)
_SUM_USER_SQL = text(
    "SELECT COALESCE(SUM(web_search_count), 0) FROM usage.usage_logs "
    "WHERE user_id = CAST(:id AS uuid) AND requested_at >= :start AND requested_at < :end"
)
_SUM_TEAM_SQL = text(
    "SELECT COALESCE(SUM(web_search_count), 0) FROM usage.usage_logs "
    "WHERE team_id = CAST(:id AS uuid) AND requested_at >= :start AND requested_at < :end"
)


def user_key(user_id: str, period: str) -> str:
    return f"wsq:user:{{{user_id}}}:{period}"


def team_key(team_id: str, period: str) -> str:
    return f"wsq:team:{{{team_id}}}:{period}"


@dataclass(frozen=True)
class WebSearchPolicy:
    #: 사용자 월 한도(None = 무제한).
    user_limit: int | None
    #: 팀 공용 풀 월 한도(None = 무제한).
    team_limit: int | None
    #: 요청당 검색 수 상한(≥1).
    per_request_cap: int


def resolve_policy(rows: list[tuple[str, int | None, int | None]],
                   default_cap: int) -> WebSearchPolicy:
    """(scope, monthly_query_limit, per_request_cap) 행들 → 유효 정책(모듈 docstring)."""
    by = {r[0]: r for r in rows}
    org, team, user = by.get("ORG"), by.get("TEAM"), by.get("USER")
    user_limit = user[1] if user and user[1] is not None else (org[1] if org else None)
    team_limit = team[1] if team else None
    cap = next((r[2] for r in (user, team, org) if r and r[2] is not None), default_cap)
    return WebSearchPolicy(user_limit=user_limit, team_limit=team_limit,
                           per_request_cap=max(1, int(cap)))


def iterations_for_cap(cap: int, per_turn: int) -> int:
    """요청당 상한을 채울 수 있는 검색 턴 수 — env 의 고정 2×3 을 대신한다."""
    if per_turn <= 0:
        return max(1, cap)
    return max(1, math.ceil(cap / per_turn))


def period_utc_range(period: str) -> tuple[datetime, datetime]:
    """리포팅 TZ 'YYYY-MM' → [월초, 다음 월초) (aware datetime — timestamptz 와 비교)."""
    from app.periods import _reporting_tz

    y, m = (int(x) for x in period.split("-"))
    tz = _reporting_tz()
    start = datetime(y, m, 1, tzinfo=tz)
    end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=tz)
    return start, end


class PolicyLoader:
    """budget.web_search_policies 를 (user, team) 별로 30초 캐시."""

    def __init__(self, ttl: float = POLICY_CACHE_TTL_S, clock=time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, WebSearchPolicy]] = {}

    async def load(self, session_factory, user_id: str, team_id: str | None,
                   default_cap: int) -> WebSearchPolicy:
        key = (user_id, team_id or "")
        hit = self._cache.get(key)
        now = self._clock()
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        rows: list = []
        if session_factory is not None:
            try:
                async with session_factory() as db:
                    res = await db.execute(_POLICY_SQL, {
                        "user_id": user_id,
                        "team_id": team_id or "00000000-0000-0000-0000-000000000000",
                    })
                    rows = [tuple(r) for r in res.all()]
            except Exception:
                # fail-open — 마지막 값이 있으면 그것, 없으면 무제한 + env 상한.
                logger.warning("web_search_policy_load_failed", exc_info=True)
                if hit:
                    return hit[1]
        policy = resolve_policy(rows, default_cap)
        if len(self._cache) >= _POLICY_CACHE_MAX:
            self._cache.clear()
        self._cache[key] = (now, policy)
        return policy


POLICY_LOADER = PolicyLoader()


async def _sum_from_db(session_factory, sql, scope_id: str, period: str) -> int | None:
    if session_factory is None:
        return None
    start, end = period_utc_range(period)
    try:
        async with session_factory() as db:
            return int((await db.execute(sql, {"id": scope_id, "start": start, "end": end}))
                       .scalar() or 0)
    except Exception:
        logger.warning("web_search_counter_db_sum_failed", exc_info=True)
        return None


async def ensure_counter(redis, key: str, session_factory, sql, scope_id: str,
                         period: str) -> int:
    """카운터 값. 키가 없으면 usage_logs 합계로 재구성(큰 쪽 유지)."""
    raw = await redis.get(key)
    if raw is not None:
        return int(raw)
    db_sum = await _sum_from_db(session_factory, sql, scope_id, period)
    if db_sum is None:
        return 0
    return int(await redis.eval(_REBUILD_LUA, 1, key, db_sum, COUNTER_TTL_S))


@dataclass
class WebSearchGate:
    """요청 1건의 웹 검색 판정."""

    #: 루프를 돌려도 되는가(app on + 월 한도 여유).
    enabled: bool
    #: 월 한도 소진으로 soft block 됐는가.
    monthly_limit_reached: bool = False
    policy: WebSearchPolicy | None = None
    user_id: str = ""
    team_id: str | None = None
    period: str = ""
    user_used: int = 0
    team_used: int = 0

    def max_iterations(self, per_turn: int, env_iterations: int) -> int:
        if self.policy is None:
            return env_iterations
        return iterations_for_cap(self.policy.per_request_cap, per_turn)

    def wrap(self, mcp_client: Any, redis) -> Any:
        if self.policy is None:
            return mcp_client
        return QuotaGuard(mcp_client, redis=redis, gate=self)


async def gate_web_search(*, state: dict, redis, session_factory, auth_context,
                          app_enabled: bool, default_cap: int) -> WebSearchGate:
    """판정 순서의 '월 한도' 단계. app off(또는 global off)면 아무것도 하지 않는다."""
    if not app_enabled:
        return WebSearchGate(enabled=False)
    from app.periods import request_period

    user_id = str(getattr(auth_context, "user_id", "") or "")
    team_id = getattr(auth_context, "team_id", None) or None
    team_id = str(team_id) if team_id else None
    if not user_id:
        return WebSearchGate(enabled=True)
    period = request_period()
    policy = await POLICY_LOADER.load(session_factory, user_id, team_id, default_cap)
    gate = WebSearchGate(enabled=True, policy=policy, user_id=user_id, team_id=team_id,
                         period=period)
    if redis is None:
        return gate
    try:
        gate.user_used = await ensure_counter(redis, user_key(user_id, period), session_factory,
                                              _SUM_USER_SQL, user_id, period)
        if team_id:
            gate.team_used = await ensure_counter(redis, team_key(team_id, period),
                                                  session_factory, _SUM_TEAM_SQL, team_id, period)
    except Exception:
        logger.warning("web_search_quota_counter_read_failed", exc_info=True)
        return gate
    over_user = policy.user_limit is not None and gate.user_used >= policy.user_limit
    over_team = (team_id is not None and policy.team_limit is not None
                 and gate.team_used >= policy.team_limit)
    if over_user or over_team:
        gate.enabled = False
        gate.monthly_limit_reached = True
        state[NOTICE_STATE_KEY] = NOTICE_MONTHLY_LIMIT
        logger.info("web_search_soft_blocked_monthly_limit", user_used=gate.user_used,
                    team_used=gate.team_used, scope="user" if over_user else "team")
    return gate


def add_monthly_limit_notice(body: dict, dialect: str) -> dict:
    """soft block 된 요청 본문에 모델용 시스템 노트를 붙인다(제자리, 같은 dict 반환).

    Anthropic: ``system`` 끝에(문자열이면 이어 붙이고, 블록 목록이면 text 블록 추가 —
    기존 cache_control 접두는 그대로라 프롬프트 캐시를 깨지 않는다).
    Responses: ``instructions`` 끝에.
    """
    if not isinstance(body, dict):
        return body
    if dialect == "responses":
        prev = body.get("instructions")
        body["instructions"] = (f"{prev}\n\n{MONTHLY_LIMIT_SYSTEM_NOTE}"
                                if isinstance(prev, str) and prev else MONTHLY_LIMIT_SYSTEM_NOTE)
        return body
    system = body.get("system")
    if isinstance(system, list):
        body["system"] = [*system, {"type": "text", "text": MONTHLY_LIMIT_SYSTEM_NOTE}]
    elif isinstance(system, str) and system:
        body["system"] = f"{system}\n\n{MONTHLY_LIMIT_SYSTEM_NOTE}"
    else:
        body["system"] = MONTHLY_LIMIT_SYSTEM_NOTE
    return body


class WebSearchQuotaExceeded(Exception):
    """요청 중에 월 한도에 닿았다 — 이 검색은 실행하지 않는다."""


class WebSearchRequestCapReached(Exception):
    """요청당 상한에 닿았다 — 이 검색은 실행하지 않는다."""


@dataclass
class QuotaGuard:
    """요청 1건 동안 AgentCore 클라이언트를 감싼다 — 검색마다 월 한도 예약, 요청당 상한.

    루프(_run_turn_searches)는 ``remaining_request_searches()`` 로 턴 허용량을 줄이고,
    ``monthly_limit_reached`` 가 서면 남은 검색을 월 한도 오류로 답한다.
    """

    inner: Any
    redis: Any
    gate: WebSearchGate
    #: 예약됐거나 끝난(성공) 검색 수 — 실패하면 되돌린다(요청당 상한 = 과금된 검색 수).
    reserved: int = 0
    monthly_limit_reached: bool = False

    def __getattr__(self, name: str) -> Any:  # ensure_initialized 등은 그대로 위임
        return getattr(self.inner, name)

    @property
    def per_request_cap(self) -> int:
        return self.gate.policy.per_request_cap if self.gate.policy else 0

    def remaining_request_searches(self) -> int:
        return max(0, self.per_request_cap - self.reserved)

    async def _reserve(self) -> list[str]:
        """범위별 예약. 성공하면 잡은 키 목록, 월 한도면 WebSearchQuotaExceeded."""
        g, p = self.gate, self.gate.policy
        if self.redis is None or p is None:
            return []
        plan = [(user_key(g.user_id, g.period), p.user_limit)]
        if g.team_id:
            plan.append((team_key(g.team_id, g.period), p.team_limit))
        taken: list[str] = []
        try:
            for key, limit in plan:
                v = await self.redis.eval(_RESERVE_LUA, 1, key,
                                          -1 if limit is None else int(limit), COUNTER_TTL_S)
                if int(v) < 0:
                    await self._release(taken)
                    raise WebSearchQuotaExceeded(key)
                taken.append(key)
        except WebSearchQuotaExceeded:
            raise
        except Exception:
            # Redis 장애 — fail-open(검색 허용). 잡은 몫은 되돌린다.
            logger.warning("web_search_quota_reserve_failed", exc_info=True)
            await self._release(taken)
            return []
        return taken

    async def _release(self, keys: list[str]) -> None:
        for key in keys:
            try:
                await self.redis.eval(_RELEASE_LUA, 1, key)
            except Exception:
                logger.warning("web_search_quota_release_failed", key=key)

    async def search(self, query: str, max_results: int = 10):
        if self.monthly_limit_reached:
            raise WebSearchQuotaExceeded("monthly")
        if self.remaining_request_searches() <= 0:
            raise WebSearchRequestCapReached()
        self.reserved += 1
        try:
            keys = await self._reserve()
        except WebSearchQuotaExceeded:
            self.reserved -= 1
            self.monthly_limit_reached = True
            raise
        try:
            return await self.inner.search(query, max_results)
        except BaseException:
            self.reserved -= 1
            await self._release(keys)
            raise
