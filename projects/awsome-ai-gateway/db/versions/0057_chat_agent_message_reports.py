# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""chat_agent.messages.reports — 리포트 카드 영속화

Revision ID: 0057
Revises: 0056
Create Date: 2026-11-17

report 이벤트(다운로드 리포트 카드)는 스트림 중 acc_reports 에만 모이고 DB 에
저장되지 않았다 — 새로고침하면 카드가 사라지고, /reports/download 도
URI→세션 소유권을 검증할 근거가 없었다(버킷+prefix 화이트리스트만).

tool_calls/charts/validator 와 같은 패턴의 jsonb 컬럼을 추가해:
  - history 복원 시 리포트 카드가 다시 렌더된다
  - /reports/download 가 reports @> [{s3_uri:...}] 로 소유 세션을 검증할 수 있다
"""
from alembic import op
from sqlalchemy import text

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE chat_agent.messages "
            "ADD COLUMN IF NOT EXISTS reports jsonb"
        )
    )


def downgrade() -> None:
    op.execute(text("ALTER TABLE chat_agent.messages DROP COLUMN IF EXISTS reports"))
