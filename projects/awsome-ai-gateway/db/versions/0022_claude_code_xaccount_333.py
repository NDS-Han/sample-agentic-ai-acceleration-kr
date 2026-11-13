# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""claude-code → 333 cross-account Bedrock native (routing_profiles 업데이트)

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-05

원래 멀티계정 설계: claude-code→333, codex→123, cowork→222 (각기 다른 계정). clean 배포에서
claude-code 가 123 in-account 로 남아 있던 것을 설계대로 333 로 분리.

⚠️ **예시 ARN 은 더 이상 기본 시드하지 않는다** — admin UI 에 샘플 ARN/External ID 가 실제 값처럼
채워져 보이는 문제 때문에 account_role_arn/external_id 는 NULL 로 둔다(0055 참조). 크로스계정
라우팅이 필요하면 운영자가 실제 역할 ARN 을 입력해 활성화한다.

claude-code 는 Bedrock **native**(bedrock-runtime, boto3 invoke_model). gateway-proxy 가 이 row 의
account_role_arn 을 보고 설정된 role 을 AssumeRole → 크로스계정 bedrock-runtime 클라이언트로 호출
(BedrockAccountClientProvider + BedrockAdapter client_resolver). backend='invoke' 유지(Mantle 아님).
region 은 클라이언트 엔드포인트용; claude-code alias 가 global.anthropic.* 면 model-id rewrite 는 no-op.

**즉시 롤백**: downgrade = account_role_arn/external_id NULL → in-account 복귀
(gateway 코드가 account_role_arn NULL 이면 in-account adapter 사용). 무배포 롤백.
적용 후 Redis 캐시 `routing_profile:claude-code` 플러시 — 핫키 TTL 5분 외에
`:stale` 키(TTL 24h, DB 저하 폴백 경로로 서빙)도 있으므로 즉시 수렴은
`routing_profile:*` 패턴 삭제가 안전.
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

def upgrade() -> None:
    # 샘플 ARN 을 시드하지 않는다 — 기본은 in-account(NULL). 크로스계정은 운영자가
    # admin UI/API 에서 실제 ARN+External ID 를 입력해 활성화한다 (0055 참조).
    # ARN 이 이미 설정된 행(운영자 입력)은 건드리지 않는다.
    op.execute(
        """
        UPDATE model.routing_profiles
           SET account_role_arn = NULL,
               region           = 'ap-northeast-2',
               external_id      = NULL
         WHERE client = 'claude-code'
           AND account_role_arn IS NULL
        """
    )


def downgrade() -> None:
    # 즉시 123 in-account 복귀 (account_role_arn NULL → gateway 가 in-account adapter 사용).
    op.execute(
        """
        UPDATE model.routing_profiles
           SET account_role_arn = NULL, external_id = NULL, region = 'ap-northeast-2'
         WHERE client = 'claude-code'
        """
    )
