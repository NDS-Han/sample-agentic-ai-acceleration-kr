# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""usage_logs.key_prefix — 요청을 낸 VK 의 prefix 귀속

Revision ID: 0051
Revises: 0050
Create Date: 2026-11-03

## 배경

키 단위 사용량/토큰 분석(서비스 계정 앱 관측성)을 위해 요청을 인증한 VK 의
``key_prefix`` (raw[:11]) 를 사용량 행에 기록한다. gateway 가 인증 시점에
Authorization 헤더의 raw key 에서 직접 계산하므로 Redis auth 계약은
바뀌지 않는다. virtual_keys.key_prefix 와 조인 가능한 공개 식별자이며
시크릿의 32bit 접두일 뿐이라 노출 범위는 기존 관리자 화면과 동일하다.

- 백필 없음 — 배포 이전 행은 NULL. API 는 attribution_since 를 함께 내려준다.
- 인덱스 없음 — 키별 조회는 (user_id, requested_at) 범위 인덱스 안에서
  prefix 필터로 충분하다. 전역 prefix 검색이 필요해지면 별도 마이그레이션.
"""
from alembic import op
from sqlalchemy import text

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            "ALTER TABLE usage.usage_logs "
            "ADD COLUMN IF NOT EXISTS key_prefix VARCHAR(16)"
        )
    )
    # 서비스 계정 한정 결정적 백필 — 이 계정들은 exchange/sts 경로가 막혀 있어
    # 모든 사용이 VK 인증이고, 발급 시 구 키가 즉시 만료돼 한 유저의 키 수명
    # 구간이 겹치지 않는다. 따라서 requested_at 이 어느 키의 수명 안에 있는지로
    # 귀속이 유일하게 결정된다. 일반 유저는 JWT 행과 섞일 수 있어 대상에서
    # 제외한다. NULL 만 채우는 idempotent UPDATE 다.
    bind.execute(
        text(
            "UPDATE usage.usage_logs u SET key_prefix = vk.key_prefix "
            "FROM auth.virtual_keys vk "
            "WHERE u.user_id = vk.user_id AND u.key_prefix IS NULL "
            "AND vk.kind = 'service' "
            "AND vk.user_id IN (SELECT id FROM auth.users WHERE is_service_account) "
            "AND u.requested_at >= vk.issued_at "
            "AND u.requested_at < COALESCE(vk.revoked_at, vk.expires_at)"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text("ALTER TABLE usage.usage_logs DROP COLUMN IF EXISTS key_prefix")
    )
