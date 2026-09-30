# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""팀 기본 유저 cap D: budget.budget_configs.default_user_cap_usd

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-30

## 무엇을 추가하나

docs/us-llm-gateway/budget-rules.md (v6.1) §3-2 — 팀 예산 행(scope=TEAM,
client IS NULL)에 ``default_user_cap_usd`` 를 추가한다. 개인 예산(A_u)이 없는
팀 멤버에게 적용되는 기본 cap 이다:

    cap_u = A_u ?? D

    D = NULL  → 미설정 유저는 팀 한도 T 만 적용 (현행 pass-through, 선착순)
    D = 0     → 미설정 유저 차단
    D > 0     → 미설정 유저 개인 상한

컬럼은 TEAM scope 행에서만 의미를 가진다. USER/app 행에서 값이 들어 있어도
소비자(gateway-proxy team config 캐시, admin-api 집계)는 TEAM 행만 읽는다.

## 되돌리기

컬럼을 떨어뜨리면 D 가 함께 사라진다 — 미설정 유저는 다시 T 만 적용받는다
(제한이 느슨해지는 방향).
"""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: 손으로 만들어 둔 환경에서도 멱등.
    op.execute(
        """
        ALTER TABLE budget.budget_configs
            ADD COLUMN IF NOT EXISTS default_user_cap_usd NUMERIC(12,4)
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN budget.budget_configs.default_user_cap_usd IS
            'TEAM scope 행의 기본 유저 cap D — 개인 예산 미설정 멤버에 적용. NULL=팀 한도만, 0=차단'
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE budget.budget_configs DROP COLUMN IF EXISTS default_user_cap_usd"
    )
