# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""auth.teams(dept_id,name) / auth.departments(org_id,name) UNIQUE 인덱스

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-12

## 왜 필요한가

OIDC/그룹 기반 팀·부서 자동생성은 "조회 → 없으면 INSERT" 의 check-then-act 다.
두 exchange 요청이 같은 신규 팀을 동시에 만들면 둘 다 조회를 빗나가 중복 행이
생긴다 — 이후 ``get_team_by_dept_and_name`` 의 LIMIT 1 은 어느 쪽을 고를지
비결정이라 같은 사용자가 로그인 때마다 다른 팀에 배정될 수 있다.

UNIQUE 인덱스가 있으면 패자의 INSERT 가 승자 트랜잭션 커밋까지 **블록**된 뒤
IntegrityError 로 실패한다 — 코드는 savepoint 롤백 + 재조회로 승자 행을 받는다.
(인덱스 없으면 패자 INSERT 도 성공해 중복이 영구화된다 — 그래서 인덱스가 먼저다.)

## 배포 순서

인덱스 생성 전에 기존 중복을 검사한다 — 중복이 있으면 인덱스 생성이 raw
에러로 실패하므로, 어떤 (dept_id|org_id, name) 이 몇 번 중복인지 적은 명확한
예외를 던진다(수동 병합 후 재시도). dev/prod 는 중복 0 확인됨.

CREATE UNIQUE INDEX(비-CONCURRENT) 는 테이블에 SHARE 락 — 쓰기만 잠시 막는다.
teams/departments 는 소형 테이블이라 재작성 없이 수 ms — 배치 창 제약 없음.
"""

from alembic import op
from sqlalchemy import text

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def _assert_no_duplicates(table: str, cols: str) -> None:
    rows = op.get_bind().execute(
        text(
            f"SELECT {cols}, COUNT(*) AS c FROM {table} "
            f"GROUP BY {cols} HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if rows:
        raise RuntimeError(
            f"{table} 에 기존 중복이 있어 UNIQUE 인덱스를 만들 수 없다: {rows}. "
            "수동으로 병합(사용자/정책 FK 를 승자 행으로 재지정 후 패자 삭제)한 뒤 "
            "마이그레이션을 재시도하라."
        )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _assert_no_duplicates("auth.teams", "dept_id, name")
    _assert_no_duplicates("auth.departments", "org_id, name")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_teams_dept_name "
        "ON auth.teams (dept_id, name)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_departments_org_name "
        "ON auth.departments (org_id, name)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auth.uq_teams_dept_name")
    op.execute("DROP INDEX IF EXISTS auth.uq_departments_org_name")
