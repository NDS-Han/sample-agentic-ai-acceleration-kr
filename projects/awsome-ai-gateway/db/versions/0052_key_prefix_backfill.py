# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""usage_logs.key_prefix 서비스 계정 백필 — 키 수명 구간으로 NULL 행 귀속

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-03

## 배경

0051 이전에 기록된 usage_logs 는 key_prefix=NULL 이다. 서비스 계정은
exchange/sts 경로가 막혀 있어 모든 호출이 VK 인증이라, requested_at 이 어느
키의 수명 구간 안에 있는지로 귀속이 유일하게 결정된다.

⚠️ 수명 구간은 ``COALESCE(revoked_at, expires_at)`` 로 못 잡는다 — 회전
(``expire_and_create``)은 status 만 'EXPIRED' 로 바꾸고 expires_at 을 원래
최대 TTL 그대로 둬서, 구 키 구간이 새 키 발급 이후까지 늘어나 겹친다.
UPDATE … FROM 은 다중 매칭 시 어느 행을 쓸지 비결정적이다. 그래서 구간 끝을
**같은 유저의 다음 키 issued_at**(kind 무관 — 언플래그 뒤 발급된 키도 경계)
으로 LEAD 절단한다.

일반 유저는 JWT 호출이 섞일 수 있어 대상에서 제외한다. NULL 만 채우는
idempotent UPDATE — 롤링 업그레이드 중 구 게이트웨이가 낸 NULL 행도 같은
마이그레이션을 한 번 더 돌리면 채워진다.
"""
from alembic import op
from sqlalchemy import text

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            "WITH k AS ("
            "  SELECT vk.user_id, vk.key_prefix, vk.kind, vk.issued_at,"
            "         LEAST("
            "           COALESCE(vk.revoked_at, vk.expires_at),"
            "           COALESCE("
            "             LEAD(vk.issued_at) OVER ("
            "               PARTITION BY vk.user_id ORDER BY vk.issued_at"
            "             ),"
            "             'infinity'::timestamptz"
            "           )"
            "         ) AS ended_at"
            "    FROM auth.virtual_keys vk"
            ") "
            "UPDATE usage.usage_logs l SET key_prefix = k.key_prefix "
            "FROM k "
            "WHERE k.kind = 'service' "
            "AND l.user_id = k.user_id "
            "AND l.key_prefix IS NULL "
            "AND l.requested_at >= k.issued_at "
            "AND l.requested_at < k.ended_at"
        )
    )


def downgrade() -> None:
    # 백필 행과 라이브 귀속 행을 구분할 수 없어 되돌리기 불가 — NULL 로 되돌리면
    # 워커가 정상 기록한 신규 행까지 지운다. 컬럼 자체는 0051.downgrade 가 담당.
    pass
