# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""usage.usage_logs.web_search_cost_usd — AgentCore Web Search 비용(모니터링 전용)

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-24

## 무엇
요청 1건이 쓴 AgentCore Web Search 비용을 **기록 시점 단가로** 행에 고정한다.
cost-recorder-worker 가 ``web_search_count × (단가/1000)`` 으로 채우고, 단가는
``public.system_settings['web_search_pricing']``(관리자 편집, 기본 $7.00/1k)에서
읽는다. 단가를 바꿔도 이미 기록된 행은 다시 계산되지 않는다(이력 보존).

## NULL 과 0 은 다르다
  - NULL = 추적 이전(이 기능 배포 전 행) — "무료였다" 가 아니라 "기록 안 됨".
  - 0    = 추적 중, 이 요청은 검색 0회.
그래서 DEFAULT 를 두지 않는다. 백필하지 않는다(과거 행은 NULL 그대로).

## 예산과 무관
이 컬럼은 ``cost_usd`` 와 **별개**다. budget_usages·HARD_BLOCK·CPM/CPH 는
``cost_usd`` 만 읽는다 — 웹 검색 비용은 어떤 한도에도 들어가지 않는다.

## 락
DEFAULT 없는 nullable ADD COLUMN 은 PG11+ 에서 카탈로그만 바뀐다(테이블 재작성
없음). ACCESS EXCLUSIVE 는 잡지만 순간이다. 인덱스는 만들지 않는다 — 집계는
기존 requested_at 인덱스 범위 스캔에서 SUM 으로 읽는다.
"""
from alembic import op
from sqlalchemy import text

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE usage.usage_logs "
            "ADD COLUMN IF NOT EXISTS web_search_cost_usd NUMERIC(14,6)"
        )
    )


def downgrade() -> None:
    # 추적 시작 표지는 함께 지운다 — 컬럼 없이 표지만 남으면 재업그레이드 후 UI 가
    # 그 사이(NULL) 구간을 추적된 것으로 오인한다. 관리자가 정한 단가
    # ('web_search_pricing')는 남긴다 — 다시 올렸을 때 조용히 기본값으로 돌아가면 안 된다.
    op.execute(
        text(
            "DELETE FROM public.system_settings "
            "WHERE key = 'web_search_cost_tracking_since'"
        )
    )
    op.execute(text("ALTER TABLE usage.usage_logs DROP COLUMN IF EXISTS web_search_cost_usd"))
