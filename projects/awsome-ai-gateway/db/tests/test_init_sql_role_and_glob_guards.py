# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""db/ 정적 가드 3종 — 실 DB 없이 돌아간다(CI 에 넣을 수 있는 종류).

1. **공개된 비밀번호로 LOGIN 가능한 롤 금지.**
   이 디렉터리의 SQL 은 로컬 docker-compose 뿐 아니라 **prod 를 포함한 모든 환경의
   migration Job 에서 매 helm install/upgrade 마다** 실행된다
   (``db/run_migration.sh`` 의 ``for f in /app/init/*.sql``,
   ``deployment/docs/eks-fargate/03-secrets.md``). 따라서
   ``CREATE ROLE x WITH LOGIN PASSWORD '<리터럴>'`` 은 곧 "git 에 공개된 비밀번호로
   로그인 가능한 롤을 prod Aurora 에 만든다" 는 뜻이다.
   실제로 그랬다: ``proxy_user`` / ``admin_api_user`` / ``notification_worker_user`` 가
   ``*_password_change_me`` 로 만들어졌고, ``admin_api_user`` 는 auth(=virtual_keys)·
   budget·model 에 full CRUD 를 갖는다. 파일 주석은 "환경변수로 override 하라"고 했지만
   ``.sql`` 에는 환경변수 치환 메커니즘이 없어 override 는 애초에 불가능했다.
   허용되는 형태는 두 가지뿐 — ``NOLOGIN`` 이거나, ``format(... %L, gen_random_uuid()…)``
   처럼 **런타임 랜덤**을 넣는 것(``08_create_chat_reader.sql`` 방식).

2. **runner 가 파일을 하나씩 열거하지 않는지.**
   과거 실사고: ``08_create_chat_reader.sql`` 이 열거 목록에 없어 cloud 모드에서 한 번도
   실행되지 않았고, prod 에 ``gateway_chat_reader`` 롤이 없었다. 와일드카드여야
   로컬(postgres entrypoint)과 cloud(run_migration.sh)의 동작이 일치한다.

3. **compose 의 postgres 이미지가 pgvector 를 포함하는지.**
   stock ``postgres:16-alpine`` 로는 ``alembic upgrade head`` 가 실패한다
   (0005 가 ``CREATE EXTENSION vector`` 를 실행). 즉 compose 로는 스키마를 head 까지
   올릴 수 없었고, real-Postgres 증명 테스트를 compose 로 풀어주려는 계획이 성립하지
   않았다. 실측 근거는 해당 테스트 docstring 에 있다.

⚠️ 이 파일의 경로는 ``db/run_migration.sh`` 주석이 가리키는 재발방지 테스트 위치다.
   옮기면 그 주석도 같이 고칠 것.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DB_DIR = Path(__file__).resolve().parent.parent
INIT_DIR = DB_DIR / "init"
RUNNER = DB_DIR / "run_migration.sh"

# 리터럴 비밀번호로 LOGIN 하는 롤 생성/변경.
#   잡아야 함: CREATE ROLE x WITH LOGIN PASSWORD 'literal'
#              ALTER  ROLE x WITH LOGIN PASSWORD 'literal'
#   통과해야 함: ... WITH NOLOGIN
#                format('CREATE ROLE x LOGIN PASSWORD %L ...', gen_random_uuid()::text)
#                ALTER ROLE x WITH NOLOGIN PASSWORD NULL
_LITERAL_LOGIN_PASSWORD = re.compile(
    r"\b(?:CREATE|ALTER)\s+ROLE\b[^;']*?(?<!NO)\bLOGIN\b[^;']*?\bPASSWORD\s*'",
    re.IGNORECASE | re.DOTALL,
)


def _sql_files() -> list[Path]:
    return sorted(INIT_DIR.glob("*.sql"))


# ──────────────────────────────────────────────────────────────────────────────
# 0) 공허성 대조군 — 정규식과 파일 수집이 실제로 동작하는가
# ──────────────────────────────────────────────────────────────────────────────


def test_the_scanner_finds_the_init_sql_files():
    """파일을 못 찾으면 아래 가드 전부가 공허해진다."""
    files = _sql_files()
    assert len(files) >= 8, f"init SQL 을 {len(files)}개만 찾았다 — 경로가 바뀌었나: {INIT_DIR}"
    assert (INIT_DIR / "04_create_users.sql") in files


def test_the_pattern_actually_matches_the_old_defect():
    """대조군 — 옛 형태를 못 잡는 정규식이면 가드가 공허하다."""
    old = "CREATE ROLE proxy_user WITH LOGIN PASSWORD 'proxy_password_change_me';"
    assert _LITERAL_LOGIN_PASSWORD.search(old), "옛 결함 형태를 정규식이 잡지 못한다"


def test_the_pattern_does_not_flag_the_safe_forms():
    """대조군 — 안전한 형태를 잡으면 거짓양성으로 가드를 못 쓴다."""
    safe = [
        "CREATE ROLE proxy_user WITH NOLOGIN;",
        "ALTER ROLE proxy_user WITH NOLOGIN PASSWORD NULL;",
        # 08_create_chat_reader.sql 방식 — 런타임 랜덤값
        "EXECUTE format('CREATE ROLE r LOGIN PASSWORD %L NOINHERIT',"
        " gen_random_uuid()::text || gen_random_uuid()::text);",
    ]
    for stmt in safe:
        assert not _LITERAL_LOGIN_PASSWORD.search(stmt), f"거짓양성: {stmt!r}"


# ──────────────────────────────────────────────────────────────────────────────
# 1) 가드 — 공개된 비밀번호로 LOGIN 가능한 롤 금지
# ──────────────────────────────────────────────────────────────────────────────


def test_no_init_sql_creates_a_login_role_with_a_literal_password():
    offenders: list[str] = []
    for path in _sql_files():
        sql = path.read_text(encoding="utf-8")
        # `--` 주석 줄은 제외한다(설명문에 옛 형태를 인용하고 있다).
        body = "\n".join(
            line for line in sql.splitlines() if not line.lstrip().startswith("--")
        )
        for match in _LITERAL_LOGIN_PASSWORD.finditer(body):
            line_no = body[: match.start()].count("\n") + 1
            offenders.append(f"{path.name}:~{line_no} {match.group(0)[:70]!r}")

    assert offenders == [], (
        "리터럴 비밀번호로 LOGIN 가능한 롤을 만든다 — 이 SQL 은 prod migration Job 에서도 "
        "실행되므로 곧 '공개된 비밀번호로 prod 로그인 가능'이다. NOLOGIN 을 쓰거나 "
        "gen_random_uuid() 런타임 랜덤을 쓸 것(08_create_chat_reader.sql 참고):\n  "
        + "\n  ".join(offenders)
    )


def test_the_three_service_roles_are_nologin():
    """의도 고정 — 세 롤은 권한 컨테이너일 뿐 인증 주체가 아니다."""
    sql = (INIT_DIR / "04_create_users.sql").read_text(encoding="utf-8")
    for role in ("proxy_user", "admin_api_user", "notification_worker_user"):
        assert re.search(rf"CREATE ROLE {role} WITH NOLOGIN\b", sql), (
            f"{role} 이 NOLOGIN 으로 생성되지 않는다"
        )
        # ⚠️ ALTER 분기가 없으면 **이미 배포된 dev·prod 의 롤은 옛 공개 비밀번호를 유지한다**
        #    (롤이 존재하므로 CREATE 가 스킵된다). 회수 경로가 반드시 있어야 한다.
        assert re.search(rf"ALTER ROLE {role} WITH NOLOGIN PASSWORD NULL", sql), (
            f"{role}: 이미 존재하는 롤의 옛 비밀번호를 회수하는 ALTER 분기가 없다 — "
            f"이미 배포된 환경은 그대로 공개 비번을 유지한다"
        )


def test_no_service_role_dsn_remains_in_the_tree():
    """NOLOGIN 으로 잠근 롤을 접속 문자열에서 계속 쓰면 런타임 인증 실패가 된다."""
    repo = DB_DIR.parent
    candidates = [
        repo / "docker-compose.yml",
        repo / "notification-worker" / "src" / "worker" / "config.py",
        repo / "notification-worker" / "tests" / "integration" / "conftest.py",
    ]
    present = [p for p in candidates if p.exists()]
    # 대조군 — 파일이 다 사라졌으면 아래 루프가 공허하다.
    assert len(present) == len(candidates), f"검사 대상 파일이 없다: {candidates}"

    offenders: list[str] = []
    for path in present:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith(("#", "--")):
                continue  # 주석의 설명문은 허용
            if re.search(r"(?:proxy_user|admin_api_user|notification_worker_user):", line):
                offenders.append(f"{path.relative_to(repo)}:{i}")

    assert offenders == [], (
        "NOLOGIN 롤을 DSN 에 쓰고 있다 — 'gateway' 유저를 쓸 것"
        "(배포도 동일: values-eks-fargate-prod.yaml `notificationWorkerUser: \"gateway\"`):\n  "
        + "\n  ".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2) 가드 — runner 는 와일드카드로 전체를 돌려야 한다
# ──────────────────────────────────────────────────────────────────────────────


def test_the_runner_globs_init_sql_instead_of_listing_files():
    """실사고 재발방지: 열거 목록은 새 파일을 조용히 빠뜨린다."""
    script = RUNNER.read_text(encoding="utf-8")
    assert "for f in /app/init/*.sql" in script, (
        "run_migration.sh 가 init SQL 을 와일드카드로 순회하지 않는다 — 파일을 하나씩 "
        "열거하면 새로 추가된 SQL 이 cloud 모드에서 조용히 누락된다"
        "(08_create_chat_reader.sql 실사고)"
    )
    # 열거 흔적(개별 파일명을 psql -f 로 직접 지정)이 남아있지 않은지.
    for path in _sql_files():
        assert f"-f /app/init/{path.name}" not in script, (
            f"{path.name} 을 개별 열거하고 있다 — 와일드카드 순회만 남길 것"
        )


def test_the_runner_comment_points_at_this_file():
    """주석이 가리키는 테스트 경로가 실제로 존재해야 한다(예전엔 없는 경로였다)."""
    script = RUNNER.read_text(encoding="utf-8")
    match = re.search(r"db/tests/(\S+\.py)", script)
    assert match, "run_migration.sh 에 재발방지 테스트 경로 주석이 없다"
    referenced = DB_DIR / "tests" / match.group(1)
    assert referenced.exists(), (
        f"run_migration.sh 가 존재하지 않는 테스트를 가리킨다: db/tests/{match.group(1)} "
        f"(실제 파일: {Path(__file__).name})"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3) 가드 — compose 의 postgres 이미지는 pgvector 를 포함해야 한다
# ──────────────────────────────────────────────────────────────────────────────


def test_compose_postgres_image_ships_pgvector():
    """stock ``postgres:*-alpine`` 로는 ``alembic upgrade head`` 가 반드시 실패한다.

    ``db/versions/0005_add_chat_agent_schema.py`` 가
    ``CREATE EXTENSION IF NOT EXISTS vector`` 를 실행하기 때문이다
    (``chat_agent.schema_embeddings`` 의 ``vector(1024)`` 컬럼용).

    실측(2026-09-11, 형제 컨테이너 + 공유 네트워크):
      * ``postgres:16-alpine``      → ``asyncpg.exceptions.FeatureNotSupportedError:
        extension "vector" is not available`` (head 도달 실패)
      * ``pgvector/pgvector:pg16``  → ``alembic_version=0033``, ``vector 0.8.6`` 활성

    즉 예전 compose 는 스키마를 head 까지 올릴 수 없었고, real-Postgres 를 요구하는
    증명 테스트들을 compose 로 풀어주려는 계획 자체가 성립하지 않았다.
    """
    compose = (DB_DIR.parent / "docker-compose.yml").read_text(encoding="utf-8")

    # postgres 서비스의 image: 줄을 찾는다(주석 제외).
    lines = [line for line in compose.splitlines() if not line.lstrip().startswith("#")]
    images = [
        line.split("image:", 1)[1].strip()
        for line in lines
        if "image:" in line and ("postgres" in line or "pgvector" in line)
    ]
    # 대조군 — 이미지 줄을 못 찾으면 아래 단정이 공허하다.
    assert images, "compose 에서 postgres 이미지 줄을 찾지 못했다(서비스명/형식이 바뀌었나)"

    for image in images:
        assert "pgvector" in image, (
            f"compose 의 postgres 이미지가 {image!r} 다 — pgvector 확장이 없으면 "
            f"migration 0005 의 `CREATE EXTENSION vector` 가 실패해 alembic head 에 "
            f"도달할 수 없다. `pgvector/pgvector:pg16` 을 쓸 것."
        )


def test_the_migration_that_needs_pgvector_still_does():
    """대조군 — 0005 가 vector 확장을 실제로 요구하는지.

    이 요구가 사라졌다면 위 가드는 근거를 잃는다(이미지를 되돌려도 된다).
    반대로 다른 migration 이 새 확장을 요구하기 시작하면 여기서 드러난다.
    """
    versions = DB_DIR / "versions"
    needing = sorted(
        p.name
        for p in versions.glob("*.py")
        if "CREATE EXTENSION IF NOT EXISTS vector" in p.read_text(encoding="utf-8")
    )
    assert needing == ["0005_add_chat_agent_schema.py"], (
        f"vector 확장을 요구하는 migration 집합이 바뀌었다: {needing} — "
        f"compose 이미지 가드의 근거를 재확인할 것"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 4) 가드 — 의존성 lock 파일은 반드시 git 에 tracked 여야 한다
# ──────────────────────────────────────────────────────────────────────────────


def test_every_service_lockfile_is_tracked_by_git():
    """CI 는 ``uv sync --frozen`` / ``npm ci`` 로 lock 을 그대로 설치한다.

    ⚠️ ``actions/checkout`` 은 **tracked 파일만** 가져온다. 그래서 lock 이 로컬 디스크에만
    있고 커밋되지 않았으면 CI 는 첫 실행부터 "lockfile 없음" 으로 죽는다 — 로컬에서는
    아무 문제가 없어서 알아채기 어렵다. 실제로 ``notification-worker/uv.lock`` 이 그 상태였다
    (gitignore 된 것도 아니고 그냥 커밋 누락, 나머지 5개 서비스는 tracked).

    lock 이 없으면 재현성도 없다 — 같은 커밋이 다른 의존성으로 빌드된다.
    """
    import os
    import subprocess

    repo = DB_DIR.parent
    locks = sorted(
        p for p in repo.glob("*/uv.lock") if ".venv" not in p.parts
    ) + sorted(repo.glob("*/package-lock.json"))

    # 대조군 — lock 을 하나도 못 찾았으면 아래 루프가 공허하다.
    assert len(locks) >= 6, f"lock 파일을 {len(locks)}개만 찾았다: {[str(p) for p in locks]}"

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True
        )

    # ⚠️ "git 이 없으면 skip" 을 그냥 쓰면 CI 에서 git 이 깨진 날 이 가드가 조용히 사라진다.
    #    그래서 두 경우를 구분한다:
    #      * git work tree 가 **아예 아니다** → 검사 자체가 성립하지 않는다(소스 타르볼,
    #        `git archive` 로 뽑은 트리, Docker 빌드 컨텍스트). skip.
    #      * git work tree 인데 명령이 실패했다 → 진짜 이상이다. fail.
    #    그리고 CI 에서는 checkout 이 반드시 git 저장소를 만들므로, work tree 가 아니라는
    #    사실 자체가 실패다(그 경로로 가드를 우회할 수 없게 못박는다).
    inside = _git("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        if os.environ.get("CI"):
            raise AssertionError(
                "CI 인데 git work tree 가 아니다 — actions/checkout 이 저장소를 만들지 "
                f"않았다면 이 가드는 물론이고 `uv sync --frozen` 도 성립하지 않는다: "
                f"{inside.stderr.strip()[:200]}"
            )
        pytest.skip(
            "git work tree 가 아니다(소스 타르볼 / git archive 트리) — tracked 여부를 "
            "물을 수 없다. CI 에서는 이 경로가 실패로 처리된다."
        )

    result = _git("ls-files", "-z", "--", *[str(p.relative_to(repo)) for p in locks])
    # work tree 안에서 실패했다면 조용히 통과시키지 않는다(가드가 공허해진다).
    assert result.returncode == 0, f"git ls-files 실패: {result.stderr[:200]}"
    tracked = {part for part in result.stdout.split("\0") if part}

    untracked = [
        str(p.relative_to(repo)) for p in locks if str(p.relative_to(repo)) not in tracked
    ]
    assert untracked == [], (
        "lock 파일이 git 에 없다 — actions/checkout 은 tracked 파일만 가져오므로 CI 의 "
        "`uv sync --frozen`/`npm ci` 가 lockfile 부재로 실패한다(로컬에서는 정상으로 보인다). "
        "`git add` 할 것:\n  " + "\n  ".join(untracked)
    )
