# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add service-account key support — users.is_service_account, virtual_keys.kind/issued_by/issued_via_service_token

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-03

## 배경

서버사이드 앱(RAG 등)이 사람 세션(SSO 캡)과 무관한 장기 VK 를 필요로 한다.
SSO 캡은 ``key_service.issue_key`` 의 호출자가 ``sso_session_expires_at`` 를
넘길 때만 적용되는 호출자 책임이라, 별도 발급 경로(``POST /admin/keys``)를
만들면 캡 없는 키가 나온다 — 그 경로를 사람 계정에 열어 두면 "관리자가
임의 사용자에게 365일 키" 라는 SSO 캡 우회가 되므로, 장기키 발급 대상을
서비스 계정으로 한정하는 표지가 필요하다.

- ``users.is_service_account`` : 장기(service) 키 발급 대상 표지.
  관리자가 admin-ui 에서 설정하며, 설정과 해제 모두 감사 대상.
- ``virtual_keys.kind`` : 발급 경로 구분 ('exchange' | 'sts' | 'internal' | 'service').
  issue_key 가 kind ↔ is_service_account 조합을 강제한다 — 서비스 계정은
  exchange/sts 경로로 단기키를 받지 못하고, 일반 유저는 service 키를 받지 못한다.
- ``virtual_keys.issued_by`` : 발급자 user_id (audit 외에 대시보드 표시용).
- ``virtual_keys.issued_via_service_token`` : service token 경유 발급 시 토큰 id.
"""
from alembic import op
from sqlalchemy import text

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            "ALTER TABLE auth.users "
            "ADD COLUMN IF NOT EXISTS is_service_account BOOLEAN "
            "NOT NULL DEFAULT false"
        )
    )
    bind.execute(
        text(
            "ALTER TABLE auth.virtual_keys "
            "ADD COLUMN IF NOT EXISTS kind VARCHAR(32) "
            "NOT NULL DEFAULT 'exchange'"
        )
    )
    bind.execute(
        text(
            "ALTER TABLE auth.virtual_keys "
            "ADD COLUMN IF NOT EXISTS issued_by UUID REFERENCES auth.users(id)"
        )
    )
    bind.execute(
        text(
            "ALTER TABLE auth.virtual_keys "
            "ADD COLUMN IF NOT EXISTS issued_via_service_token UUID "
            "REFERENCES auth.service_tokens(id)"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    for stmt in (
        "ALTER TABLE auth.virtual_keys DROP COLUMN IF EXISTS issued_via_service_token",
        "ALTER TABLE auth.virtual_keys DROP COLUMN IF EXISTS issued_by",
        "ALTER TABLE auth.virtual_keys DROP COLUMN IF EXISTS kind",
        "ALTER TABLE auth.users DROP COLUMN IF EXISTS is_service_account",
    ):
        bind.execute(text(stmt))
