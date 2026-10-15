# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add the >100K long-context pricing tier to claude-haiku-5-5

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-15

## 배경

Claude Haiku 5.5 는 Anthropic/AWS 공식 **프롬프트 길이 구간 요금제**다:

    | 구간            | input | output | cache 5m | cache 1h | cache read | (/1M) |
    | ≤100K tokens    | $0.10 | $0.50  | $0.125   | $0.20    | $0.01      |
    | >100K tokens    | $0.50 | $2.50  | $0.625   | $1.00    | $0.05      | (5배) |

근거: Anthropic pricing 문서("Claude Haiku 5.5 is priced by prompt length:
a request whose prompt is over 100,000 tokens pays higher prices") +
AWS Bedrock model card("'Claude Haiku 5.5 - Long Context' 별도 행").
프롬프트 길이는 cache read/write 를 **포함한** 입력 토큰 총량이고 요청 단위로
독립 판정한다 — gateway 의 resolve_context_tier(세 입력 버킷 합 > threshold)와
정확히 같은 의미론이다. **long 구간 단가는 short 의 정확히 5배** — 이 비율은
공시 구조라 지역 프로파일(global./us./apac. 프리미엄)과 무관하게 성립한다.

## 문제

`claude-haiku-5-5` 는 2026-10-13 에 admin 등록으로 생긴 런타임 행이다
(시드 아님). 단가 5개 차원은 공시가와 일치하지만 `long_context_*` 6열이
전부 NULL — 100K 초과 요청이 **5배 과소청구**된다. pricing_sync 도 이전엔
`*_above_272k_tokens` 필드만 읽어 카탈로그 동기화로는 이 티어(100K)를
못 잡았다(같은 라운드에서 `above_{N}k` 일반화로 수정).

## 감사 결과 — 소급 영향 없음

    SELECT count(*), sum(cost_usd),
           max(input_tokens + cache_read_tokens + cache_creation_tokens)
      FROM usage.usage_logs WHERE model_alias='claude-haiku-5-5';
    → 87건 / $0.050046 / 최대 프롬프트 70,086 토큰

100K 초과 요청 0건 — 이 마이그레이션만으로 종결, 별도 정산 정정 불필요.

## 대상 지정 — 단가가 아니라 provider_model_id 로

alias 이름과 단가값으로 대상을 잡으면 지역 프로파일(us./apac. 프리미엄
요율)이나 운영자 단가 조정 환경에서 가드가 불일치해 **성공 표시인 채
no-op** 이 된다. 대신 `model_aliases.provider_model_id LIKE
'%claude-haiku-5-5%'` 로 대상 alias 를 찾고, long 단가는 닫는 행의 short
단가 ×5 로 `INSERT … SELECT` 한다 — 어떤 지역/할증 요율로 등록됐든
구조적 5배 관계는 유지된다. 유일한 가드는 "티어가 아직 없다"
(threshold IS NULL) 뿐 — Haiku 5.5 에 티어가 없는 행은 어느 단가로든
버그이므로 수정한다. 티어가 이미 있으면 no-op(운영자 개입 보존).

마지막 DO 블록 대신 Python 에서 행 수를 세어 alembic 로그로 남긴다 —
`RAISE WARNING` 은 asyncpg 드라이버(log listener 없음)에서 버려져
실제 배포 경로에서는 보이지 않는다.

## 배포 시 필수 절차

``MODEL_CACHE_TTL = 300`` — router_service 가 model config 를 Redis 에 5분
캐시한다. 적용 후 캐시를 비울 것:

    redis-cli --scan --pattern 'model:*' | xargs -r redis-cli DEL
"""
import logging

from alembic import op
from sqlalchemy import text

log = logging.getLogger("alembic.runtime.migration")

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"

EFFECTIVE_FROM = "2026-10-15T00:00:00Z"

# 대상 모델 판별 — alias 이름이 아니라 provider_model_id 로. 짧은 alias,
# global./us./apac. inference profile 모두 커버한다.
HAIKU55_PROVIDER_LIKE = "%claude-haiku-5-5%"

LONG_THRESHOLD = 100000


def upgrade() -> None:
    # 단일 CTE 문장으로 닫기+삽입을 원자적으로 묶는다 — UPDATE ... RETURNING
    # 의 결과가 곧 INSERT 의 입력이라 "행은 닫혔는데 새 열린 행은 없는"
    # 반쪽 상태가 구조적으로 불가하다.
    #
    # UPDATE 가드(INSERT 와 대칭):
    #  - effective_from = EFFECTIVE_FROM 인 행이 이미 있으면 아예 건드리지
    #    않는다 — 닫기만 하고 삽입이 막히면 열린 행 0개($0 과금)가 된다.
    #  - effective_from > EFFECTIVE_FROM 인 열린 행도 건드리지 않는다 —
    #    마이그레이션 이후에 운영자가 연 행을 과거 시점으로 닫으면
    #    effective_from > effective_until 인 역전 구간이 생겨 이력이 오염된다.
    result = op.get_bind().execute(
        text(
            f"""
            WITH closed AS (
                UPDATE model.model_pricings p
                   SET effective_until = '{EFFECTIVE_FROM}'
                 WHERE p.effective_until IS NULL
                   AND p.long_context_threshold_tokens IS NULL
                   AND p.effective_from <= '{EFFECTIVE_FROM}'
                   AND EXISTS (
                       SELECT 1 FROM model.model_aliases a
                        WHERE a.alias = p.model_alias
                          AND a.provider_model_id LIKE '{HAIKU55_PROVIDER_LIKE}'
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM model.model_pricings o
                        WHERE o.model_alias = p.model_alias
                          AND o.effective_from = '{EFFECTIVE_FROM}'
                   )
                RETURNING p.*
            )
            INSERT INTO model.model_pricings
                (id, model_alias,
                 input_price_per_1k_tokens, output_price_per_1k_tokens,
                 cache_creation_5m_price_per_1k_tokens,
                 cache_creation_1h_price_per_1k_tokens,
                 cache_read_price_per_1k_tokens,
                 long_context_threshold_tokens,
                 long_context_input_price_per_1k_tokens,
                 long_context_output_price_per_1k_tokens,
                 long_context_cache_creation_5m_price_per_1k_tokens,
                 long_context_cache_creation_1h_price_per_1k_tokens,
                 long_context_cache_read_price_per_1k_tokens,
                 effective_from, created_by)
            SELECT gen_random_uuid(), c.model_alias,
                   c.input_price_per_1k_tokens,
                   c.output_price_per_1k_tokens,
                   c.cache_creation_5m_price_per_1k_tokens,
                   c.cache_creation_1h_price_per_1k_tokens,
                   c.cache_read_price_per_1k_tokens,
                   {LONG_THRESHOLD},
                   c.input_price_per_1k_tokens * 5,
                   c.output_price_per_1k_tokens * 5,
                   c.cache_creation_5m_price_per_1k_tokens * 5,
                   c.cache_creation_1h_price_per_1k_tokens * 5,
                   c.cache_read_price_per_1k_tokens * 5,
                   '{EFFECTIVE_FROM}', '{SYSTEM_USER}'
              FROM closed c
            """
        )
    )
    closed_count = result.rowcount if result.rowcount is not None else -1

    # 검증을 Python 에서 한다 — 서버측 RAISE WARNING 은 asyncpg 드라이버가
    # 버려 배포 로그에 남지 않는다. 매칭·삽입·누락을 전부 숫자로 남겨
    # no-op 이 조용히 지나가지 않게 한다.
    bind = op.get_bind()
    matched = bind.execute(
        text(
            "SELECT count(*) FROM model.model_aliases a "
            "WHERE a.provider_model_id LIKE :pat"
        ),
        {"pat": HAIKU55_PROVIDER_LIKE},
    ).scalar()
    missing = bind.execute(
        text(
            "SELECT count(*) FROM model.model_aliases a "
            "WHERE a.provider_model_id LIKE :pat "
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM model.model_pricings p "
            "       WHERE p.model_alias = a.alias "
            "         AND p.effective_until IS NULL "
            "         AND p.long_context_threshold_tokens IS NOT NULL"
            "  )"
        ),
        {"pat": HAIKU55_PROVIDER_LIKE},
    ).scalar()
    log.info(
        "haiku55_tier_migration: matched_aliases=%s closed=%s missing_tier=%s",
        matched,
        closed_count,
        missing,
    )
    if missing:
        log.warning(
            "haiku55_tier_migration: %s alias(es) still have no open tiered "
            "pricing row — migration did not apply (check pricing rows); "
            ">100K prompts will be undercharged 5x",
            missing,
        )


def downgrade() -> None:
    """티어 행을 제거하고 이전(무티어) 행을 다시 연다.

    UPDATE 로 단가를 덮어쓴 게 아니라 행을 닫고 새 행을 둔 것이므로 되돌릴 것은
    (1) 우리 행 삭제, (2) effective_until = EFFECTIVE_FROM 인 행의 복원 뿐이다.
    """
    # 우리가 넣은 행만 지운다 — effective_from·threshold·"long = short×5"
    # 관계까지 대조해 운영자가 이후 만든 행은 건드리지 않는다.
    op.execute(
        f"""
        DELETE FROM model.model_pricings p
         WHERE p.effective_from = '{EFFECTIVE_FROM}'
           AND p.long_context_threshold_tokens = {LONG_THRESHOLD}
           AND p.long_context_input_price_per_1k_tokens
               = p.input_price_per_1k_tokens * 5
           AND p.long_context_output_price_per_1k_tokens
               = p.output_price_per_1k_tokens * 5
           AND EXISTS (
               SELECT 1 FROM model.model_aliases a
                WHERE a.alias = p.model_alias
                  AND a.provider_model_id LIKE '{HAIKU55_PROVIDER_LIKE}'
           )
        """
    )
    # 닫았던 이전 행을 다시 연다 — 우리가 닫은 시점과 같은 effective_until 인
    # 무티어 행만. 단, 그 alias 에 이미 다른 열린 행이 있으면 재오픈하지
    # 않는다(열린 행 유일 인덱스 idx_model_pricings_active_unique 위반 방지 —
    # 업그레이드 후 운영자가 새 가격 행을 등록한 경우).
    op.execute(
        f"""
        UPDATE model.model_pricings p
           SET effective_until = NULL
         WHERE p.effective_until = '{EFFECTIVE_FROM}'
           AND p.long_context_threshold_tokens IS NULL
           AND EXISTS (
               SELECT 1 FROM model.model_aliases a
                WHERE a.alias = p.model_alias
                  AND a.provider_model_id LIKE '{HAIKU55_PROVIDER_LIKE}'
           )
           AND NOT EXISTS (
               SELECT 1 FROM model.model_pricings o
                WHERE o.model_alias = p.model_alias
                  AND o.effective_until IS NULL
           )
        """
    )
