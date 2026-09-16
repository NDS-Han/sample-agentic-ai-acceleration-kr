# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""correct Claude Sonnet 5 pricing to the AWS-published rate (was 1.5x too high)

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-26

0027 이 예고한 정정을 집행한다. 0027 은 Claude 5 계열을 등록할 때 공시 단가가 없어서
직전 세대 단가를 **잠정값**으로 넣고 이렇게 적어 두었다:

    "직전 세대 동일 단가로 등록하고, 확정 시 별도 마이그레이션으로 정정한다"
    "Sonnet 5  ← Sonnet 4.6 과 동일   in 0.003000 / out 0.015000 / ..."

그 트리거가 발생했다. AWS 가 2026-08-14 에 Claude 5 SKU 를 Price List 에 공시했고
(AmazonBedrockFoundationModels offer version 20260814094546, OnDemand effectiveDate
2026-08-01), 실제 공시가는 Sonnet 4.6 과 **다르다**.

## 실측 대조 (2026-08-26, Price List API 직접 조회)

    curl -s https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/\\
         AmazonBedrockFoundationModels/current/index.json

alias 의 provider_model_id 가 ``global.anthropic.*`` 이므로 **Global CRIS** SKU 가
권위 있는 계열이다(In-Region 계열은 정확히 1.10 배 — 예: Sonnet 5 input Global $2.00
vs In-Region $2.20). Global 단가는 리전 불변임을 확인했다(APN2 = USE1, 5개 차원 전부).
claude-code 라우팅 리전이 ap-northeast-2 이므로 이 불변성이 필요하다.

| alias 계열 | 차원 | 공시(Global, /1M) | 기존 seed(/1M) | 배수 |
|---|---|---|---|---|
| **Sonnet 5** | input       | **$2.00**  | $3.00  | **1.500x** |
| **Sonnet 5** | output      | **$10.00** | $15.00 | **1.500x** |
| **Sonnet 5** | cache 5m    | **$2.50**  | $3.75  | **1.500x** |
| **Sonnet 5** | cache 1h    | **$4.00**  | $6.00  | **1.500x** |
| **Sonnet 5** | cache read  | **$0.20**  | $0.30  | **1.500x** |

SKU: input ``YGES7RD83JWVPNKG`` / output ``JEHFQVAE53BY2X97`` / cache-write-5m
``KSBHJHHKYATEYJ7G`` / cache-write-1h ``3KW76RJZ3F7W8ZZ8`` / cache-read
``EFCA839SS62N88RU`` (usagetype ``USE1-MP:USE1_*_global_standard-Units``).

즉 Sonnet 5 는 5개 차원 **전부** 정확히 1.5 배로 과대 청구되어 왔다. 청구 방향이
과대이므로(고객에게 실제 AWS 원가보다 많이 받음) 정정한다.

## 이 마이그레이션이 건드리지 **않는** 것 — 전수 감사 결과

같은 조회로 seed 된 모든 alias 를 대조했다. Sonnet 5 외에는 정정할 것이 없다.
(다시 감사하지 않도록 결과를 남긴다. 단위: /1M, Global CRIS.)

| alias | 공시 in/out/5m/1h/read | 판정 |
|---|---|---|
| claude-haiku-4-5-20251001 | 1.00 / 5.00 / 1.25 / 2.00 / 0.10 | MATCH (1.000x) |
| claude-sonnet-4-6 | 3.00 / 15.00 / 3.75 / 6.00 / 0.30 | MATCH |
| claude-sonnet-4-6[1m] | 동일 SKU | MATCH — Sonnet 4.6 은 long-context SKU 자체가 없다 |
| global.anthropic.claude-opus-4-6-v1 | 5.00 / 25.00 / 6.25 / 10.00 / 0.50 | MATCH |
| claude-opus-4-7 | 5.00 / 25.00 / 6.25 / 10.00 / 0.50 | MATCH |
| claude-opus-4-8 (+full ID) | 5.00 / 25.00 / 6.25 / 10.00 / 0.50 | MATCH |
| **claude-opus-5 (+full ID)** | 5.00 / 25.00 / 6.25 / 10.00 / 0.50 | **MATCH** — 0027 의 "Opus 4.8 동일" 추정은 공시로 확인됨 |
| codex-gpt-5.6-terra | 2.20 / 13.20 / 2.75 / — / 0.22 | MATCH (GovCloud SKU / 1.2, 4/4 차원 정확히 일치) |
| codex-gpt-5.6-luna | 0.22 / 1.32 / 0.275 / — / 0.022 | MATCH (동일 방식) |

공시 근거가 없어 **의도적으로 손대지 않는** 행 (추정으로 청구액을 바꾸지 않는다):

* ``codex-gpt`` (openai.gpt-5.5) — 4개 Bedrock offer 전수에 SKU 0건. seed 된
  $1.25/$10.00 는 OpenAI 1P 정가이고 0017 이 스스로 'PLACEHOLDER' 라 적어 두었다.
  gpt-5.5 는 Mantle 전용이라 표준 카탈로그 레코드가 아예 없다. 859 계정 CUR/인보이스
  라인아이템 대조만이 확정할 수 있다. (cache-write 컬럼이 0 인 것도 미검증 —
  형제 Mantle 모델은 전부 30m cache-write SKU 를 input×1.25 로 공시한다.)
* ``codex-gpt-5.6-sol`` — terra/luna 는 us-gov-west-1 에 있는데 sol 은 어느 리전에도
  없다. seed 값은 terra 의 정확히 2.5 배로 내부 일관성은 있으나 기계 판독 근거가 없다.
* ``cowork-opus`` — Mantle(도쿄) 별칭인데 0009 가 Opus 4.8 **Global** 단가를 넣었다.
  Anthropic 모델은 '-mantle-' usagetype 이 0건이고 Opus 4.8 모델카드의 bedrock-mantle
  행은 Geo/Global inference ID 가 N/A 라, APN1 In-Region($5.50 = 0.909 배 과소)만이
  적용될 수 있다는 **추론**이다. 905 계정 인보이스로 확인 후 별도 마이그레이션.
* ``llama-3-70b`` — OPENMODEL 자가호스팅(endpoint_url NULL). AWS 공시가가 구조적으로
  존재하지 않는다. 0029 스스로 잠정값이라 명시.

## effective_from 을 2026-08-26 으로 두는 이유

AWS 의 OnDemand effectiveDate 는 2026-08-01 이지만 **소급 적용하지 않는다.**

1. router_service.\\_get_pricing 은 ``effective_from <= now()`` 중 **effective_from
   DESC LIMIT 1** 을 고른다. 0027 행이 2026-08-19 이므로 2026-08-01 행을 넣으면
   0027 행이 계속 이기고 새 행은 죽은 행이 된다.
2. 이미 기록된 usage_logs.cost_usd 는 요청 시점에 확정·차감(Redis budget_deduct)된
   값이라 가격 행을 바꿔도 재계산되지 않는다. 시계열의 목적은 과거 보존이다.

따라서 2026-08-19 ~ 2026-08-26 사이의 Sonnet 5 사용분은 1.5 배로 기록된 채 남는다.
그 구간에 실사용이 있었다면 정산 정정은 **별도 판단**이 필요하다:

    SELECT count(*), sum(cost_usd) FROM usage.usage_logs
     WHERE model_alias IN ('claude-sonnet-5','global.anthropic.claude-sonnet-5');

0 건이면 이 마이그레이션만으로 종결된다.

## 운영자 개입이 있으면 no-op 이다

열린 행의 5개 차원이 0027 의 잠정값과 **정확히 일치할 때만** 정정한다. admin UI
(model_service.set_pricing) 나 PricingSyncService 로 이미 단가를 바꿔 둔 환경에서는
아무것도 하지 않는다 — 마이그레이션이 운영자의 의도적 값을 조용히 덮어쓰면 안 된다.
따라서 환경마다 결과가 다를 수 있다(정정됨 / no-op). 적용 후 아래로 확인할 것:

    SELECT model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens
      FROM model.model_pricings
     WHERE model_alias LIKE '%sonnet-5%' AND effective_until IS NULL;

## ⚠️ 감시 항목 — 2026-08-31 이후 재확인

Anthropic 1P 는 Sonnet 5 를 $3.00/$15.00 로 안내하면서 2026-08-31 까지 $2.00/$10.00
도입가를 적용한다는 정보가 있다. 만약 Bedrock 이 1P 를 따라간다면 2026-09-01 에
Global 이 $3.00/$15.00 (= 지금 제거하는 그 값)으로 오를 수 있다.

**그럼에도 지금 $2.00 을 넣는다.** Price List 에는 OnDemand term 이 정확히 1개이고
(Sonnet 5 토큰 SKU 265개 전부 nterms=1, effectiveDate 2026-08-01), 예약된 2차 term 도
promo/intro usagetype 도 없다. 즉 2026-08-26 시점의 권위 있는 공시가는 $2.00 이다.
없는 인상을 미리 넣는 것은 추측이며, 추측을 **과대 청구 방향으로** 하는 것은 특히
잘못이다. 2026-08-31 이후 offer 를 재조회해 인상이 실제로 공시되면 그때 또 하나의
effective_from 행을 넣는다(이 파일과 동일한 패턴).

## 배포 시 필수 절차

``MODEL_CACHE_TTL = 300`` — router_service 가 model config 를 Redis 에 5분 캐시한다.
마이그레이션만 돌리면 최대 5분간 구 단가로 계산된다. 배포 후 캐시를 비울 것:

    redis-cli --scan --pattern 'model:*' | xargs -r redis-cli DEL

또한 0029 가 아직 어떤 migration 이미지 태그에도 반영돼 있지 않다
(dev ``1.0.50-models`` 주석 head=0028, prod ``1.0.51-initorder``). 0030 은 0029 와
함께 적용된다 — 다음 migration 이미지 빌드 시 values 의 태그/주석을 갱신할 것.
"""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"

# 0027 이 넣은 잠정 행의 effective_from. downgrade 에서 이 행을 다시 열 때 쓴다.
PROVISIONAL_FROM = "2026-08-19T00:00:00Z"
EFFECTIVE_FROM = "2026-08-26T00:00:00Z"

# 0027 은 짧은 alias 와 full inference-profile ID 를 **둘 다** 등록했다. 클라이언트가
# 어느 형태로 보내든 같은 단가여야 하므로 두 행 모두 정정한다. 한쪽만 고치면 보내는
# 형태에 따라 청구액이 달라진다.
SONNET_5_ALIASES = ("claude-sonnet-5", "global.anthropic.claude-sonnet-5")

# 공시 Global CRIS 단가, /1k (Price List /1M ÷ 1000).
PUBLISHED = {
    "input": "0.002000",   # $2.00 /1M
    "output": "0.010000",  # $10.00 /1M
    "cache_5m": "0.002500",  # $2.50 /1M
    "cache_1h": "0.004000",  # $4.00 /1M
    "cache_read": "0.000200",  # $0.20 /1M
}

# 0027 이 넣은 잠정 단가. 이 값과 **정확히 일치하는 열린 행**만 정정 대상이다 —
# 아래 upgrade() 의 운영자 개입 가드 참조.
PROVISIONAL = {
    "input": "0.003000",
    "output": "0.015000",
    "cache_5m": "0.003750",
    "cache_1h": "0.006000",
    "cache_read": "0.000300",
}


def upgrade() -> None:
    for alias in SONNET_5_ALIASES:
        # 열려 있는 행(effective_until IS NULL)을 새 행 시작점으로 닫는다. 이는
        # admin-api 의 ModelRepository.close_current_pricing 과 **같은 규약**이다
        # (model_service.set_pricing 이 새 행 삽입 전에 호출한다). 단가 자체는 절대
        # 덮어쓰지 않는다 — 과거 구간의 계산 근거로 보존해야 한다.
        #
        # router_service 는 effective_from DESC 로 고르므로 이 UPDATE 없이도 새 행이
        # 이기지만, 닫지 않으면 열린 행이 2개가 되어 close_current_pricing /
        # get_current_pricing(scalar_one_or_none) 이 이후에 깨진다.
        #
        # ⚠️ **운영자 개입 가드** — 5개 차원이 0027 의 잠정값과 정확히 일치할 때만 닫는다.
        # admin UI(model_service.set_pricing) 나 PricingSyncService 로 운영자가 이미
        # 단가를 바꿨다면 그 행은 건드리지 않는다. 이 가드가 없으면 마이그레이션이
        # 운영자의 의도적 값(예: 사내 chargeback 마크업)을 조용히 덮어쓴다.
        op.execute(
            f"""
            UPDATE model.model_pricings
               SET effective_until = '{EFFECTIVE_FROM}'
             WHERE model_alias = '{alias}'
               AND effective_until IS NULL
               AND input_price_per_1k_tokens = {PROVISIONAL["input"]}
               AND output_price_per_1k_tokens = {PROVISIONAL["output"]}
               AND cache_creation_5m_price_per_1k_tokens = {PROVISIONAL["cache_5m"]}
               AND cache_creation_1h_price_per_1k_tokens = {PROVISIONAL["cache_1h"]}
               AND cache_read_price_per_1k_tokens = {PROVISIONAL["cache_read"]}
            """
        )

        # 0027/0025 와 동일한 가드: model_pricings PK 는 gen_random_uuid() 라
        # ON CONFLICT dedupe 가 불가능하므로 (model_alias, effective_from) 으로 막는다.
        #
        # 추가로 "열린 행이 없을 때만" 삽입한다. 위 UPDATE 가 잠정 행을 닫았다면 열린
        # 행이 0개이므로 삽입된다. 운영자 행이 남아 있으면(위 UPDATE 가 건너뜀) 열린
        # 행이 그대로 있으므로 삽입도 건너뛴다 → 마이그레이션 전체가 깔끔한 no-op.
        # 재실행 시에도 우리 행이 열려 있어 자연히 no-op 이다.
        op.execute(
            f"""
            INSERT INTO model.model_pricings
                (id, model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
                 cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
                 cache_read_price_per_1k_tokens, effective_from, created_by)
            SELECT gen_random_uuid(), '{alias}',
                   {PUBLISHED["input"]}, {PUBLISHED["output"]},
                   {PUBLISHED["cache_5m"]}, {PUBLISHED["cache_1h"]},
                   {PUBLISHED["cache_read"]},
                   '{EFFECTIVE_FROM}', '{SYSTEM_USER}'
             WHERE EXISTS (SELECT 1 FROM model.model_aliases WHERE alias = '{alias}')
               AND NOT EXISTS (
                   SELECT 1 FROM model.model_pricings
                    WHERE model_alias = '{alias}' AND effective_until IS NULL
               )
               AND NOT EXISTS (
                   SELECT 1 FROM model.model_pricings
                    WHERE model_alias = '{alias}' AND effective_from = '{EFFECTIVE_FROM}'
               )
            """
        )

        # description 의 "단가 잠정" 문구도 함께 정정한다 — 운영자가 admin UI 에서
        # 보는 문장이고, 잠정이 아니게 되었기 때문이다.
        #
        # ⚠️ 위 삽입이 **실제로 일어났을 때만** 바꾼다(EXISTS). 운영자 개입으로 단가 정정을
        #    건너뛴 alias 의 설명만 "단가 공시 확인" 으로 바뀌면, 화면 설명이 그 환경의 실제
        #    열린 단가(사내 마크업 등)와 모순된다. no-op 이면 description 도 no-op —
        #    "마이그레이션 전체가 깔끔한 no-op" 이라는 위 주석의 약속이 부분이면 안 된다.
        op.execute(
            f"""
            UPDATE model.model_aliases
               SET description = replace(description,
                       '단가 잠정(Sonnet 4.6 동일), 공시 시 정정',
                       '단가 공시 확인(2026-08-14 Price List, Global CRIS $2.00/$10.00)')
             WHERE alias = '{alias}'
               AND EXISTS (
                   SELECT 1 FROM model.model_pricings
                    WHERE model_alias = '{alias}'
                      AND effective_from = '{EFFECTIVE_FROM}'
                      AND input_price_per_1k_tokens = {PUBLISHED["input"]}
                      AND output_price_per_1k_tokens = {PUBLISHED["output"]}
               )
            """
        )


def downgrade() -> None:
    """정정 행을 제거하고 0027 의 잠정 행을 다시 연다.

    UPDATE 로 단가를 덮어쓴 것이 없으므로 되돌릴 것은 (1) 새 행 삭제, (2) 잠정 행의
    effective_until 을 NULL 로 복원, (3) description 문구 복원 뿐이다.
    """
    for alias in SONNET_5_ALIASES:
        # 우리가 넣은 행만 지운다 — 단가 차원까지 대조한다. effective_from 만으로 지우면,
        # 운영자가 마침 같은 날짜로 자기 행을 넣어 둔 환경에서 downgrade 가 그 행을
        # 삭제한다(upgrade 는 운영자를 존중하는데 downgrade 가 파괴하면 가드가 무의미하다).
        op.execute(
            f"""
            DELETE FROM model.model_pricings
             WHERE model_alias = '{alias}'
               AND effective_from = '{EFFECTIVE_FROM}'
               AND input_price_per_1k_tokens = {PUBLISHED["input"]}
               AND output_price_per_1k_tokens = {PUBLISHED["output"]}
               AND cache_read_price_per_1k_tokens = {PUBLISHED["cache_read"]}
            """
        )
        # 0027 이 넣은 그 행만 다시 연다. effective_until 을 무조건 NULL 로 밀면
        # 0027 이전에 닫힌 다른 행까지 열려 열린 행이 여러 개가 된다.
        op.execute(
            f"""
            UPDATE model.model_pricings
               SET effective_until = NULL
             WHERE model_alias = '{alias}'
               AND effective_from = '{PROVISIONAL_FROM}'
               AND effective_until = '{EFFECTIVE_FROM}'
            """
        )
        op.execute(
            f"""
            UPDATE model.model_aliases
               SET description = replace(description,
                       '단가 공시 확인(2026-08-14 Price List, Global CRIS $2.00/$10.00)',
                       '단가 잠정(Sonnet 4.6 동일), 공시 시 정정')
             WHERE alias = '{alias}'
            """
        )
