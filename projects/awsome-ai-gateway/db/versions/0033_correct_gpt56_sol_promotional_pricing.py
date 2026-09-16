# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""correct GPT-5.6 Sol pricing to the 2026-08-21 published cut (was 25%/50% too high)

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-03

0030 이 "기계 판독 근거가 없어 의도적으로 손대지 않는다"고 남겨 둔 행 중 하나를 집행한다.
0030 의 판단은 Price List API 만 봤을 때는 옳았다 — sol 은 4개 Bedrock offer 전수에
SKU 가 0건이다. 그러나 **SKU 부재는 가격의 부재가 아니다.** AWS 모델 카드에는 공시가가
있고, 날짜와 발표문까지 있다.

## 근거 (2026-09-03 실조회)

발표문: ``https://aws.amazon.com/about-aws/whats-new/2026/08/bedrock-openai-gpt-56-sol-reduced-pricing/``

> "Following the recent Terra and Luna price reductions, Sol now costs $4 per million
> input tokens and $20 per million output tokens—20% lower input pricing and 33.3% lower
> output pricing. **This promotional pricing is available at least through November 21,
> 2026.**"

모델 카드: ``https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-sol.html``
Short Context Window (272K), $ /1M:

    inference option        input   30m cache write   cache read   output
    In-Region                4.40        5.50            0.44       22.00
    Geo CRIS (us.)           4.40        5.50            0.44       22.00
    Global CRIS (global.)    4.00        5.00            0.40       20.00

발표문의 "$4 / $20" 은 **Global CRIS** 열이고, 인하율 "20% / 33.3%" 는 Geo/In-Region 열에
정확히 맞는다(5.50 x 0.8 = 4.40, 33.00 x 2/3 = 22.00). 둘 다 진짜이며 inference option 이
다르다. 이 둘을 섞으면 9.09% 과소 청구가 된다.

``codex-gpt-5.6-sol`` 은 ``endpoint_url = https://bedrock-mantle.us-east-2.api.aws/openai``,
즉 **In-Region Mantle** 이다(CRIS 아님). 따라서 In-Region 열 = 4.40/5.50/0.44/22.00 이
적용된다. 마침 Geo 열과 동일하므로 0032 의 runtime 행과 같은 값이 된다.

가격이 plane 에 독립적이라는 근거: 카드 하나가 두 endpoint 를 모두 덮고 가격표는 하나뿐이며,
``endpoints.html`` 이 *"Choose an endpoint based on the APIs and capabilities you need,
not cost"* 라고 명시한다. 즉 이 인하는 Mantle 행에도 그대로 적용된다.

## 대조 — terra / luna 는 정정 대상이 아니다

같은 카드로 전수 대조했다. 0025 가 넣은 값과 정확히 일치한다(단위 /1M, In-Region):

    | alias               | 카드 in/cw/cr/out          | 0025 seed (x1000)          | 판정  |
    | codex-gpt-5.6-terra | 2.20 / 2.75  / 0.22  / 13.20 | 2.20 / 2.75  / 0.22  / 13.20 | MATCH |
    | codex-gpt-5.6-luna  | 0.22 / 0.275 / 0.022 / 1.32  | 0.22 / 0.275 / 0.022 / 1.32  | MATCH |
    | codex-gpt-5.6-sol   | 4.40 / 5.50  / 0.44  / 22.00 | 5.50 / 6.875 / 0.55  / 33.00 | DRIFT |

terra/luna 는 GovCloud SKU / 1.2 로도 같은 값이 나온다(0032 참조) — 서로 독립적인 두
근거가 일치한다. sol 만 정정한다.

## effective_from = 2026-08-21 (인하 발표일)

0030 은 소급을 거부했지만 그 이유는 여기 해당하지 않는다. 0030 의 문제는 기존 행이
2026-08-19 인데 AWS effectiveDate 가 2026-08-01 이어서 **소급 행이 죽는** 것이었다.
여기서 기존 sol 행은 0025 의 2026-08-06 이고 인하일은 2026-08-21 이므로
``effective_from DESC`` 에서 새 행이 이긴다. 실제 인하일을 쓰는 것이 더 정확하다.

이미 기록된 ``usage_logs.cost_usd`` 는 요청 시점에 확정·차감된 값이라 재계산되지 않는다.
2026-08-21 ~ 이 마이그레이션 적용 시점 사이의 sol 사용분은 과대 기록된 채 남는다. 정산
정정이 필요한지는 별도 판단이며, 먼저 규모를 확인할 것:

    SELECT count(*), sum(cost_usd) FROM usage.usage_logs
     WHERE model_alias = 'codex-gpt-5.6-sol' AND created_at >= '2026-08-21';

0 건이면 이 마이그레이션만으로 종결된다.

## ⚠️ 2026-11-21 재확인 필수

promotional pricing 이므로 되돌아갈 수 있다. 그날 이후 모델 카드를 재조회해서 인상이
공시되면 **같은 패턴으로 행을 하나 더** 넣는다(이 파일을 복사). 지금 없는 인상을 미리
넣지는 않는다 — 추측을 과대 청구 방향으로 하는 것은 특히 잘못이다(0030 과 같은 규칙).

0032 의 runtime 행(``gpt-5.6-sol``)은 애초에 정정된 값으로 seed 되므로 여기서 다루지
않는다. 두 plane 을 함께 손대야 하는 것은 다음 인상/인하 때부터다.

## 운영자 개입이 있으면 no-op 이다

0030 과 동일한 가드: 4개 차원(+1h 컬럼)이 0025 의 값과 **정확히 일치하는 열린 행**만
정정한다. admin UI(``model_service.set_pricing``)나 ``PricingSyncService`` 로 이미 단가를
바꿔 둔 환경에서는 아무것도 하지 않는다. 환경마다 결과가 다를 수 있다. 적용 후 확인:

    SELECT model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
           effective_from, effective_until
      FROM model.model_pricings WHERE model_alias = 'codex-gpt-5.6-sol'
     ORDER BY effective_from;

## 배포 시 필수 절차

``MODEL_CACHE_TTL = 300`` — 최대 5분간 구 단가로 계산된다. 배포 후:

    redis-cli --scan --pattern 'model:*' | xargs -r redis-cli DEL
"""
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"

ALIAS = "codex-gpt-5.6-sol"
PROVISIONAL_FROM = "2026-08-06T00:00:00Z"  # 0025 의 행
EFFECTIVE_FROM = "2026-08-21T00:00:00Z"  # AWS 인하 발표일

# 모델 카드 In-Region short-context, /1k (카드의 /1M ÷ 1000).
PUBLISHED = {
    "input": "0.004400",  # $4.40 /1M
    "output": "0.022000",  # $22.00 /1M
    "cache_5m": "0.005500",  # $5.50 /1M (30m cache write)
    "cache_read": "0.000440",  # $0.44 /1M
}

# 0025 가 넣은 값. 이 값과 정확히 일치하는 열린 행만 정정 대상이다.
# 0025 는 cache_write 를 5m/1h 두 컬럼에 같은 값으로 넣었다.
STALE = {
    "input": "0.005500",
    "output": "0.033000",
    "cache_5m": "0.006875",
    "cache_1h": "0.006875",
    "cache_read": "0.000550",
}


def upgrade() -> None:
    # 열린 행을 새 행 시작점으로 닫는다. 단가 자체는 덮어쓰지 않는다 — 과거 구간의
    # 계산 근거로 보존해야 한다. admin-api 의 ModelRepository.close_current_pricing 과
    # 같은 규약이고, 닫지 않으면 열린 행이 2개가 되어 get_current_pricing 의
    # scalar_one_or_none 이 이후에 깨진다.
    op.execute(
        f"""
        UPDATE model.model_pricings
           SET effective_until = '{EFFECTIVE_FROM}'
         WHERE model_alias = '{ALIAS}'
           AND effective_until IS NULL
           AND input_price_per_1k_tokens = {STALE["input"]}
           AND output_price_per_1k_tokens = {STALE["output"]}
           AND cache_creation_5m_price_per_1k_tokens = {STALE["cache_5m"]}
           AND cache_creation_1h_price_per_1k_tokens = {STALE["cache_1h"]}
           AND cache_read_price_per_1k_tokens = {STALE["cache_read"]}
        """
    )

    # (model_alias, effective_from) 중복 방지 + "열린 행이 없을 때만" 삽입.
    # 위 UPDATE 가 stale 행을 닫았으면 열린 행이 0개이므로 삽입된다. 운영자 행이 남아
    # 있으면(UPDATE 가 건너뜀) 열린 행이 1개이므로 삽입하지 않는다 = no-op.
    #
    # 1h 컬럼은 0025 와 동일하게 5m 과 같은 값을 넣는다: Bedrock 은 OpenAI 모델에
    # cache-write 단가를 하나(30m)만 공시하고, 1h 컬럼은 usage.cache_ttl_1h 가 설정된
    # Anthropic dialect(routers/messages.py)에서만 선택되므로 이 경로에서는 도달 불가다.
    op.execute(
        f"""
        INSERT INTO model.model_pricings
            (id, model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
             cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
             cache_read_price_per_1k_tokens, effective_from, created_by)
        SELECT gen_random_uuid(), '{ALIAS}',
               {PUBLISHED["input"]}, {PUBLISHED["output"]},
               {PUBLISHED["cache_5m"]}, {PUBLISHED["cache_5m"]},
               {PUBLISHED["cache_read"]},
               '{EFFECTIVE_FROM}', '{SYSTEM_USER}'
        WHERE EXISTS (SELECT 1 FROM model.model_aliases WHERE alias = '{ALIAS}')
          AND NOT EXISTS (
                SELECT 1 FROM model.model_pricings
                 WHERE model_alias = '{ALIAS}' AND effective_from = '{EFFECTIVE_FROM}'
          )
          AND NOT EXISTS (
                SELECT 1 FROM model.model_pricings
                 WHERE model_alias = '{ALIAS}' AND effective_until IS NULL
          )
        """
    )

    # description 도 갱신한다 — admin UI 가 이 문자열을 그대로 보여주므로, 단가만 고치고
    # 설명을 두면 운영자가 화면에서 근거를 확인할 수 없다.
    #
    # ⚠️ 위 정정이 **실제로 적용됐을 때만** 갱신한다(EXISTS). 운영자 개입으로 단가 정정이
    #    건너뛰어졌는데 설명만 "Pricing = AWS model card" 로 바뀌면, 화면의 설명이 그 환경의
    #    실제 열린 단가(사내 마크업 등)와 모순된다 — 실 PG 대조에서 이 조합을 재현해 확인했다.
    #    guard 는 부분이 아니라 전부여야 한다: no-op 이면 description 도 no-op.
    op.execute(
        f"""
        UPDATE model.model_aliases
           SET description = 'Codex -> 859 Bedrock Mantle GPT-5.6 Sol (Ohio, Responses API) '
                             '— coding / agentic. Pricing = AWS model card In-Region '
                             '(promotional since 2026-08-21, guaranteed only through 2026-11-21).'
         WHERE alias = '{ALIAS}'
           AND description = 'Codex -> 859 Bedrock Mantle GPT-5.6 Sol (Ohio, Responses API) — coding / agentic'
           AND EXISTS (
                 SELECT 1 FROM model.model_pricings
                  WHERE model_alias = '{ALIAS}'
                    AND effective_from = '{EFFECTIVE_FROM}'
                    AND input_price_per_1k_tokens = {PUBLISHED["input"]}
                    AND output_price_per_1k_tokens = {PUBLISHED["output"]}
           )
        """
    )


def downgrade() -> None:
    """정정 행을 제거하고 0025 의 행을 다시 연다.

    삽입한 행만 삭제하고(단가 5개 차원 전부 일치 조건), 그 다음에 0025 행의
    ``effective_until`` 을 NULL 로 되돌린다. 순서가 중요하다 — 먼저 열면 열린 행이
    2 개가 되는 순간이 생긴다.

    운영자가 그 사이에 또 다른 행을 넣었다면 이 DELETE 는 0건이 되고 0025 행도 열지
    않는다(``effective_until`` 이 정확히 EFFECTIVE_FROM 인 것만 되돌린다). 즉 운영자
    개입을 downgrade 도 존중한다.
    """
    op.execute(
        f"""
        DELETE FROM model.model_pricings
         WHERE model_alias = '{ALIAS}'
           AND effective_from = '{EFFECTIVE_FROM}'
           AND input_price_per_1k_tokens = {PUBLISHED["input"]}
           AND output_price_per_1k_tokens = {PUBLISHED["output"]}
           AND cache_read_price_per_1k_tokens = {PUBLISHED["cache_read"]}
        """
    )
    op.execute(
        f"""
        UPDATE model.model_pricings
           SET effective_until = NULL
         WHERE model_alias = '{ALIAS}'
           AND effective_from = '{PROVISIONAL_FROM}'
           AND effective_until = '{EFFECTIVE_FROM}'
        """
    )
    op.execute(
        f"""
        UPDATE model.model_aliases
           SET description = 'Codex -> 859 Bedrock Mantle GPT-5.6 Sol (Ohio, Responses API) — coding / agentic'
         WHERE alias = '{ALIAS}'
           AND description LIKE '%promotional since 2026-08-21%'
        """
    )
