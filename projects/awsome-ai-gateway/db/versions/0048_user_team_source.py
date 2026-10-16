# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add users.team_source — distinguish manual team assignment from sync-derived

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-16

## 배경 (R21 S7)

관리자가 admin-ui 로 사용자를 수동 배정한 팀을 Cognito sync / OIDC 로그인이
그룹-유도 팀으로 되돌려버리고 있었다 — 이관 부수효과(개별 예산·레이트리밋
비활성화)까지 다시 발화한다. 싱크가 배정한 것인지 관리자가 배정한 것인지
구분할 표지가 없어 생긴 문제라 컬럼으로 구분한다.

- 'sync'   : OIDC 로그인 / cognito_sync / stale-team cleanup 이 배정 (기본값,
             기존 행도 전부 이 경로로 생겼다 — 수동 배정 표지가 없었으므로)
- 'manual' : admin-ui 의 PUT /admin/users/{id}/team (transfer_user) 배정

'sync' 인 행만 sync/로그인이 팀을 갱신하고, 'manual' 인 행은 건드리지 않는다
(관리자가 다시 이관하면 그때 갱신). 팀이 사라진 수동 사용자의 정리는 기존처럼
관리자 책임이다.
"""
from alembic import op
from sqlalchemy import text

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        text(
            "ALTER TABLE auth.users "
            "ADD COLUMN IF NOT EXISTS team_source VARCHAR(16) "
            "NOT NULL DEFAULT 'sync'"
        )
    )


def downgrade() -> None:
    op.get_bind().execute(
        text("ALTER TABLE auth.users DROP COLUMN IF EXISTS team_source")
    )
