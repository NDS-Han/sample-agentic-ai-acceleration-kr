# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""budget.web_search_policies — 웹 검색 월 검색 수 한도 + 요청당 상한 (ORG/TEAM/USER)

Revision ID: 0059
Revises: 0058
Create Date: 2026-11-24

## 무엇
AgentCore 웹 검색을 **검색 수**로 통제한다(달러 아님 — 무료분·단가 변경과 무관).
한도를 넘으면 게이트웨이가 그 호출자에게 web_search 도구를 주지 않을 뿐(soft block),
모델 호출은 그대로 간다. 예산·HARD_BLOCK·CPM/CPH 와 완전히 별개다.

## 열의 의미 (NULL = 상속/미설정)
  - ORG  행(scope_id NULL) monthly_query_limit = 사용자 1인 월 기본 한도
  - TEAM 행 monthly_query_limit = 팀 **공용 풀**(팀원 합계) 월 한도
  - USER 행 monthly_query_limit = 그 사용자의 월 한도(ORG 기본값을 덮어씀)
  - per_request_cap: 요청 1건의 검색 수 상한. USER → TEAM → ORG → 게이트웨이 env 기본(5)
  - monthly_query_limit 0 = 그 범위에서 웹 검색 금지(즉시 soft block). per_request_cap 은 1~50

행이 하나도 없으면 무제한 + env 기본 요청당 상한 — 기존 동작과 같다.
"""
from alembic import op
from sqlalchemy import text

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS budget.web_search_policies (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                scope               VARCHAR(8)  NOT NULL,
                scope_id            UUID,
                monthly_query_limit INTEGER,
                per_request_cap     INTEGER,
                updated_by          UUID REFERENCES auth.users(id),
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_wsp_scope CHECK (scope IN ('ORG', 'TEAM', 'USER')),
                CONSTRAINT ck_wsp_scope_id CHECK ((scope = 'ORG') = (scope_id IS NULL)),
                CONSTRAINT ck_wsp_limit CHECK (monthly_query_limit IS NULL OR monthly_query_limit >= 0),
                CONSTRAINT ck_wsp_cap CHECK (per_request_cap IS NULL OR per_request_cap BETWEEN 1 AND 50)
            )
            """
        )
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_search_policies_scope "
            "ON budget.web_search_policies (scope, COALESCE(scope_id, "
            "'00000000-0000-0000-0000-000000000000'::uuid))"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS budget.web_search_policies"))
