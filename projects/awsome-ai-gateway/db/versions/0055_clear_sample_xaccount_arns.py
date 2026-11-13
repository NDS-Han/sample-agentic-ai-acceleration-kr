# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""routing_profiles 샘플 크로스계정 ARN 제거 — 기본값 빈 값(placeholder 노출)

Revision ID: 0055
Revises: 0054
Create Date: 2026-11-08

0009(cowork)/0022(claude-code)가 예시 ARN 을 실제 값으로 시드해 admin UI 크로스계정
카드에 플레이스홀더 대신 샘플 ARN/External ID 가 채워져 보이는 문제를 고친다.
기본은 in-account(account_role_arn=NULL)여야 하며, 크로스계정 라우팅은 운영자가
실제 역할 ARN 을 명시 입력할 때만 활성화되어야 한다.

샘플 값과 **정확히 일치하는 행만** 비운다 — 운영자가 실제 ARN 으로 교체한 경우에는
손대지 않는다(멱등). 적용 후 Redis 캐시 `routing_profile:<client>` 는 TTL(5분)
만료로 자연 수렴.
"""
from alembic import op
from sqlalchemy import text

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

# 샘플 ARN 과 정확히 일치하는 행만 대상 (운영자 설정 실제 ARN 은 보존)
_SAMPLE_ARNS = (
    "arn:aws:iam::333344445555:role/llm-gateway-claude-code-bedrock",  # 0022 claude-code
    "arn:aws:iam::222233334444:role/llm-gateway-cowork-bedrock",  # 0009 cowork
)


def upgrade() -> None:
    for arn in _SAMPLE_ARNS:
        op.execute(
            text(
                "UPDATE model.routing_profiles "
                "   SET account_role_arn = NULL, external_id = NULL "
                " WHERE account_role_arn = :arn"
            ).bindparams(arn=arn)
        )


def downgrade() -> None:
    # 샘플 값으로 되돌리지 않는다 — 되돌리면 예시 ARN 이 다시 시드된다.
    pass
