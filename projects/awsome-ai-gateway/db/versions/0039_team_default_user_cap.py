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

## T = NULL 행 (§3-1/G-6)

팀 예산 미설정 상태에서도 D 를 미리 설정할 수 있어야 한다(사전 준비).
``max_budget_usd`` 의 NOT NULL 을 풀어 TEAM scope + client IS NULL 행만
NULL 을 허용한다 — T=NULL 인 행이 곧 "D 만 저장된 팀 config" 다.
T=NULL 과 행 없음은 enforcement 에서 동일하게 ``team_budget_unset`` 으로 판정.

## 되돌리기

컬럼을 떨어뜨리면 D 가 함께 사라진다 — 미설정 유저는 다시 T 만 적용받는다
(제한이 느슨해지는 방향). max_budget_usd NULL 행이 있으면 NOT NULL 복원이
실패하므로, downgrade 전에 해당 행의 T 를 채우거나 제거해야 한다.
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

    # T=NULL 행 허용 — TEAM scope + client IS NULL 한정. USER/app 행의
    # max_budget_usd 는 여전히 NOT NULL (CHECK 로 강제).
    op.execute(
        """
        ALTER TABLE budget.budget_configs
            ALTER COLUMN max_budget_usd DROP NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE budget.budget_configs
            ADD CONSTRAINT ck_budget_configs_max_required
            CHECK (
                max_budget_usd IS NOT NULL
                OR (scope::text = 'TEAM' AND client IS NULL)
            )
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE budget.budget_configs DROP CONSTRAINT IF EXISTS ck_budget_configs_max_required"
    )
    # T=NULL 행이 남아 있으면 NOT NULL 복원이 실패한다 — 그 경우 운영자가
    # 먼저 행을 정리해야 한다는 게 이 downgrade 의 명시적 전제다.
    op.execute(
        """
        UPDATE budget.budget_configs
            SET max_budget_usd = 0
            WHERE max_budget_usd IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE budget.budget_configs
            ALTER COLUMN max_budget_usd SET NOT NULL
        """
    )
    op.execute(
        "ALTER TABLE budget.budget_configs DROP COLUMN IF EXISTS default_user_cap_usd"
    )
