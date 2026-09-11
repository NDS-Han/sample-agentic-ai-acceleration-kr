# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import asyncio
import os
import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic migrations need schema modification privileges → use master user
# DB_MASTER_URL takes precedence, fallback to DB_URL for local dev
db_master = os.getenv("DB_MASTER_URL")
db_app = os.getenv("DB_URL")
db_url = db_master or db_app or config.get_main_option("sqlalchemy.url")

# create_async_engine requires postgresql+asyncpg:// format
# DB_MASTER_URL comes from Helm chart as postgresql:// (for psql compatibility)
# Convert it to postgresql+asyncpg:// for SQLAlchemy async engine
if db_url and db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    # Also convert sslmode= (psql format) to ssl= (asyncpg format)
    if "?sslmode=" in db_url:
        db_url = db_url.replace("?sslmode=", "?ssl=")

# DB_MASTER_URL contains $(DB_MASTER_PASSWORD) placeholder that K8s resolves,
# but special chars in the password can break URL parsing for asyncpg.
# If DB_MASTER_PASSWORD env is set, rebuild the URL with properly quoted password.
# Cannot use urlparse here because chars like [ ] in the password break parsing.
if db_url and os.getenv("DB_MASTER_PASSWORD"):
    from urllib.parse import quote
    safe_password = quote(os.getenv("DB_MASTER_PASSWORD"), safe="")
    # Replace password between :// user : password @ host
    db_url = re.sub(r'(://[^:]+:)[^@]+(@)', rf'\g<1>{safe_password}\2', db_url, count=1)

# DEBUG: 어떤 URL 이 쓰이는지만 남긴다 — 자격증명은 절대 찍지 않는다.
# ⚠️ 예전 코드는 `db_url[:40]` 을 찍고 주석에 "mask password" 라고 적어두었지만,
#    실제로는 마스킹이 아니었다. `postgresql+asyncpg://<user>:` 접두사가
#    30~35자(`postgres`=30, prod 의 `postgres_admin`=35)라서 40자 컷은 비밀번호
#    앞 5~10자를 그대로 노출한다. 실측(로컬 PG16, 2026-09-09):
#    `[ENV.PY DEBUG] Using URL starting with: postgresql+asyncpg://postgres:NOT-A-REAL-PW...`
#    (예시의 비번은 대체값이다 — 이 파일에 실제 조각을 남기면 이 수정이 막으려는 유출을
#     주석으로 다시 저지르게 된다. 실측 당시엔 비번 앞 10자가 그대로 찍혔다.)
#    migration Job 은 매 helm install/upgrade 마다 dev·prod 양쪽에서 돌고 stdout 은
#    CloudWatch Logs 에 보존되므로, 그대로 두면 master 비번 앞부분이 로그에 축적된다.
#    userinfo 구간(`://user:pass@`) 을 통째로 지운다.
def _redact_credentials(url: str) -> str:
    """`scheme://user:pass@host/db` → `scheme://<redacted>@host/db`.

    ⚠️ 정규식 `://[^/]*@` 를 쓰면 **비밀번호에 `/` 가 있을 때 아무것도 치환되지 않고
    전체 URL 이 그대로 출력된다** — `[^/]*` 가 `/` 를 넘지 못해 매치 자체가 실패하기
    때문이다. 리댁션 함수가 조용히 no-op 하는 것이 최악이라 문자열 위치로 바꿨다.
    (실측: `postgresql://postgres:pa/ss@host/db` → 치환 0회.)
    현재 배포에서는 도달하지 않는다(앱 유저 비번은 `random_password ... special=false`
    로 영숫자만, master 는 RDS 관리형이라 `/`·`"`·`@` 를 제외한다) — 그래도 이 함수는
    입력을 신뢰하지 않아야 한다.

    `@` 를 **마지막**부터 찾는다: 비밀번호에 인코딩되지 않은 `@` 가 섞여도 authority
    끝까지 지운다. path/query 에만 `@` 가 있는(자격증명 없는) URL 은 호스트까지 함께
    가려질 수 있는데, 그건 진단 정보를 조금 잃는 쪽이라 안전한 방향의 실패다.
    """
    marker = "://"
    i = url.find(marker)
    if i == -1:
        return url
    start = i + len(marker)
    at = url.rfind("@")
    if at < start:
        return url  # 자격증명 없음 — 그대로 둔다
    return url[:start] + "<redacted>@" + url[at + 1 :]


print(f"[ENV.PY DEBUG] DB_MASTER_URL exists: {bool(db_master)}")
print(f"[ENV.PY DEBUG] DB_URL exists: {bool(db_app)}")
if db_url:
    # host/db 는 진단에 필요하므로 남기고 자격증명만 제거.
    print(f"[ENV.PY DEBUG] Using URL: {_redact_credentials(db_url)}")
else:
    print("[ENV.PY DEBUG] ERROR: No database URL found!")


# transaction_per_migration=True commits each migration script independently
# instead of wrapping the whole upgrade in one transaction. Required for the
# enum split (0008 ALTER TYPE ADD VALUE must COMMIT before 0009 INSERTs use the
# new value, else PostgreSQL raises "unsafe use of new value of enum type").
# Safe here because every migration is idempotent (IF NOT EXISTS / ON CONFLICT).
def run_migrations_offline() -> None:
    context.configure(
        url=db_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
