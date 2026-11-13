# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""routing_profiles 샘플 크로스계정 ARN 제거 — 기본값 빈 값(placeholder 노출)

Revision ID: 0055
Revises: 0054
Create Date: 2026-11-08

0009(cowork)/0022(claude-code)가 예시 ARN 을 실제 값으로 시드해 admin UI 크로스계정
카드에 플레이스홀더 대신 샘플 ARN/External ID 가 채워져 보이는 문제를 고친다.
기본은 in-account(account_role_arn=NULL)여야 하며, 크로스계정 라우팅은 운영자가
실제 역할 ARN 을 명시 입력할 때만 활성화되어야 한다.

샘플 ARN 과 일치하는 행만 비운다(`btrim` 으로 공백 변형까지 포착) — 운영자가 실제
ARN 으로 교체한 경우에는 손대지 않는다(멱등). ARN 이 샘플과 일치하면 external_id
도 무조건 함께 비운다 — 샘플 ARN 에 실제 external_id 를 붙인 조합은 어차피
assume 에 실패하므로 남길 이유가 없다.

`model.model_aliases` 의 모델 레벨 라우팅(account_role_arn 은 프로필보다 우선,
router_service.model_account_route)도 같은 ARN 목록으로 스윕한다 — 시드에 없지만
문서의 샘플 ARN 을 직접 넣은 행이 있으면 동일하게 비운다.

적용 후 Redis 캐시 수렴 상한: `routing_profile:<client>` 5분 + `<key>:stale` 최대
24h(DB 저하 폴백 경로로만 서빙). 즉시 수렴이 필요하면
`redis-cli --scan --pattern 'routing_profile:*' | xargs -r redis-cli DEL`.
"""
from alembic import op
from sqlalchemy import text

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

# 샘플 ARN 과 일치하는 행만 대상 (운영자 설정 실제 ARN 은 보존)
_SAMPLE_ARNS = (
    "arn:aws:iam::333344445555:role/llm-gateway-claude-code-bedrock",  # 0022 claude-code
    "arn:aws:iam::222233334444:role/llm-gateway-cowork-bedrock",  # 0009 cowork
)

# 프로필 단위 + 모델 단위 두 라우팅 표면을 같은 규칙으로 스윕.
_TABLES = ("model.routing_profiles", "model.model_aliases")


def upgrade() -> None:
    for table in _TABLES:
        for arn in _SAMPLE_ARNS:
            op.execute(
                text(
                    f"UPDATE {table} "
                    "   SET account_role_arn = NULL, external_id = NULL, "
                    "       updated_at = now() "
                    " WHERE btrim(account_role_arn) = :arn"
                ).bindparams(arn=arn)
            )


def downgrade() -> None:
    # 샘플 값으로 되돌리지 않는다 — 되돌리면 예시 ARN 이 다시 시드된다.
    pass
