"""add missing indexes and uniqueness invariants

Revision ID: 0034
Revises: 0033

## 무엇을, 왜

내부 트리에 있던 인덱스/유니크 제약 중 **이 스키마에 없고, 실측으로 이득이 확인된 것만**
가져온다. 객체 단위로 판정했고 기각한 것도 아래에 근거와 함께 남긴다.

### 1. `idx_users_team_id` — auth.users (team_id)

팀 단위 조회가 전부 seq scan 이었다. Cognito 동기화의 팀별 사용자 조회,
`/admin/budgets/summary` 의 팀 매핑, `list_users(team_id=...)` 가 이 컬럼을 탄다.

### 2. `idx_model_aliases_lower_unique` — model.model_aliases (lower(alias))

alias 는 대소문자를 구분하지 않고 조회되는데(게이트웨이가 요청의 model 문자열을 그대로
넘기고 사용자는 대소문자를 섞어 쓴다) 유일성은 대소문자 구분으로만 걸려 있었다.
`Claude-Sonnet` 과 `claude-sonnet` 이 동시에 존재할 수 있었고, 그때 어느 행이 이기는지는
정렬에 의존한다 — 즉 같은 요청이 배포마다 다른 모델로 갈 수 있다.

### 3. `idx_model_pricings_active_unique` — model.model_pricings (model_alias) WHERE effective_until IS NULL

"alias 당 활성 단가는 하나" 가 코드의 불변식이다
(`model_repository.close_current_pricing` 이 새 단가를 넣기 전에 기존 행을 닫는다).
그 사이에 끼어든 동시 요청이 활성 단가 2개를 만들면 어느 쪽으로 과금되는지가 비결정적이다.

### 4. `idx_virtual_keys_user_active_unique` — auth.virtual_keys (user_id) WHERE status='ACTIVE'

⚠️ 이 인덱스는 **애플리케이션 수정과 함께여야만 안전하다.** 근거(실 PostgreSQL 16 실측):

  * 발급은 CTE 한 문장으로 "기존 ACTIVE 를 EXPIRED + 새 ACTIVE INSERT" 를 한다
    (`key_repository.expire_and_create`). CTE 안의 문장들은 같은 스냅샷을 보므로
    상대 트랜잭션이 넣는 행을 보지 못한다.
  * 인덱스가 **없으면**: 동시 발급 2건이 **둘 다 성공**해 사용자당 ACTIVE 키가 2개 남는다
    (실측 `최종 ACTIVE: 2`). 코드가 표방하는 불변식이 조용히 깨진 상태다.
  * 인덱스만 **넣으면**: 그 경쟁이 유니크 위반으로 드러나는데 재시도가 없어
    **두 요청이 모두 실패**한다(실측: 최종 ACTIVE 는 기존 키 그대로).
    CLI 로그인·OIDC 교환이 그 경로이므로 사용자에겐 원인 불명의 500 이 된다.
  * 그래서 `key_service.issue_key` 에 savepoint + 1회 재시도를 함께 넣었다. 상대가
    커밋을 끝낸 뒤 재시도하면 우리 UPDATE 가 그 키를 EXPIRED 로 만들고 INSERT 가 성공한다.

  단독 CTE 는 인덱스와 정상 공존한다(실측 `expired=1 inserted=1`, 최종 ACTIVE 1).

### 5. `idx_productivity_events_idempotency`

이벤트 재전송이 생산성 지표를 중복 계상하는 것을 막는다. 컬럼이 없으므로 함께 추가한다.

## 기각한 객체

* **`idx_usage_logs_requested_at`** — 0026 이 같은 이름으로 이미 만든다. 중복.
* **`idx_usage_logs_requested_at_status`** — 실측으로 이득 0. 플래너가 아예 고르지 않는다
  (계획·cost·buffers 가 단일 인덱스일 때와 동일). `sum(cost_usd)` 때문에 index-only scan 이
  불가능해 heap 방문이 필수이고, status 로 걸러지는 건 소수의 비-SUCCESS 행뿐이라 읽는
  블록 수가 줄지 않는다. 근거는 0026 의 docstring 에 기록.
* **`idx_rate_limit_configs_active_unique`** — 0024 의
  `uq_rate_limit_configs_active` 가 이미 있고 `COALESCE(scope_id, ...)` 로 NULL scope_id 까지
  덮어 **더 강하다.** 약한 쪽으로 바꾸면 후퇴다.
* **`idx_teams_dept_name_unique` / `idx_departments_org_name_unique`** — 0035 로 분리.
  기존 운영 DB 에 동명 팀/부서가 있으면 이 마이그레이션 전체가 실패해 배포가 멈춘다.
  병합 계획(어느 팀으로 합치고 멤버를 어디로 옮길지)이 필요한 데이터 작업이라 코드 변경과
  같은 커밋에 넣지 않는다.

## 왜 db/init/02_create_tables.sql 에는 넣지 않는가

`db/run_migration.sh` 가 init SQL 을 매 배포마다 재적용한 **뒤** `alembic upgrade head` 를
돌린다. 마이그레이션만으로 신규·기존 DB 가 모두 덮이므로 init 중복은 이득이 없고,
init 쪽이 `ON_ERROR_STOP=1` 이라 중복 정의가 실패하면 배포가 죽는다.

## CONCURRENTLY 를 쓰지 않는 이유

`db/env.py` 가 `transaction_per_migration=True` 로 각 마이그레이션을 트랜잭션에 감싸는데
CONCURRENTLY 는 트랜잭션 블록 안에서 실행할 수 없다(0026 의 같은 주석 참조).
"""
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. 팀별 사용자 조회
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_team_id ON auth.users (team_id)")

    # ── 2. alias 대소문자 무시 유일성
    #    기존 중복이 있으면 여기서 실패한다 — 그게 맞다. 대소문자만 다른 alias 두 개는
    #    라우팅이 비결정적이라는 뜻이므로, 조용히 넘기지 말고 운영자가 정리해야 한다.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_model_aliases_lower_unique "
        "ON model.model_aliases (lower(alias))"
    )

    # ── 3. alias 당 활성 단가 1개
    #    ⚠️ 생성 전에 기존 중복을 **닫는다**(삭제하지 않는다). 단가는 이력이므로 지우면
    #    과거 청구 근거가 사라진다. 오래된 행의 effective_until 을 다음 행의
    #    effective_from 으로 채워 이력을 연결한 뒤 유일성을 건다.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, model_alias, effective_from,
                   LEAD(effective_from) OVER (
                       PARTITION BY model_alias ORDER BY effective_from, id
                   ) AS next_from,
                   ROW_NUMBER() OVER (
                       PARTITION BY model_alias ORDER BY effective_from DESC, id DESC
                   ) AS rn
            FROM model.model_pricings
            WHERE effective_until IS NULL
        )
        UPDATE model.model_pricings p
        SET effective_until = COALESCE(r.next_from, now())
        FROM ranked r
        WHERE p.id = r.id AND r.rn > 1
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_model_pricings_active_unique "
        "ON model.model_pricings (model_alias) WHERE effective_until IS NULL"
    )

    # ── 4. 사용자당 ACTIVE 가상키 1개
    #    ⚠️ key_service.issue_key 의 savepoint 재시도와 **짝**이다. 그것 없이 이 인덱스만
    #    두면 동시 발급이 양쪽 500 이 된다(docstring 의 실측 참조).
    #    생성 전에 기존 중복을 정리한다: 사용자별 최신 1개만 ACTIVE 로 남기고 나머지는
    #    EXPIRED. 키는 이력이 아니라 자격증명이므로 오래된 것을 만료시키는 게 맞다.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY user_id ORDER BY issued_at DESC, id DESC
                   ) AS rn
            FROM auth.virtual_keys
            WHERE status = 'ACTIVE'
        )
        UPDATE auth.virtual_keys k
        SET status = 'EXPIRED'
        FROM ranked r
        WHERE k.id = r.id AND r.rn > 1
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_virtual_keys_user_active_unique "
        "ON auth.virtual_keys (user_id) WHERE status = 'ACTIVE'"
    )

    # ── 5. 생산성 이벤트 멱등키
    op.execute(
        "ALTER TABLE usage.productivity_events "
        "ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(256)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_productivity_events_idempotency "
        "ON usage.productivity_events (idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )


def downgrade() -> None:
    # ⚠️ 되돌릴 때 데이터는 복구하지 않는다. upgrade 가 닫은 단가 행과 만료시킨 키를
    #    되살리면 유일성이 없던 상태로 돌아가는 것이라 의미가 없고, 무엇보다 어느 행이
    #    원래 열려 있었는지 기록이 없다. 인덱스/컬럼만 제거한다.
    op.execute("DROP INDEX IF EXISTS usage.idx_productivity_events_idempotency")
    op.execute(
        "ALTER TABLE usage.productivity_events DROP COLUMN IF EXISTS idempotency_key"
    )
    op.execute("DROP INDEX IF EXISTS auth.idx_virtual_keys_user_active_unique")
    op.execute("DROP INDEX IF EXISTS model.idx_model_pricings_active_unique")
    op.execute("DROP INDEX IF EXISTS model.idx_model_aliases_lower_unique")
    op.execute("DROP INDEX IF EXISTS auth.idx_users_team_id")
