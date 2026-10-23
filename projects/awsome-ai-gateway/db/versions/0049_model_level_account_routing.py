# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add model-level cross-account routing columns to model.model_aliases

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-23

## 배경

크로스계정 Bedrock 라우팅은 지금까지 `model.routing_profiles`(클라이언트/앱
단위)로만 가능했다 — "어느 계정으로 갈지"가 모델이 아니라 앱에 묶여 있었다.
이 컬럼들은 alias 행에 동일한 세 필드를 둬서 모델 단위 라우팅을 가능하게 한다:

- ``account_role_arn``  이 모델의 호출을 받을 AWS 계정의 AssumeRole 대상
- ``external_id``       confused-deputy 방지용 (대상 계정 trust 조건)
- ``region``            대상 계정에서 호출할 bedrock-runtime 리전

전부 NULL 허용 — NULL 이면 기존 클라이언트 프로필 라우팅이 그대로 적용되므로
배포만으로는 동작 변화가 없다. 해석 우선순위는 **모델 레벨 > 클라이언트
프로필**이다 (gateway messages.py 주석 참조).

필드명은 ``model.routing_profiles`` 와 동일하게 맞춘다 — 같은 의미의 필드를
두 테이블에서 다르게 부르면 SQL 관리자가 헷갈린다.
"""
from alembic import op
from sqlalchemy import text

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            "ALTER TABLE model.model_aliases "
            "ADD COLUMN IF NOT EXISTS account_role_arn VARCHAR(512)"
        )
    )
    bind.execute(
        text(
            "ALTER TABLE model.model_aliases "
            "ADD COLUMN IF NOT EXISTS external_id VARCHAR(256)"
        )
    )
    bind.execute(
        text(
            "ALTER TABLE model.model_aliases "
            "ADD COLUMN IF NOT EXISTS region VARCHAR(64)"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    for col in ("region", "external_id", "account_role_arn"):
        bind.execute(
            text(f"ALTER TABLE model.model_aliases DROP COLUMN IF EXISTS {col}")
        )
