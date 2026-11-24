# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""웹 검색 통제 — 월 검색 수 한도(사용자 + 팀 공용 풀) + 요청당 상한 (services/web_search_quota).

  - 정책 해석: 요청당 상한 USER → TEAM → ORG → env, 사용자 월 한도 USER → ORG, 팀 풀 = TEAM
  - 팀 공용 풀과 개인 한도를 함께 확인
  - 동시 요청에서도 한도를 넘지 않는다(범위별 원자 예약 — 초과 0)
  - 카운터 재구성: 키가 없으면 usage_logs 합계, 있으면 큰 쪽 유지
  - AgentCore 실패 시 예약 되돌림(카운터 = 성공 검색 수)
  - 요청당 상한·요청 중 월 한도 도달 → 실행하지 않고 오류 결과(요청은 계속)
  - soft block: /v1/messages(claude-code·cowork)·/v1/responses(codex) 에서 도구 미주입 +
    시스템 노트 + x-gateway-web-search 헤더, 모델 호출·입장 심사(예산·레이트리밋)는 그대로
  - app off 가 월 한도보다 먼저(정책·카운터를 보지도 않는다)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from starlette.datastructures import Headers

import app.services.web_search_quota as q
from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    AuthType,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    Role,
)
from app.services.agentcore_mcp_client import AgentCoreMcpTimeout

USER = "00000000-0000-0000-0000-0000000000a1"
USER2 = "00000000-0000-0000-0000-0000000000a2"
USER3 = "00000000-0000-0000-0000-0000000000a3"
TEAM = "00000000-0000-0000-0000-0000000000b1"
PERIOD = "2026-11"


@pytest.fixture(autouse=True)
def _fresh_policy_cache():
    q.POLICY_LOADER._cache.clear()
    yield
    q.POLICY_LOADER._cache.clear()


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _auth(user=USER, team=TEAM) -> AuthContext:
    return AuthContext(user_id=user, team_id=team, dept_id="00000000-0000-0000-0000-0000000000c1",
                       roles=[Role.DEVELOPER], auth_type=AuthType.VIRTUAL_KEY, key_id="k1",
                       allowed_models=None, sso_subject="sub-1")


def _policy(user_limit=None, team_limit=None, cap=5):
    return q.WebSearchPolicy(user_limit=user_limit, team_limit=team_limit, per_request_cap=cap)


def _patch_policy(monkeypatch, policy):
    calls = []

    async def _load(session_factory, user_id, team_id, default_cap):
        calls.append((user_id, team_id))
        return policy

    monkeypatch.setattr(q.POLICY_LOADER, "load", _load)
    monkeypatch.setattr("app.periods.request_period", lambda: PERIOD)
    return calls


# ── 정책 해석 ────────────────────────────────────────────────────────────────
class TestResolvePolicy:
    def test_no_rows_is_unlimited_with_env_cap(self):
        assert q.resolve_policy([], 5) == _policy(None, None, 5)

    def test_cap_resolves_user_then_team_then_org(self):
        org, team, user = ("ORG", None, 9), ("TEAM", None, 7), ("USER", None, 3)
        assert q.resolve_policy([org, team, user], 5).per_request_cap == 3
        assert q.resolve_policy([org, team], 5).per_request_cap == 7
        assert q.resolve_policy([org], 5).per_request_cap == 9
        assert q.resolve_policy([("USER", 10, None), org], 5).per_request_cap == 9  # NULL = 상속

    def test_user_limit_is_user_then_org_and_team_is_a_pool(self):
        rows = [("ORG", 100, None), ("TEAM", 1000, None), ("USER", 20, None)]
        p = q.resolve_policy(rows, 5)
        assert (p.user_limit, p.team_limit) == (20, 1000)
        p = q.resolve_policy([("ORG", 100, None), ("TEAM", 1000, None)], 5)
        # 팀 행의 월 한도는 팀 공용 풀이지 사용자 기본값이 아니다.
        assert (p.user_limit, p.team_limit) == (100, 1000)
        assert q.resolve_policy([("TEAM", 50, None)], 5).user_limit is None

    def test_cap_replaces_fixed_rounds(self):
        assert q.iterations_for_cap(5, 3) == 2
        assert q.iterations_for_cap(1, 3) == 1
        assert q.iterations_for_cap(10, 3) == 4
        assert q.iterations_for_cap(4, 0) == 4

    async def test_loader_caches_and_fails_open(self):
        calls = []

        class _S:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def execute(self, stmt, params):
                calls.append(params)
                if len(calls) > 1:
                    raise RuntimeError("db down")
                r = MagicMock()
                r.all.return_value = [("ORG", 10, None), ("USER", None, 2)]
                return r

        now = [0.0]
        loader = q.PolicyLoader(ttl=30, clock=lambda: now[0])
        p = await loader.load(lambda: _S(), USER, TEAM, 5)
        assert p == _policy(10, None, 2)
        assert calls[0] == {"user_id": USER, "team_id": TEAM}
        now[0] = 10
        assert await loader.load(lambda: _S(), USER, TEAM, 5) == p and len(calls) == 1
        now[0] = 40  # 만료 → DB 장애 → 마지막 값
        assert await loader.load(lambda: _S(), USER, TEAM, 5) == p
        fresh = q.PolicyLoader()
        assert await fresh.load(lambda: _S(), USER, TEAM, 5) == _policy(None, None, 5)


# ── 게이트: 팀 공용 풀 + 개인 한도 ────────────────────────────────────────────────
class TestGate:
    async def _gate(self, monkeypatch, redis, policy, auth=None, app_enabled=True):
        _patch_policy(monkeypatch, policy)
        state: dict = {}
        g = await q.gate_web_search(state=state, redis=redis, session_factory=None,
                                    auth_context=auth or _auth(), app_enabled=app_enabled,
                                    default_cap=5)
        return g, state

    async def test_under_both_limits_is_enabled(self, monkeypatch, redis):
        await redis.set(q.user_key(USER, PERIOD), 3)
        await redis.set(q.team_key(TEAM, PERIOD), 10)
        g, state = await self._gate(monkeypatch, redis, _policy(5, 20))
        assert g.enabled and not g.monthly_limit_reached
        assert (g.user_used, g.team_used) == (3, 10)
        assert q.NOTICE_STATE_KEY not in state

    async def test_user_limit_reached_soft_blocks(self, monkeypatch, redis):
        await redis.set(q.user_key(USER, PERIOD), 5)
        g, state = await self._gate(monkeypatch, redis, _policy(5, 1000))
        assert not g.enabled and g.monthly_limit_reached
        assert state[q.NOTICE_STATE_KEY] == "monthly_limit_reached"

    async def test_team_pool_reached_blocks_even_with_personal_room(self, monkeypatch, redis):
        await redis.set(q.user_key(USER, PERIOD), 1)
        await redis.set(q.team_key(TEAM, PERIOD), 40)  # 팀원 합계
        g, _ = await self._gate(monkeypatch, redis, _policy(100, 40))
        assert g.monthly_limit_reached

    async def test_zero_limit_blocks_immediately(self, monkeypatch, redis):
        g, _ = await self._gate(monkeypatch, redis, _policy(0, None))
        assert g.monthly_limit_reached

    async def test_teamless_user_ignores_team_pool(self, monkeypatch, redis):
        g, _ = await self._gate(monkeypatch, redis, _policy(None, 0),
                                auth=_auth(team=""))
        assert g.enabled

    async def test_app_off_precedes_quota(self, monkeypatch, redis):
        calls = _patch_policy(monkeypatch, _policy(0, 0))
        state: dict = {}
        g = await q.gate_web_search(state=state, redis=redis, session_factory=None,
                                    auth_context=_auth(), app_enabled=False, default_cap=5)
        assert not g.enabled and not g.monthly_limit_reached
        assert calls == [] and state == {}           # 정책도 안 읽고, 노트·헤더도 없다
        assert await redis.keys("*") == []

    async def test_redis_failure_fails_open(self, monkeypatch):
        broken = MagicMock()
        broken.get = AsyncMock(side_effect=ConnectionError("down"))
        g, state = await self._gate(monkeypatch, broken, _policy(0, 0))
        assert g.enabled and not g.monthly_limit_reached and state == {}


# ── 카운터 재구성 ───────────────────────────────────────────────────────────────
class _SumSession:
    def __init__(self, value):
        self.value = value
        self.params = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params):
        self.params.append((str(stmt), params))
        r = MagicMock()
        r.scalar.return_value = self.value
        return r


class TestRebuild:
    async def test_missing_key_is_rebuilt_from_usage_logs(self, redis, monkeypatch):
        monkeypatch.setattr("app.periods.request_period", lambda: PERIOD)
        sf = _SumSession(7)
        v = await q.ensure_counter(redis, q.user_key(USER, PERIOD), sf, q._SUM_USER_SQL,
                                   USER, PERIOD)
        assert v == 7 and await redis.get(q.user_key(USER, PERIOD)) == "7"
        sql, params = sf.params[0]
        assert "SUM(web_search_count)" in sql and "user_id" in sql
        assert params["id"] == USER
        # 리포팅 TZ 월 경계(aware) — 2026-11 의 [11/1, 12/1)
        assert params["start"].month == 11 and params["end"].month == 12
        assert params["start"].tzinfo is not None
        assert await redis.ttl(q.user_key(USER, PERIOD)) > 30 * 86400

    async def test_existing_key_is_not_overwritten(self, redis):
        await redis.set(q.user_key(USER, PERIOD), 4)
        sf = _SumSession(99)
        assert await q.ensure_counter(redis, q.user_key(USER, PERIOD), sf, q._SUM_USER_SQL,
                                      USER, PERIOD) == 4
        assert sf.params == []

    async def test_rebuild_keeps_the_larger_value(self, redis):
        key = q.user_key(USER, PERIOD)
        await redis.set(key, 9)   # in-flight 예약이 DB 보다 앞서 있다
        assert await redis.eval(q._REBUILD_LUA, 1, key, 7, 100) == 9
        assert await redis.eval(q._REBUILD_LUA, 1, key, 12, 100) == 12
        assert await redis.get(key) == "12"

    async def test_db_unavailable_counts_from_zero_without_writing(self, redis):
        assert await q.ensure_counter(redis, q.user_key(USER, PERIOD), None, q._SUM_USER_SQL,
                                      USER, PERIOD) == 0
        assert await redis.exists(q.user_key(USER, PERIOD)) == 0


# ── 예약: 동시성·되돌림·요청당 상한 ────────────────────────────────────────────
def _inner(delay=0.0, fail=False):
    inner = MagicMock()

    async def _search(query, max_results=10):
        await asyncio.sleep(delay)
        if fail:
            raise AgentCoreMcpTimeout("timeout")
        return SimpleNamespace(results=[], raw_text='{"results": []}')

    inner.search = AsyncMock(side_effect=_search)
    return inner


def _guard(redis, policy, user=USER, team=TEAM, inner=None):
    gate = q.WebSearchGate(enabled=True, policy=policy, user_id=user, team_id=team, period=PERIOD)
    return gate.wrap(inner or _inner(delay=0.001), redis)


class TestReserve:
    async def test_concurrency_never_exceeds_user_limit(self, redis):
        """동시 요청 20건 × 검색 1회, 개인 한도 5 → 정확히 5건만 실행, 초과 0."""
        guards = [_guard(redis, _policy(5, None)) for _ in range(20)]
        res = await asyncio.gather(*(g.search("x") for g in guards), return_exceptions=True)
        ok = [r for r in res if not isinstance(r, Exception)]
        refused = [r for r in res if isinstance(r, q.WebSearchQuotaExceeded)]
        assert len(ok) == 5 and len(refused) == 15
        assert await redis.get(q.user_key(USER, PERIOD)) == "5"
        assert sum(g.inner.search.await_count for g in guards) == 5  # AgentCore 호출도 5회

    async def test_concurrency_shared_team_pool_across_users(self, redis):
        """세 사용자가 팀 풀 4 를 나눠 쓴다 — 개인 한도는 여유, 팀 합계가 4 를 넘지 않는다."""
        guards = [_guard(redis, _policy(100, 4), user=u)
                  for u in (USER, USER2, USER3) for _ in range(5)]
        res = await asyncio.gather(*(g.search("x") for g in guards), return_exceptions=True)
        assert sum(not isinstance(r, Exception) for r in res) == 4
        assert await redis.get(q.team_key(TEAM, PERIOD)) == "4"
        users = [int(await redis.get(q.user_key(u, PERIOD)) or 0) for u in (USER, USER2, USER3)]
        assert sum(users) == 4  # 팀 거절 시 개인 몫은 되돌렸다

    async def test_failed_search_releases_reservation(self, redis):
        g = _guard(redis, _policy(5, 10), inner=_inner(fail=True))
        with pytest.raises(AgentCoreMcpTimeout):
            await g.search("x")
        assert await redis.get(q.user_key(USER, PERIOD)) == "0"
        assert await redis.get(q.team_key(TEAM, PERIOD)) == "0"
        assert g.remaining_request_searches() == 5  # 실패는 요청당 상한도 쓰지 않는다

    async def test_unlimited_still_counts(self, redis):
        g = _guard(redis, _policy(None, None))
        await g.search("x")
        assert await redis.get(q.user_key(USER, PERIOD)) == "1"
        assert await redis.get(q.team_key(TEAM, PERIOD)) == "1"

    async def test_per_request_cap(self, redis):
        g = _guard(redis, _policy(None, None, cap=2))
        await g.search("a")
        await g.search("b")
        assert g.remaining_request_searches() == 0
        with pytest.raises(q.WebSearchRequestCapReached):
            await g.search("c")
        assert g.inner.search.await_count == 2

    async def test_only_quota_keys_are_touched(self, redis):
        """예산·레이트리밋 키에는 손대지 않는다 — 검색 수 기준 통제만."""
        g = _guard(redis, _policy(3, 3))
        await g.search("a")
        keys = sorted(await redis.keys("*"))
        assert keys == [q.team_key(TEAM, PERIOD), q.user_key(USER, PERIOD)]
        assert all(k.startswith("wsq:") for k in keys)


# ── 루프 안: 요청당 상한·요청 중 월 한도 도달 ─────────────────────────────────────
class TestLoopIntegration:
    async def test_turn_allowance_follows_request_cap(self, redis):
        from app.services.web_search_loop import _run_turn_searches

        g = _guard(redis, _policy(None, None, cap=2))
        out = await _run_turn_searches(g, [{"query": "a"}, {"query": "b"}, {"query": "c"}],
                                       allowance=3, deadline=1e12, default_max_results=5,
                                       max_result_chars=0, result_text_chars=0)
        assert [o[1] for o in out] == [True, True, False]
        assert out[2][3] == "capped"
        # 다음 턴 — 남은 상한 0 이면 "이 요청의 검색 예산 소진" 으로 답한다(재시도 유도 없음).
        out2 = await _run_turn_searches(g, [{"query": "d"}], allowance=3, deadline=1e12,
                                        default_max_results=5, max_result_chars=0,
                                        result_text_chars=0)
        assert out2[0][3] == "exhausted" and "used up" in out2[0][0]
        assert g.inner.search.await_count == 2

    async def test_mid_request_crossing_monthly_limit(self, redis):
        """개인 한도 1 — 같은 턴 두 번째 검색과 다음 턴 검색은 월 한도 오류, 요청은 계속."""
        from app.services.web_search_loop import _run_turn_searches

        g = _guard(redis, _policy(1, None, cap=5), inner=_inner(delay=0))
        out = await _run_turn_searches(g, [{"query": "a"}, {"query": "b"}], allowance=3,
                                       deadline=1e12, default_max_results=5,
                                       max_result_chars=0, result_text_chars=0)
        oks = sorted(o[1] for o in out)
        assert oks == [False, True]
        refused = next(o for o in out if not o[1])
        assert "monthly web search limit" in refused[0]
        assert "monthly web search limit reached" in refused[2]  # 🔎 trace line
        assert g.monthly_limit_reached
        nxt = await _run_turn_searches(g, [{"query": "c"}], allowance=3, deadline=1e12,
                                       default_max_results=5, max_result_chars=0,
                                       result_text_chars=0)
        assert "monthly web search limit" in nxt[0][0] and nxt[0][3] == "exhausted"
        assert g.inner.search.await_count == 1
        assert await redis.get(q.user_key(USER, PERIOD)) == "1"

    def test_budget_sentence_states_total_cap(self, redis):
        from app.services.web_search_loop import _anthropic_tool_def, _request_cap

        g = _guard(redis, _policy(None, None, cap=4))
        assert _request_cap(g) == 4
        assert _request_cap(MagicMock()) == 0
        desc = _anthropic_tool_def((3, 2, _request_cap(g)))["description"]
        assert "at most 4 searches in total" in desc


# ── 시스템 노트·헤더 ────────────────────────────────────────────────────────────
class TestNotice:
    def test_anthropic_system_variants(self):
        assert q.add_monthly_limit_notice({}, "anthropic")["system"] == q.MONTHLY_LIMIT_SYSTEM_NOTE
        assert q.add_monthly_limit_notice({"system": "Be brief."}, "anthropic")["system"] == (
            "Be brief.\n\n" + q.MONTHLY_LIMIT_SYSTEM_NOTE)
        blocks = [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}]
        out = q.add_monthly_limit_notice({"system": blocks}, "anthropic")["system"]
        assert out[0] == blocks[0]  # 캐시 접두 그대로
        assert out[1] == {"type": "text", "text": q.MONTHLY_LIMIT_SYSTEM_NOTE}

    def test_responses_instructions(self):
        assert q.add_monthly_limit_notice({"instructions": "X"}, "responses")["instructions"] == (
            "X\n\n" + q.MONTHLY_LIMIT_SYSTEM_NOTE)
        assert q.add_monthly_limit_notice({}, "responses")["instructions"] == (
            q.MONTHLY_LIMIT_SYSTEM_NOTE)

    async def test_header_injected_from_state(self):
        from app.middleware.otel import HeaderInjectorMiddleware

        async def _app(scope, receive, send):
            scope["state"]["web_search_notice"] = "monthly_limit_reached"
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        sent = []

        async def _send(m):
            sent.append(m)

        await HeaderInjectorMiddleware(_app)({"type": "http", "state": {}}, None, _send)
        headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
        assert headers["x-gateway-web-search"] == "monthly_limit_reached"


# ── 라우터: soft block ─────────────────────────────────────────────────────────
def _model_config() -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id="anthropic.claude-opus", alias="claude-opus",
        provider=ProviderType.BEDROCK, api_format=ApiFormat.ANTHROPIC_MESSAGES,
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        status=ModelStatus.ACTIVE, created_at=datetime(2026, 1, 1),
    )


class _FakeRequest:
    def __init__(self, body: dict, state: dict, app_state):
        self._body = json.dumps(body).encode()
        self.scope = {"state": state}
        self.app = SimpleNamespace(state=app_state)
        self.headers = Headers()

    async def body(self) -> bytes:
        return self._body

    async def is_disconnected(self) -> bool:
        return False


class _ReachedFallback(Exception):
    def __init__(self, kwargs):
        super().__init__("fallback")
        self.kwargs = kwargs


def _messages_app_state(web_search: bool):
    profile = SimpleNamespace(backend="bedrock", default_model=None, web_search_enabled=web_search,
                              region=None, account_role_arn=None)
    st = SimpleNamespace()
    st.provider_registry = MagicMock()
    st.cost_recorder = MagicMock()
    st.cost_recorder.finalize = AsyncMock()
    st.routing_profile_loader = MagicMock()
    st.routing_profile_loader.load = AsyncMock(return_value=profile)
    st.agentcore_mcp_client = MagicMock()
    st.body_logger = None
    st.circuit_breaker = None
    st.fallback_resolver = None
    st.alias_provider_map = {}
    st.tokenizer = None
    return st


async def _call_messages(monkeypatch, redis, *, client, web_search=True, policy=None):
    import app.routers.messages as messages_mod
    import app.services.web_search_loop as wsl_mod
    from app.services.router_service import RouterService

    class _Router(RouterService):
        async def alias_provider(self, redis, db, alias):
            return ProviderType.BEDROCK

        async def resolve_bedrock_model(self, redis, db, alias):
            return _model_config()

    monkeypatch.setattr(messages_mod, "_router_service", _Router())
    loop_calls: list = []

    async def _fake_loop(**kwargs):
        loop_calls.append(kwargs)
        return MagicMock(status_code=200)

    monkeypatch.setattr(wsl_mod, "run_web_search_loop", _fake_loop)
    admissions: list = []

    async def _fake_rl(**kwargs):
        admissions.append(kwargs)
        return None

    monkeypatch.setattr("app.services.fallback_loop.enforce_rate_limits", _fake_rl)

    async def _fallback(**kwargs):
        raise _ReachedFallback(kwargs)

    monkeypatch.setattr(messages_mod, "run_fallback_loop", _fallback)
    _patch_policy(monkeypatch, policy or _policy(5, None))
    state = {"auth_context": _auth(), "client": client, "_redis": redis,
             "_session_factory": None, "request_id": "req-1"}
    body = {"model": "claude-opus", "max_tokens": 16, "system": "You are helpful.",
            "messages": [{"role": "user", "content": "news today?"}]}
    req = _FakeRequest(body, state, _messages_app_state(web_search))
    try:
        resp = await messages_mod.messages(req)
        return resp, None, loop_calls, state, admissions
    except _ReachedFallback as reached:
        return None, reached.kwargs, loop_calls, state, admissions


@pytest.mark.parametrize("client", ["claude-code", "cowork"])
async def test_messages_soft_block_when_monthly_limit_reached(monkeypatch, redis, client):
    await redis.set(q.user_key(USER, PERIOD), 5)
    _resp, fb, loop_calls, state, _adm = await _call_messages(monkeypatch, redis, client=client)
    assert loop_calls == [], "도구를 주입하는 웹 검색 루프가 돌았다"
    assert fb is not None, "모델 호출(폴백 경로)이 진행되지 않았다"
    assert fb["req_data"]["system"] == "You are helpful.\n\n" + q.MONTHLY_LIMIT_SYSTEM_NOTE
    assert not any(t.get("name") == "web_search" for t in fb["req_data"].get("tools") or [])
    assert state["web_search_notice"] == "monthly_limit_reached"


async def test_messages_runs_loop_with_guard_when_under_limit(monkeypatch, redis):
    await redis.set(q.user_key(USER, PERIOD), 2)
    resp, fb, loop_calls, state, adm = await _call_messages(
        monkeypatch, redis, client="claude-code", policy=_policy(5, None, cap=4))
    assert fb is None and len(loop_calls) == 1 and resp.status_code == 200
    kw = loop_calls[0]
    assert isinstance(kw["mcp_client"], q.QuotaGuard)
    assert kw["mcp_client"].per_request_cap == 4
    assert kw["max_iterations"] == 2          # ceil(4 / 3 per turn)
    assert "web_search_notice" not in state
    assert len(adm) == 1                       # 입장 심사(예산·레이트리밋)는 그대로


async def test_messages_app_off_precedes_quota(monkeypatch, redis):
    await redis.set(q.user_key(USER, PERIOD), 999)
    _resp, fb, loop_calls, state, _adm = await _call_messages(
        monkeypatch, redis, client="claude-code", web_search=False, policy=_policy(0, 0))
    assert fb is not None and loop_calls == []
    assert fb["req_data"]["system"] == "You are helpful."   # 노트 없음
    assert "web_search_notice" not in state


async def test_messages_soft_block_keeps_budget_admission(monkeypatch, redis):
    """월 한도 소진이어도 모델 호출의 입장 심사·예산 경로는 바뀌지 않는다 — 폴백 루프에
    budget_status 가 그대로 넘어가고, 한도 판정은 예산 키를 건드리지 않는다."""
    await redis.set(q.user_key(USER, PERIOD), 5)
    _resp, fb, _loop, _state, _adm = await _call_messages(monkeypatch, redis, client="claude-code")
    assert "budget_status" in fb and fb["auth_context"].user_id == USER
    assert all(k.startswith("wsq:") for k in await redis.keys("*"))


async def test_responses_soft_block_for_codex(monkeypatch, redis):
    from app.routers.openai_compat import _handle_responses
    from tests.unit.test_responses_model_selection import _build

    request, adapter, _calls, resolve = _build("codex-gpt", web_search=True)
    monkeypatch.setattr("app.routers.openai_compat._router_service.resolve_codex_model", resolve)
    loop_calls: list = []

    async def _fake_loop(**kwargs):
        loop_calls.append(kwargs)
        return MagicMock(status_code=200)

    monkeypatch.setattr("app.services.web_search_loop.run_web_search_loop", _fake_loop)
    rl_calls: list = []

    async def _fake_rl(**kwargs):
        rl_calls.append(kwargs)
        return None

    monkeypatch.setattr("app.services.rate_limit_enforcement.enforce_rate_limits", _fake_rl)
    _patch_policy(monkeypatch, _policy(None, 10))
    await redis.set(q.team_key(TEAM, PERIOD), 10)   # 팀 풀 소진
    state = request.scope["state"]
    state["auth_context"] = _auth()
    state["_redis"] = redis

    resp = await _handle_responses(request)

    assert resp.status_code == 200, "모델 호출이 막혔다"
    assert loop_calls == []
    sent = json.loads(adapter.invoke.await_args.args[0])
    assert sent["instructions"].endswith(q.MONTHLY_LIMIT_SYSTEM_NOTE)
    assert not any(t.get("name") == "web_search" for t in sent.get("tools") or [])
    assert state["web_search_notice"] == "monthly_limit_reached"
    assert len(rl_calls) == 1


async def test_responses_runs_loop_with_guard_when_under_limit(monkeypatch, redis):
    from app.routers.openai_compat import _handle_responses
    from tests.unit.test_responses_model_selection import _build

    request, _adapter, _calls, resolve = _build("codex-gpt", web_search=True)
    monkeypatch.setattr("app.routers.openai_compat._router_service.resolve_codex_model", resolve)
    loop_calls: list = []

    async def _fake_loop(**kwargs):
        loop_calls.append(kwargs)
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True})

    monkeypatch.setattr("app.services.web_search_loop.run_web_search_loop", _fake_loop)

    async def _fake_rl(**kwargs):
        return None

    monkeypatch.setattr("app.services.rate_limit_enforcement.enforce_rate_limits", _fake_rl)
    _patch_policy(monkeypatch, _policy(10, 10, cap=7))
    state = request.scope["state"]
    state["auth_context"] = _auth()
    state["_redis"] = redis

    resp = await _handle_responses(request)
    assert resp.status_code == 200 and len(loop_calls) == 1
    assert isinstance(loop_calls[0]["mcp_client"], q.QuotaGuard)
    assert loop_calls[0]["max_iterations"] == 3      # ceil(7 / 3)
    assert "web_search_notice" not in state
