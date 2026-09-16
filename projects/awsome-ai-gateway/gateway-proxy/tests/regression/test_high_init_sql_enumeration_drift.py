# Copyright 2026 Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: db/run_migration.sh 의 init SQL 루프가 db/init 의 모든 파일을 덮어야 한다.

Bug (실사고): 루프가 `01_*.sql` ~ `07_*.sql` 을 손으로 열거하고 있었다. 2026-05-28 에
추가된 `08_create_chat_reader.sql` 은 이 목록에 들어간 적이 없어 **cloud 모드에서 한 번도
실행되지 않았다**. 결과: prod Aurora 에 `gateway_chat_reader` 롤이 부재
(2026-09-09 `pg_roles` 실측 — dev=존재(운영자가 손으로 생성), prod=부재. prod 에
01~07 이 만드는 롤들은 모두 존재하므로 08 만 건너뛴 것이 확정된다).
로컬 docker-compose 는 postgres 의 `/docker-entrypoint-initdb.d` 가 `*.sql` 전체를
실행하므로 로컬에서는 증상이 나타나지 않았다 — 두 runner 의 동작이 갈렸던 것이 본질.

Fix: `for f in /app/init/*.sql`.

이 테스트는 문자열 grep 이 아니라 **셸이 실제로 확장할 글롭을 파싱해서 실제 db/init
디렉터리에 적용**한다. 그래서 누군가 다시 열거 방식으로 되돌리고 새 파일을 추가하면
(= 원래 사고의 재현) 텍스트가 어떻게 생겼든 상관없이 실패한다.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # regression -> tests -> gateway-proxy -> root
RUN_MIGRATION = PROJECT_ROOT / "db" / "run_migration.sh"
INIT_DIR = PROJECT_ROOT / "db" / "init"

# run_migration.sh 안에서 init SQL 을 돌리는 루프. 컨테이너 내 경로가 /app/init 이다.
_LOOP_RE = re.compile(r"^\s*for\s+f\s+in\s+(?P<globs>.*?)\s*;\s*do\s*$", re.MULTILINE)
_CONTAINER_INIT_PREFIX = "/app/init/"


def _loop_patterns() -> list[str]:
    """루프에 적힌 글롭들을 db/init 기준 파일명 패턴으로 변환."""
    content = RUN_MIGRATION.read_text(encoding="utf-8")
    matches = _LOOP_RE.findall(content)
    assert matches, f"{RUN_MIGRATION} 에서 `for f in ...; do` 루프를 찾지 못했다"

    patterns: list[str] = []
    for globs in matches:
        for token in shlex.split(globs):
            if token.startswith(_CONTAINER_INIT_PREFIX):
                patterns.append(token[len(_CONTAINER_INIT_PREFIX) :])
    assert patterns, (
        f"루프에 {_CONTAINER_INIT_PREFIX} 로 시작하는 글롭이 없다 — "
        "경로가 바뀌었다면 이 테스트의 _CONTAINER_INIT_PREFIX 도 함께 고쳐야 한다"
    )
    return patterns


def test_loop_globs_cover_every_init_sql_file():
    """db/init/*.sql 전부가 cloud 모드 루프에 걸려야 한다 (열거 누락 = 조용한 미적용)."""
    all_sql = {p.name for p in INIT_DIR.glob("*.sql")}
    assert all_sql, f"{INIT_DIR} 에 .sql 이 없다 — 경로가 바뀌었는지 확인"

    patterns = _loop_patterns()
    covered = {
        name for name in all_sql if any(fnmatch.fnmatchcase(name, pat) for pat in patterns)
    }

    missing = sorted(all_sql - covered)
    assert not missing, (
        f"db/run_migration.sh 의 루프가 다음 init SQL 을 실행하지 않는다: {missing}. "
        f"루프 글롭={patterns}. 손으로 열거하지 말고 `/app/init/*.sql` 을 쓸 것 — "
        "누락되면 배포는 성공하고 DB 만 조용히 뒤처진다(08_create_chat_reader.sql 실사고)."
    )


def test_init_sql_filenames_keep_two_digit_prefix():
    """글롭 확장은 collation 순서이므로, 두 자리 숫자 접두사가 실행 순서를 보장한다."""
    names = sorted(p.name for p in INIT_DIR.glob("*.sql"))
    bad = [n for n in names if not re.match(r"^\d\d_", n)]
    assert not bad, (
        f"두 자리 숫자 접두사가 없는 init SQL: {bad}. `for f in /app/init/*.sql` 는 "
        "정렬 순서대로 실행하므로 접두사가 없으면 의존 순서가 깨진다."
    )

    prefixes = [n[:2] for n in names]
    dupes = sorted({p for p in prefixes if prefixes.count(p) > 1})
    assert not dupes, (
        f"중복된 순서 접두사: {dupes}. 같은 번호 두 개는 실행 순서가 파일명 나머지에 "
        "좌우되므로 재현 가능한 순서가 아니다."
    )


def test_dockerfile_ships_whole_init_dir():
    """루프가 와일드카드여도 이미지에 파일이 안 들어가면 같은 증상이 난다."""
    dockerfile = (PROJECT_ROOT / "db" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY\s+init/\s+\./init/", dockerfile, re.MULTILINE), (
        "db/Dockerfile 이 init/ 디렉터리 전체를 복사해야 한다 — 파일을 하나씩 COPY 하면 "
        "런타임 글롭이 아무리 넓어도 새 파일이 이미지에 없어서 그대로 누락된다."
    )
