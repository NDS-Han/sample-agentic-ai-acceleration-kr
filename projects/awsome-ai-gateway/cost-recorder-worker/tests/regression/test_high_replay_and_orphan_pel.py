# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""워커의 두 방향 회계 오류 — 이중청구와 영구 유실.

같은 뿌리에서 반대 방향으로 갈라진다: 소비자는 DB 를 **커밋한 뒤** XACK 한다.

  이중청구  같은 배치를 다시 읽으면 ``usage_logs`` 는 ``ON CONFLICT DO NOTHING`` 으로
            넘어가지만 ``budget_usages.used_usd`` 는 **가산** UPSERT 라 두 번 더해진다.
            $12 배치면 사용자의 월 사용액이 $24 로 영구히 기록된다. 그 값이 하필 게이트웨이가
            Redis degrade 시 읽고 Redis 복구 후 카운터를 되돌리는 진실의 원천이라, $30 한도
            사용자가 실제 지출 $15 에서 남은 달 내내 hard_block 된다.

  영구 유실  consumer 이름이 파드 이름이고, 파드 이름은 롤아웃·축출·OOM 마다 바뀐다.
            ``XREADGROUP id='0'`` 은 Redis 의미상 **자기 이름의 PEL 만** 돌려주므로, 죽은
            파드의 unacked 메시지는 새 파드가 영원히 보지 못한다. ``cost:stream`` 은
            MAXLEN~100_000 으로 트림되므로 결국 원본까지 사라진다 — usage_logs 행도,
            budget_usages 차감도, 일별 카운터도 없다(과소청구, 복구 불가).

두 방어는 **짝**이다: 회수(XAUTOCLAIM)는 살아 있는 형제 replica 의 배치를 겹쳐 가져올 수
있고, 그때 이중청구를 막는 것이 재처리 필터다. 한쪽만 넣으면 다른 쪽이 나빠진다.

⚠️ 이중청구는 **실 PostgreSQL** 로, 고아 회수는 **실 Redis** 로 확인한다. mock 으로는
   "가산되었는지" 와 "다른 consumer 의 PEL 이 보이는지" 를 원리적으로 검증할 수 없다.

실행:
    PROOF_DSN=postgresql+asyncpg://postgres:...@127.0.0.1:55432/gwproof \\
    REDIS_PROOF_URL=redis://127.0.0.1:56379/0 \\
      pytest tests/regression/test_high_replay_and_orphan_pel.py
"""

from __future__ import annotations

import ast
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")

_OWNED_SUFFIX = "_replayproof"
_SRC = Path(__file__).resolve().parents[2] / "src" / "worker"

USER_ID = "11111111-1111-1111-1111-111111111111"
TEAM_ID = "22222222-2222-2222-2222-222222222222"
DEPT_ID = "33333333-3333-3333-3333-333333333333"


def _entry(request_id: str, cost: str = "4.00"):
    from worker.schemas.cost_stream import CostStreamEntry

    return CostStreamEntry(
        request_id=request_id,
        user_id=USER_ID,
        team_id=TEAM_ID,
        dept_id=DEPT_ID,
        model_alias="proof-model",
        provider="BEDROCK",
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal(cost),
        latency_ms=100,
        requested_at="2026-06-01T00:00:00+00:00",
        completed_at="2026-06-01T00:00:01+00:00",
        period="2026-06",
        date="2026-06-01",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. 실 PostgreSQL — 재처리가 예산을 두 번 더하지 않는가
# ─────────────────────────────────────────────────────────────────────────────

_pg_required = pytest.mark.skipif(
    not PROOF_DSN,
    reason=(
        "PROOF_DSN 미설정 — 실 PG 필요. 가산 UPSERT 의 이중 반영은 카운터 값을 봐야만 "
        "드러난다(호출 횟수만 보면 ON CONFLICT 로 넘어간 것과 구별되지 않는다)."
    ),
)


def _split(dsn: str):
    prefix, _, name = dsn.rpartition("/")
    return prefix, name


def _asyncpg(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _make_db() -> str:
    """``PROOF_DSN`` 의 DB 를 **템플릿으로** 복제해 자기 소유 스크래치 DB 를 만든다.

    ⚠️ 자기 소유 DB 를 쓰는 이유: 공용 DB 에서 예산 합계를 검증하면 다른 테스트가 넣은
       행에 따라 결과가 달라져 재현되지 않고, 반대로 이 테스트가 남긴 행이 뒤에 도는
       테스트를 깨뜨린다.

    ⚠️ ``db/init`` 만 적용하지 않는다. 워커의 INSERT 는 마이그레이션이 추가한 컬럼까지
       쓰므로 init-only 스키마에서는 ``UndefinedColumn`` 으로 죽고, 그 실패가 이 파일이
       검증하려는 회계 오류와 구별되지 않는다. ``PROOF_DSN`` 은 CI 에서 alembic head 를
       마친 DB 를 가리키므로 그것을 템플릿으로 복제한다.

    ⚠️ TEMPLATE 복제는 원본에 **다른 접속이 없어야** 한다. 그래서 복제 전에 원본 엔진을
       만들지 않고, admin 접속만으로 CREATE 한다.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, name = _split(PROOF_DSN)
    owned = f"{name}{_OWNED_SUFFIX}"
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp")), owned
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            # 원본의 유휴 접속을 끊어 TEMPLATE 을 사용 가능하게 한다(원본은 스크래치 DB 다).
            await c.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": name},
            )
            await c.execute(text(f'CREATE DATABASE "{owned}" TEMPLATE "{name}"'))
    finally:
        await admin.dispose()
    return f"{prefix}/{owned}"


@pytest.fixture(scope="module")
async def dsn():
    asyncpg = pytest.importorskip("asyncpg")
    if not PROOF_DSN:
        pytest.skip("PROOF_DSN 미설정")
    url = await _make_db()
    conn = await asyncpg.connect(_asyncpg(url))
    try:
        # usage_logs 는 user/team/dept 세 FK 를 모두 요구한다 — 하나라도 없으면
        # IntegrityError 로 per-row 폴백을 타고 행이 스킵되며, 그 실패는 이 파일이
        # 검증하려는 회계 오류와 구별되지 않는다(실측: skipped=1, written=0).
        ORG_ID = "44444444-4444-4444-4444-444444444444"
        await conn.execute(
            "INSERT INTO auth.organizations (id, name) VALUES ($1,'replay-org') "
            "ON CONFLICT (id) DO NOTHING",
            ORG_ID,
        )
        await conn.execute(
            "INSERT INTO auth.departments (id, org_id, name) VALUES ($1,$2,'replay-dept') "
            "ON CONFLICT (id) DO NOTHING",
            DEPT_ID,
            ORG_ID,
        )
        await conn.execute(
            "INSERT INTO auth.teams (id, dept_id, name) VALUES ($1,$2,'replay-team') "
            "ON CONFLICT (id) DO NOTHING",
            TEAM_ID,
            DEPT_ID,
        )
        await conn.execute(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
            "VALUES ($1,$2,'replay@example.invalid','replay','ADMIN','replay-sub') "
            "ON CONFLICT (id) DO NOTHING",
            USER_ID,
            TEAM_ID,
        )
    finally:
        await conn.close()
    yield url

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, name = _split(PROOF_DSN)
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(
                text(f'DROP DATABASE IF EXISTS "{name}{_OWNED_SUFFIX}" WITH (FORCE)')
            )
    finally:
        await admin.dispose()


async def _used_usd(engine, scope: str, scope_id: str) -> Decimal:
    from sqlalchemy import text

    async with engine.connect() as c:
        row = (
            await c.execute(
                text(
                    "SELECT COALESCE(SUM(used_usd),0) FROM budget.budget_usages "
                    "WHERE scope = :s AND scope_id = :i"
                ),
                {"s": scope, "i": scope_id},
            )
        ).scalar_one()
    return Decimal(str(row))


async def _flusher(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from worker.batch_flusher import BatchFlusher

    class _Pipe:
        def __getattr__(self, _n):
            return lambda *a, **k: None

        async def execute(self):
            return None

    class _Redis:
        def pipeline(self, *a, **k):
            return _Pipe()

        async def publish(self, *a, **k):
            return None

    return BatchFlusher(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        redis=_Redis(),
    )


@_pg_required
async def test_replaying_a_batch_does_not_double_the_recorded_spend(dsn):
    """⚠️ 이 파일의 핵심. 같은 배치를 두 번 flush 해도 예산은 한 번만 늘어야 한다.

    소비자는 커밋 후 XACK 하므로 그 사이의 크래시가 정확히 이 상황을 만든다.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        flusher = await _flusher(engine)
        rid = f"replay-{uuid.uuid4()}"
        batch = [_entry(rid, "4.00")]

        await flusher.flush(list(batch))
        after_first = await _used_usd(engine, "USER", USER_ID)
        # ⚠️ 여기서 0 이면 FK 위반으로 per-row 폴백을 타고 행이 스킵된 것이다 — 이중청구
        #    검증의 전제가 깨진 상태이므로 통과시키면 안 된다.
        assert after_first == Decimal("4.00"), (
            f"1회 반영이 {after_first} — 전제가 깨졌다(FK 시드 누락 시 0 이 된다)"
        )

        # 재처리(같은 request_id, 같은 금액)
        await flusher.flush(list(batch))
        after_replay = await _used_usd(engine, "USER", USER_ID)
        assert after_replay == Decimal("4.00"), (
            f"재처리로 사용액이 {after_replay} 가 됐다 — 영구 이중청구다. "
            "budget_usages 는 게이트웨이가 Redis degrade 시 읽는 진실의 원천이라, "
            "이 값이 부풀면 실제 지출의 절반에서 hard_block 된다."
        )
    finally:
        await engine.dispose()


@_pg_required
async def test_the_same_request_id_twice_inside_one_batch_counts_once(dsn):
    """크래시 없이도 재현된다 — 게이트웨이 spool 이 Redis 복구 후 페이로드를 재발행한다."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        flusher = await _flusher(engine)
        rid = f"intra-{uuid.uuid4()}"
        before = await _used_usd(engine, "TEAM", TEAM_ID)
        await flusher.flush([_entry(rid, "3.00"), _entry(rid, "3.00")])
        after = await _used_usd(engine, "TEAM", TEAM_ID)
        assert after - before == Decimal("3.00"), (
            f"배치 내 중복이 {after - before} 로 반영됐다 — 한 번이어야 한다"
        )
    finally:
        await engine.dispose()


@_pg_required
async def test_distinct_requests_still_accumulate(dsn):
    """⚠️ 대조군. 위 단정들이 "두 번째는 항상 무시" 로 통과하는 것이 아님을 보인다.

    이것이 없으면 flush 를 no-op 으로 만들어도 위 두 테스트가 통과한다.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        flusher = await _flusher(engine)
        before = await _used_usd(engine, "USER", USER_ID)
        await flusher.flush([_entry(f"a-{uuid.uuid4()}", "1.00")])
        await flusher.flush([_entry(f"b-{uuid.uuid4()}", "2.00")])
        after = await _used_usd(engine, "USER", USER_ID)
        assert after - before == Decimal("3.00"), (
            f"서로 다른 요청 2건이 {after - before} 로 반영됐다 — 3.00 이어야 한다"
        )
    finally:
        await engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# 2. 실 Redis — 다른 consumer 이름의 PEL 을 회수하는가
# ─────────────────────────────────────────────────────────────────────────────

_redis_required = pytest.mark.skipif(
    not REDIS_PROOF_URL,
    reason=(
        "REDIS_PROOF_URL 미설정 — 실 Redis 필요. 'XREADGROUP id=0 은 자기 PEL 만 본다' 는 "
        "것이 Redis 의 의미론이고, 가짜로는 그 성질 자체가 존재하지 않는다."
    ),
)


class _CapturingFlusher:
    def __init__(self):
        self.seen: list[str] = []

    async def flush(self, entries):
        self.seen.extend(e.request_id for e in entries)


def _settings(stream: str, group: str, consumer: str):
    from worker.config import Settings

    return Settings(
        cost_stream_key=stream,
        cost_stream_group=group,
        cost_stream_consumer=consumer,
        batch_max_size=10,
        xautoclaim_min_idle_ms=0,  # 테스트에서는 즉시 회수
        database_url="postgresql+asyncpg://unused/unused",
        redis_url=REDIS_PROOF_URL,
    )


@_redis_required
async def test_orphaned_pel_from_a_dead_consumer_name_is_invisible_to_backlog():
    """⚠️ 먼저 결함의 전제를 확인한다 — 이게 성립하지 않으면 회수는 불필요하다.

    ``XREADGROUP id='0'`` 은 **자기 이름의** PEL 만 돌려준다. 파드 이름이 consumer
    이름이므로, 롤아웃 후의 새 파드는 죽은 파드의 unacked 메시지를 볼 수 없다.
    """
    import redis.asyncio as aioredis

    from worker.stream_consumer import StreamConsumer

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    stream = f"proof:stream:{uuid.uuid4().hex[:8]}"
    group = "cost-recorder"
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
        payload = {"payload": json.dumps(_entry("orphan-1").model_dump(mode="json"))}
        await r.xadd(stream, payload)

        # 죽은 파드가 읽고 ACK 하지 못한 상태를 만든다.
        dead = StreamConsumer(r, _CapturingFlusher(), _settings(stream, group, "pod-old"))
        await dead._drain_backlog()  # 자기 PEL 은 아직 비어 있다
        got = await r.xreadgroup(
            groupname=group, consumername="pod-old", streams={stream: ">"}, count=10
        )
        assert got, "메시지를 읽지 못했다 — 전제가 깨졌다"

        # 롤아웃: 새 이름의 파드
        new_flusher = _CapturingFlusher()
        fresh = StreamConsumer(r, new_flusher, _settings(stream, group, "pod-new"))
        await fresh._drain_backlog()
        assert new_flusher.seen == [], (
            f"backlog 가 남의 PEL 을 봤다: {new_flusher.seen} — 이 테스트의 전제가 틀렸다"
        )

        # 회수하면 보인다.
        await fresh._reclaim_orphans()
        assert new_flusher.seen == ["orphan-1"], (
            f"고아 메시지를 회수하지 못했다: {new_flusher.seen} — 롤아웃마다 영구 유실된다"
        )

        pending = await r.xpending(stream, group)
        count = pending["pending"] if isinstance(pending, dict) else pending[0]
        assert count == 0, f"회수 후에도 PEL 에 {count}건 남았다 — XACK 이 빠졌다"
    finally:
        await r.delete(stream)
        await r.aclose()


@_redis_required
async def test_reclaim_respects_min_idle_so_it_cannot_steal_a_live_batch():
    """살아 있는 형제 replica 의 진행 중 배치를 빼앗으면 안 된다.

    ⚠️ 겹쳐 회수되는 것 자체를 완전히 막을 수는 없다(idle 판정은 시간 기반이다). 그래서
       재처리 필터가 짝으로 필요하다 — 이 파일 앞부분의 PG 테스트가 그것을 증명한다.
    """
    import redis.asyncio as aioredis

    from worker.stream_consumer import StreamConsumer

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    stream = f"proof:stream:{uuid.uuid4().hex[:8]}"
    group = "cost-recorder"
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
        await r.xadd(stream, {"payload": json.dumps(_entry("live-1").model_dump(mode="json"))})
        await r.xreadgroup(
            groupname=group, consumername="pod-live", streams={stream: ">"}, count=10
        )

        settings = _settings(stream, group, "pod-other")
        settings.xautoclaim_min_idle_ms = 600_000  # 10분 — 방금 읽은 것은 대상 아님
        flusher = _CapturingFlusher()
        await StreamConsumer(r, flusher, settings)._reclaim_orphans()
        assert flusher.seen == [], (
            f"min_idle 을 무시하고 진행 중 배치를 가져왔다: {flusher.seen}"
        )
    finally:
        await r.delete(stream)
        await r.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 구조 — 두 방어가 짝으로 남아 있는지
# ─────────────────────────────────────────────────────────────────────────────


def _tree(rel: str) -> ast.Module:
    src = (_SRC / rel).read_text(encoding="utf-8")
    assert len(src) > 1000, f"{rel} 가 너무 짧다 — 경로 확인"
    return ast.parse(src)


def test_both_flush_paths_filter_replays():
    """⚠️ 배치 경로만 막으면 IntegrityError 한 번으로 폴백 경로가 계속 이중청구한다."""
    tree = _tree("batch_flusher.py")
    for fname in ("flush", "_flush_per_row"):
        fn = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == fname
            ),
            None,
        )
        assert fn is not None, f"{fname} 를 찾지 못했다"
        calls = {
            n.func.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "_filter_replays" in calls, (
            f"{fname} 가 재처리 필터를 부르지 않는다 — 그 경로는 계속 이중청구한다"
        )


def test_the_replay_filter_runs_before_the_additive_upsert():
    """순서가 뒤집히면 필터가 무의미하다 — 이미 더한 뒤에 걸러도 소용이 없다."""
    tree = _tree("batch_flusher.py")
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "flush"
    )

    def lines(name, attr=False):
        out = []
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if attr and isinstance(f, ast.Attribute) and f.attr == name:
                out.append(n.lineno)
            if not attr and isinstance(f, ast.Name) and f.id == name:
                out.append(n.lineno)
        return out

    filt = lines("_filter_replays")
    upsert = lines("_upsert_budget_usages", attr=True)
    assert filt and upsert
    assert min(filt) < min(upsert), f"필터(L{filt})가 UPSERT(L{upsert}) 뒤에 있다"


def test_the_consumer_reclaims_orphans_on_start_and_periodically():
    """기동 시 한 번만 회수하면, 돌고 있는 동안 죽은 형제의 PEL 은 다음 재시작까지 남는다."""
    tree = _tree("stream_consumer.py")
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
    attrs = [
        n.func.attr
        for n in ast.walk(run)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "_reclaim_orphans" in attrs, "기동 시 회수하지 않는다"
    assert "_reclaim_loop" in attrs or "create_task" in attrs, "주기적 회수가 없다"


def test_xautoclaim_response_shapes_are_both_handled():
    """redis-py 는 버전에 따라 2-tuple / 3-tuple 을 돌려준다.

    한쪽만 가정하면 다른 쪽에서 회수가 통째로 죽고, 그 실패는 "고아가 없다" 와 구별되지 않는다.
    """
    from worker.stream_consumer import _parse_xautoclaim

    two = ("5-0", [("1-1", {"payload": "{}"})])
    three = ("5-0", [("1-1", {"payload": "{}"})], ["9-9"])
    assert _parse_xautoclaim(two) == ("5-0", [("1-1", {"payload": "{}"})])
    assert _parse_xautoclaim(three) == ("5-0", [("1-1", {"payload": "{}"})])
    assert _parse_xautoclaim(None) == ("0-0", [])
    assert _parse_xautoclaim([]) == ("0-0", [])
