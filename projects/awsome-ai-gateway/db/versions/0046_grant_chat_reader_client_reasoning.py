# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway_chat_reader 에 usage_logs.client / reasoning_tokens 컬럼 GRANT

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-15

## 왜 필요한가 (kiro R17-5 S2)

``config/schema_whitelist.yaml`` 은 ``usage.usage_logs`` 의 ``client`` 와
``reasoning_tokens`` 를 노출하고 SQL Specialist 프롬프트도 그걸로 만들어진다.
그런데 ``db/init/08_create_chat_reader.sql`` 의 컬럼 GRANT 에는 두 컬럼이 없다 —
앱별 질의("claude code vs codex 비용") 가 sqlglot 검증을 통과하고도
``permission denied for column client`` 로 실패한다.

init 스크립트에 그냥 넣을 수 없는 이유: ``run_migration.sh`` 는 ``init/*.sql`` 을
``alembic upgrade head`` **보다 먼저** 실행한다. 신규 DB 에선 두 컬럼이 아직
없다(0007/0019 에서 생성) — ``ON_ERROR_STOP`` 상태에서 존재하지 않는 컬럼에
GRANT 하면 migration Job 이 실패한다. 그래서 alembic 쪽이 올바른 위치다.

role 미존재 환경(채팅 에이전트 미설치)을 위해 role 존재 여부를 확인하고
있을 때만 GRANT 한다.
"""

from alembic import op
from sqlalchemy import text

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

_ROLE = "gateway_chat_reader"
_GRANT = (
    "GRANT SELECT (client, reasoning_tokens) ON usage.usage_logs "
    f"TO {_ROLE}"
)
_REVOKE = (
    "REVOKE SELECT (client, reasoning_tokens) ON usage.usage_logs "
    f"FROM {_ROLE}"
)


def _role_exists() -> bool:
    return bool(
        op.get_bind().execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": _ROLE}
        ).scalar()
    )


def upgrade() -> None:
    if _role_exists():
        op.execute(_GRANT)


def downgrade() -> None:
    if _role_exists():
        op.execute(_REVOKE)
