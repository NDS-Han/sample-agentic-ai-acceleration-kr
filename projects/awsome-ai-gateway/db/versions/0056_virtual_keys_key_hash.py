# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""virtual_keys.key_hash — 폐기/로테이션의 복호화 의존 제거

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-13

라이프사이클 경로(revoke/rotate/repair/offboard)가 전부 `decrypt → sha256(raw)`
으로 Redis 키 `key:vk:{hash}` 를 구한다. VIRTUAL_KEY_ENCRYPTION_KEY 로테이션 후엔
복호화가 실패해 **폐기 자체가 불가능**해진다 — DB 상태는 REVOKED 인데 Redis 엔트리가
TTL(최대 365일) 동안 유효하게 남는다.

`key_hash = sha256(raw_key)` 를 발급 시 함께 저장하면 해시 기반 수명주기가
복호화 없이 동작한다. sha256(vk-64hex) 는 256bit 엔트로피라 역해 불가 — 저장 안전.
기존 행은 마이그레이션에서 백필할 수 없으므로(복호화 필요) 라이프사이클 터치 시
지연 백필한다.
"""
from alembic import op
from sqlalchemy import text

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE auth.virtual_keys "
            "ADD COLUMN IF NOT EXISTS key_hash VARCHAR(64)"
        )
    )


def downgrade() -> None:
    op.execute(text("ALTER TABLE auth.virtual_keys DROP COLUMN IF EXISTS key_hash"))
