# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``db/init → alembic upgrade head → init 재적용`` 전체 순서가 실 PG 에서 수렴하는지 검증한다.

왜 이 검사가 필요한가
---------------------
``run_migration.sh`` 는 init SQL 을 **먼저** 돌리고 alembic 을 **나중에** 돌린다.
init SQL 이 스키마를 alembic head 와 같은 모양으로 수렴시키는 백필(``ADD COLUMN``,
``ADD CONSTRAINT``)을 갖는 한, alembic 측 문장은 같은 객체를 다시 만드는 순간
``duplicate_object`` 로 죽는다 — 그리고 ``ON_ERROR_STOP``/트랜잭션 경계상
**migration Job 전체**가 실패한다. 코드 리뷰로는 init 파일과 리비전 파일을
나란히 읽어야만 보이고, init 을 건너뛴 DB 에서 alembic 을 돌리는 검사는 절대
이 경로를 치지 않는다.

실제로 0039 가 그렇게 죽었다: init SQL 의 ``ck_budget_configs_max_required`` 백필
→ alembic 0039 의 plain ``ADD CONSTRAINT`` → 신규 설치본 전부 migration 실패
(7차 리뷰 BLOCKER, 실 PG 재현). 수정은 ``DROP CONSTRAINT IF EXISTS … , ADD
CONSTRAINT`` 멱등 형태.

이 검사가 잡는 것
------------------
1. init SQL 단독 적용이 오류 없이 끝나는가
2. 그 위에서 ``alembic upgrade head`` 가 head 까지 도달하는가 — **init 이 만든
   객체와 이름이 겹치는 리비전 문장이 있으면 여기서 죽는다**
3. head 이후 init SQL **재적용**이 오류 없이 끝나는가 — 배포 재실행 멱등성

실행:
    PROOF_DSN=postgresql+asyncpg://postgres:...@127.0.0.1:55432/gwproof \\
      pytest tests/regression/test_high_init_then_alembic_converges.py

⚠️ ``alembic`` 과 ``asyncpg`` 가 import 가능한 환경에서만 동작한다 — 둘 중 하나라도
   없으면 명시적 이유와 함께 스킵한다(무소음 통과 방지).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")

_PROJECT = Path(__file__).resolve().parents[3]
_INIT_DIR = _PROJECT / "db" / "init"
_DB_DIR = _PROJECT / "db"

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg 미설치 — 실 PG 적용이 필요하다")
pytest.importorskip("alembic", reason="alembic 미설치 — 실 리비전 실행이 필요하다")

pytestmark = pytest.mark.skipif(
    not PROOF_DSN,
    reason=(
        "PROOF_DSN 미설정 — init→alembic 수렴은 실 PG 적용 없이는 알 수 없다 "
        "(duplicate_object·잠금 순서 문제는 파일을 읽어서는 드러나지 않는다)."
    ),
)


def _split(dsn: str) -> tuple[str, str]:
    prefix, _, name = dsn.rpartition("/")
    return prefix, name


def _asyncpg(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _apply_init(url: str) -> list[str]:
    """init/*.sql 전체 적용 — 실패를 수집해 계속 진행(한 번에 전체 실패 목록).

    실 runner(run_migration.sh)는 ``ON_ERROR_STOP=1`` 로 첫 실패에서 멈춘다.
    여기선 모든 실패를 모아 보고하되, 어떤 파일이라도 실패하면 테스트가
    어서 fail-fast 의미론보다 넓은 커버리지다 — 이 검사가 통과하면 runner
    순서대로도 통과한다는 뜻이 된다(실패가 없으니 멈출 지점이 없다).
    """
    failures: list[str] = []
    conn = await asyncpg.connect(_asyncpg(url))
    try:
        for f in sorted(_INIT_DIR.glob("*.sql")):
            try:
                await conn.execute(f.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                # 08_create_chat_reader.sql 은 이름이 정해진 DB(\c gateway)에 접속 —
                # 스크래치 DB 에서는 성립하지 않는다. ORM 대조 테스트와 같은 허용 목록.
                if f.name != "08_create_chat_reader.sql":
                    failures.append(f"{f.name}: {type(e).__name__}: {str(e)[:200]}")
    finally:
        await conn.close()
    return failures


@pytest.fixture(scope="module")
async def converged_db():
    """스크래치 DB 생성 → 테스트 본문이 init 적용 → alembic → 재적용 수행.

    ⚠️ 부작용: ``04_create_users.sql`` 은 클러스터 전역 ``CREATE/ALTER ROLE``
    을 실행한다 — 공유 테스트 인스턴스에선 기존 롤의 NOLOGIN/PASSWORD NULL
    설정이 덮어질 수 있으니 전용 인스턴스에서만 돌린다.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, name = _split(PROOF_DSN)
    owned = f"{name}_initseq"
    # 파괴적 DDL 을 도는 DB 이므로 이름으로 한 번 더 잠근다.
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp")), owned

    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await c.execute(text(f'CREATE DATABASE "{owned}"'))
    finally:
        await admin.dispose()

    url = f"{prefix}/{owned}"
    yield url

    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def test_init_applies_clean(converged_db):
    failures = await _apply_init(converged_db)
    assert failures == [], "init SQL 적용 실패 — 반쪽 스키마로는 아래 검사가 무의미하다:\n" + "\n".join(
        failures
    )


async def test_alembic_reaches_head_after_init(converged_db):
    """init 이 만든 객체(제약/컬럼/인덱스)와 겹치는 리비전 문장이 있으면 여기서 죽는다.

    7차 리뷰 BLOCKER 의 회귀 단정: init 의 ``ck_budget_configs_max_required`` 백필 +
    0039 의 plain ``ADD CONSTRAINT`` → ``duplicate_object`` 로 Job 전체 실패.
    """
    dsn = _asyncpg(converged_db)
    env = {**os.environ, "DB_MASTER_URL": dsn, "DB_URL": dsn}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_DB_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"alembic upgrade head 실패 — init SQL 과 겹치는 객체를 다시 만드는 리비전이 있다:\n"
        f"{proc.stderr[-3000:]}"
    )


async def test_init_reapply_after_head_is_idempotent(converged_db):
    """alembic head 위 init 재적용 — 배포 스크립트는 재실행되므로 멱등이어야 한다."""
    dsn = _asyncpg(converged_db)
    env = {**os.environ, "DB_MASTER_URL": dsn, "DB_URL": dsn}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_DB_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    failures = await _apply_init(converged_db)
    assert failures == [], "head 이후 init 재적용 실패 — 멱등성 회귀:\n" + "\n".join(failures)
