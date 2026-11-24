# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""usage rollup v2 — daily_aggregates 새 입도 + 일자 레지스트리 + 월 누적 계층

Revision ID: 0060
Revises: 0059
Create Date: 2026-11-24

## 왜
Analytics 의 사용자 × 모델 토큰 표가 일별 롤업을 읽으려면 롤업이 raw(usage_logs)와
**같은 질문에 같은 답**을 해야 한다. 기존 입도 (date, user_id, model_alias) 는:
  - status 가 없어 Total Tokens KPI(SUCCESS 만)와 맞출 수 없고,
  - client 가 없어 앱 필터를 걸 수 없고,
  - team/dept 를 "그 날 마지막 요청의 팀"으로 귀속해 팀 스코프가 raw 와 어긋나고,
  - 지연(latency)·전체 호출 수가 없다.

## 무엇
1. usage.daily_aggregates
   - client VARCHAR(32)        — COALESCE(usage_logs.client,'other'). NULL = 0060 이전(레거시) 행
   - status usage.usage_status — NULL = 레거시 행(전 status 혼합)
   - call_count BIGINT         — count(*) (request_count 는 종전 정의 그대로: 계량 행만)
   - latency_ms_sum BIGINT     — 평균 지연 = latency_ms_sum / call_count
   - 유니크: (date, user_id, model_alias) → (date, user_id, team_id, dept_id, model_alias, client, status)
     레거시 행은 client/status 가 NULL 이라 새 유니크와 충돌하지 않는다. 워커가 날짜를
     재집계하면(DELETE+INSERT) 그 날짜의 레거시 행은 새 입도 행으로 교체된다.
2. usage.daily_aggregate_days — 날짜별 레지스트리. admin-api 는 grain_version=2 · dirty=false ·
   tz=자기 REPORTING_TIMEZONE 인 날짜만 롤업에서 읽고 나머지는 raw 로 읽는다.
   dirty 는 과거 날짜에 늦게 도착한 usage_logs 를 워커(batch_flusher)가 표시한다.
3. usage.monthly_aggregates + usage.monthly_aggregate_months — 월 누적 계층(같은 입도,
   date 대신 period=월 1일). covered_through 까지의 일별 롤업 합이다. 긴 custom 구간은
   통째 포함되는 달을 여기서 읽는다.

## 호환
gateway-proxy /v1/usage/me·/team 은 행을 **합산**만 하므로 입도가 잘게 쪼개져도 합계가
같다(전 status 를 계속 담는 이유). request_count 정의도 그대로다.

## 락
ADD COLUMN(NOT NULL DEFAULT 상수 포함)은 PG11+ 카탈로그 변경뿐이다. 유니크 인덱스 생성은
daily_aggregates 쓰기를 잠깐 막는다(쓰는 주체는 야간 워커 잡뿐).

⚠️ 배포 순서: 이 마이그레이션 → cost-recorder-worker. 구 워커의 UPSERT 는 옛 유니크
(date,user_id,model_alias)를 ON CONFLICT 대상으로 쓰므로, 두 배포 사이에 구 워커의 야간 잡
(리포팅 tz 00:10)이나 기동 백필이 돌면 그 회차만 실패한다(로그 후 다음 회차에 새 워커가 복구).
"""
from alembic import op
from sqlalchemy import text

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


_UPGRADE = [
    # 1. daily_aggregates 새 컬럼 — 멱등(init SQL 이 먼저 놓았을 수 있다).
    "ALTER TABLE usage.daily_aggregates ADD COLUMN IF NOT EXISTS client VARCHAR(32)",
    "ALTER TABLE usage.daily_aggregates ADD COLUMN IF NOT EXISTS status usage.usage_status",
    "ALTER TABLE usage.daily_aggregates ADD COLUMN IF NOT EXISTS call_count BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE usage.daily_aggregates ADD COLUMN IF NOT EXISTS latency_ms_sum BIGINT NOT NULL DEFAULT 0",
    # 옛 유니크 제약 제거 — 이름이 설치본마다 같다는 보장이 없어 열 구성으로 찾는다.
    """
    DO $$
    DECLARE r record;
    BEGIN
        FOR r IN
            SELECT c.conname
            FROM pg_constraint c
            WHERE c.conrelid = 'usage.daily_aggregates'::regclass
              AND c.contype = 'u'
              AND (SELECT array_agg(a.attname::text ORDER BY a.attname)
                   FROM unnest(c.conkey) k JOIN pg_attribute a
                     ON a.attrelid = c.conrelid AND a.attnum = k)
                  = ARRAY['date', 'model_alias', 'user_id']
        LOOP
            EXECUTE format('ALTER TABLE usage.daily_aggregates DROP CONSTRAINT %I', r.conname);
        END LOOP;
    END $$
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_aggregates_grain ON usage.daily_aggregates "
    "(date, user_id, team_id, dept_id, model_alias, client, status)",
    # 2. 일자 레지스트리
    """
    CREATE TABLE IF NOT EXISTS usage.daily_aggregate_days (
        date           DATE        PRIMARY KEY,
        tz             VARCHAR(64),
        grain_version  SMALLINT    NOT NULL DEFAULT 0,
        dirty          BOOLEAN     NOT NULL DEFAULT false,
        aggregated_at  TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_daily_aggregate_days_dirty "
    "ON usage.daily_aggregate_days (date) WHERE dirty",
    # 3. 월 누적 계층
    """
    CREATE TABLE IF NOT EXISTS usage.monthly_aggregates (
        period                 DATE          NOT NULL,
        user_id                UUID          NOT NULL,
        team_id                UUID          NOT NULL,
        dept_id                UUID          NOT NULL,
        model_alias            VARCHAR(128)  NOT NULL,
        client                 VARCHAR(32)   NOT NULL,
        status                 usage.usage_status NOT NULL,
        input_tokens           BIGINT        NOT NULL DEFAULT 0,
        output_tokens          BIGINT        NOT NULL DEFAULT 0,
        cache_creation_tokens  BIGINT        NOT NULL DEFAULT 0,
        cache_read_tokens      BIGINT        NOT NULL DEFAULT 0,
        total_tokens           BIGINT        NOT NULL DEFAULT 0,
        total_cost_usd         NUMERIC(16,6) NOT NULL DEFAULT 0,
        call_count             BIGINT        NOT NULL DEFAULT 0,
        request_count          BIGINT        NOT NULL DEFAULT 0,
        latency_ms_sum         BIGINT        NOT NULL DEFAULT 0
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_monthly_aggregates_grain ON usage.monthly_aggregates "
    "(period, user_id, team_id, dept_id, model_alias, client, status)",
    "CREATE INDEX IF NOT EXISTS idx_monthly_aggregates_user_period "
    "ON usage.monthly_aggregates (user_id, period)",
    """
    CREATE TABLE IF NOT EXISTS usage.monthly_aggregate_months (
        period           DATE        PRIMARY KEY,
        tz               VARCHAR(64) NOT NULL,
        covered_through  DATE        NOT NULL,
        built_at         TIMESTAMPTZ NOT NULL
    )
    """,
]


def upgrade() -> None:
    for stmt in _UPGRADE:
        op.execute(text(stmt))


def downgrade() -> None:
    # 새 입도 행은 옛 유니크 (date,user_id,model_alias) 를 위반하므로 지운다 — 다운그레이드
    # 후 구 워커의 야간 잡(최근 N일)과 기동 백필(행 없는 날짜)이 옛 입도로 다시 채운다.
    op.execute(text("DROP TABLE IF EXISTS usage.monthly_aggregate_months"))
    op.execute(text("DROP TABLE IF EXISTS usage.monthly_aggregates"))
    op.execute(text("DROP TABLE IF EXISTS usage.daily_aggregate_days"))
    op.execute(text("DROP INDEX IF EXISTS usage.uq_daily_aggregates_grain"))
    op.execute(text("DELETE FROM usage.daily_aggregates WHERE status IS NOT NULL"))
    op.execute(text("ALTER TABLE usage.daily_aggregates DROP COLUMN IF EXISTS latency_ms_sum"))
    op.execute(text("ALTER TABLE usage.daily_aggregates DROP COLUMN IF EXISTS call_count"))
    op.execute(text("ALTER TABLE usage.daily_aggregates DROP COLUMN IF EXISTS status"))
    op.execute(text("ALTER TABLE usage.daily_aggregates DROP COLUMN IF EXISTS client"))
    op.execute(text(
        "ALTER TABLE usage.daily_aggregates ADD CONSTRAINT daily_aggregates_date_user_id_model_alias_key "
        "UNIQUE (date, user_id, model_alias)"
    ))
