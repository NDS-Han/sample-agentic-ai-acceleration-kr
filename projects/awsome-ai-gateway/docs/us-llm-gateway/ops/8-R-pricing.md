# 8-R. 모델 단가 맞추기 — AWS 실제 청구와 같게 (US-11)

> ← [operations.md](../operations.md) §8 목차로 · 이 절 = **§8-R** · 5분 + 캐시 5분 · **단가가 바뀔 때마다 반복**
>
> **한 줄**: 게이트웨이의 비용·예산은 **DB 의 단가 × 토큰**이다. 단가가 AWS 청구와 다르면 요청은 성공하고
> 비용·예산만 조용히 틀린다. 단가 표는 파일 하나(`update-scripts/pricing.tsv`), 적용은 스크립트 하나(`08`).
>
> **이 문서를 봐야 하나?** US-10 을 [8-D](8-D-upstream-sync.md) 로 끝냈다면 **US-11 은 하지 않는다** — 8-D 의 ⑧ 단계에서 이미 끝났다.
> **모델을 새로 추가했거나 AWS 단가가 바뀌었을 때만** 이 문서로 와서 ② 부터 따라 한다.

```
① 언제 하나 → ② 단가 표 고치기(바뀌었을 때만) → ③ 적용(08) → ④ 확인(14 · 비용 검산)
```

## ① 언제 하나

**하지 않는 경우**
- **US-10 을 [8-D](8-D-upstream-sync.md) 로 끝냈다** — US-11 을 따로 하지 않는다. 8-D 의 ⑧ 단계가 이 작업이고, 거기서 이미 단가를 맞췄다. 앞으로 upstream 을 다시 들여올 때도 8-D 를 따르면 ⑧ 에 포함돼 있다.
- **처음 설치했다** — install-guide §4-2 (C) 가 같은 표의 값을 심는다.

**하는 경우**
- **AWS 단가가 바뀌었다** — ② 에서 표를 먼저 고친 뒤 ③.
- **모델을 새로 등록했다**([8-M](8-M-models.md)) — `02` 는 `config.env` 의 단가로 넣는다. 등록 뒤 ③ 의 첫 줄(차이 확인)로 표와 맞는지 본다.

## ② 단가 표 고치기 — 바뀌었을 때만

정본은 **파일 하나**: `docs/us-llm-gateway/update-scripts/pricing.tsv`. `08`(적용)과 `14`(점검)가 둘 다 이 파일을 기준으로 삼는다.

- 열(탭 구분): `alias` · `input` · `output` · `cache_5m` · `cache_1h` · `cache_read` · `asof` · `source` — 단가는 **USD / 1K 토큰**.
- 캐시 단가 = 입력 단가 × 1.25(5분 기록) · × 2(1시간 기록) · × 0.1(읽기).
- 값을 고치면 `asof`(확인한 날짜)와 `source`(어디서 확인했나)도 같이 고친다 — 값이 조용히 낡는 것을 드러내는 유일한 장치다.
- **다른 리전·티어로 청구받는 배포**, `global.*` 모델을 쓰는 배포는 자기 청구 단가로 행을 고친다.
- 단가는 **수동**이다 — 신모델은 AWS Pricing API·가격 페이지에 없을 수 있다. AWS Price List 의 SKU 와 Cost Explorer 청구 실측으로 확인한다.

지금 표의 값(2026-09-15 확인 · 정본은 파일):

| alias | input /1K | output /1K | /1M 로는 |
|---|---|---|---|
| `claude-opus-5` | 0.005500 | 0.027500 | $5.50 / $27.50 |
| `claude-sonnet-5` | 0.002200 | 0.011000 | $2.20 / $11 |
| `claude-haiku-4-5-20251001` | 0.001100 | 0.005500 | $1.10 / $5.50 |

`claude-opus-4-8` 은 표에 없다 — 시드 단가를 유지하고 `08` 이 건드리지 않는다.

## ③ 적용 — 5분

▶ 실행 · 배포 EC2
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 08-set-model-pricing.sh
bash 08-set-model-pricing.sh --apply
```
기대:
- 첫 줄(인자 없음)은 **아무것도 바꾸지 않는다** — alias 마다 `current:` / `table:` 두 줄과 `-> CHANGE`. 전부 같으면 `nothing to change` 로 끝난다.
- `--apply` 는 `yes` 입력 → `rollback SQL written: snapshots/<ts>-08-pricing-rollback.sql` → 검증 `OK <alias> open row = table (…)` 이 바뀐 alias 수만큼.
- 새 호출에는 **5분 뒤**부터 새 단가가 쓰인다. `--apply --wait` 면 스크립트가 5분을 기다려 준다.
- `NOT REGISTERED — skipped` = DB 에 없는 alias 다. 모델 등록은 [8-M](8-M-models.md).

📋 참고: 한 모델만 = `--alias <alias>` · SQL 만 보기(AWS 불필요) = `--print-sql`. 동작은 "열린 단가 행(`effective_until IS NULL`)을 닫고 새 행을 넣는다" — 한 트랜잭션이고 행을 지우지 않는다.

## ④ 확인

▶ 실행 · 배포 EC2
```bash
bash 14-postdeploy-check.sh | grep -i price
```
기대: `OK <alias> price = pricing.tsv (…)` 가 표의 alias 수만큼(1분쯤 걸린다 — 일회용 psql 파드).

**호출 1건으로 검산** — `bash 16-usage-recent.sh --hours 1` 의 `cost_usd` 가 아래와 같아야 한다.
```
cost = (입력 토큰 × input + 출력 토큰 × output) ÷ 1000      (캐시 토큰이 있으면 cache 단가도 더한다)
예) Opus 5 · 입력 797 / 출력 10  ->  797×0.0055/1000 + 10×0.0275/1000 = 0.004659   (2026-09-20 prod)
```

## ⑤ 왜 이 값인가

- `us.anthropic.*`(미국 리전 묶음 프로파일) 호출은 AWS 가 글로벌(`global.*`) 단가보다 **10% 높은 Standard 티어**로 청구한다 — AWS Price List us-west-2 의 `*_standard` SKU, Cost Explorer 청구 실측(2026-09-01~14)으로 확인했다.
- Sonnet 5: 출시 때 예고된 "9/1 부터 $3/$15" 인상은 **취소**됐다 — 표준가 $2/$10(글로벌), Standard $2.20/$11.
- 종전 DB 값은 글로벌 단가라 Opus 5 가 10% 과소였고, Sonnet 5 는 취소된 인상이 미리 들어가 36% 과대였다.

## ⑥ 함정

- **5분 캐시** — 모델 정보(단가 포함)는 Redis 에 300초 캐시된다. 캐시는 외부 ElastiCache 라 파드 재시작으로 앞당길 수 없다.
- **과거 기록은 그대로** — `usage_logs.cost_usd` 는 기록 시점의 단가로 고정이다. 새 단가 행의 `effective_from` 은 적용 시각이다.
- **upstream 마이그레이션이 되돌린다** — 모델·단가를 심는 마이그레이션이 글로벌 값을 다시 넣는다. 그래서 8-D 는 단가를 배포 **뒤**(⑧)에 맞춘다.
- **가격 행이 없으면 비용이 $0 으로 기록된다** — 요청은 성공하고 예산만 우회된다([update-scripts README 「왜 단가가 필수인가」](../update-scripts/README.md#왜-단가가-필수인가)).
- **alias 가 `global.*` 프로파일을 가리키면 단가보다 라우팅이 먼저다** — `14` 가 `not the us.* route` 로 알려준다. `bash 02-add-opus5-model.sh --remap` 으로 `us.*` 로 옮긴 뒤 ③.

## ⑦ 되돌리기

가장 쉬운 길은 **표를 이전 값으로 되돌리고 ③ 을 다시 도는 것**이다 — 이전 값은 `--apply` 가 남긴 `snapshots/<ts>-08-pricing-rollback.sql` 안에 있다. 그 SQL 을 psql 파드로 직접 실행해도 같다([8-D §롤백](8-D-upstream-sync.md#롤백)). 어느 쪽이든 이전 값이 **새 행으로** 들어가고(행 삭제 없음) 5분 뒤 반영된다.
