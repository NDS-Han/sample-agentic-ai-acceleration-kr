# Ponytail×opus 5.5 — 과금/단가 동기화 라운드 (R6, pricing stream)

대상 커밋: `94c68440`(provider 게이트 수정) + 그 주변 과금 표면.
리뷰어: kiro `claude-opus-5.5`, 비대화형 시니어 리뷰.

## 발견 → 조치

| # | 심각도 | 발견 | 조치 |
|---|---|---|---|
| F1 | 과금 정확도(High) | `FetchResult.lookup` 이 `global.<base>` 를 geo/plain 폴백으로 허용 — Global CRIS 단가(≈9% 저가)가 in-region 모델에 적용돼 과소청구, 역방향(저장 `global.X` → plain)은 과다청구. `us.openai.gpt-5.6-*` 3 행이 실제로 이 경로를 탐 | global↔비-global 폴백 전면 폐지. exact → geo prefix(us./eu./apac./jp./us-gov.)만 plain `<base>` 폴백. docstring 정정("교차 없다"고 적혀 있었으나 구현은 교차함) |
| F2 | 과금 정확도(High) | `set_pricing` 의 "이전 행 구간 이어받기" + sync 가 `*_above_272k_tokens` 를 매핑 안 함 → 기본 단가 개정 후 >272K 구간 단가만 부패(구값 잔존 또는 NULL → 최대 ~2× 과소청구). 라이브 실측: `gpt-6.1-sol` 기본 단가는 갱신됐는데 구간은 NULL | `NormalizedPrice` 에 `long_*` 6 필드 추가, LiteLLM normalizer 가 above_272k 계열 매핑(1h 구간은 카탈로그 필드 부재 → 5m 구간과 같은 1h/5m 비율 파생). `PricingRequest` 에 optional 6 필드(None=이어받기 유지 계약), preview diff 에 `proposed_long_*`+note 노출, apply 가 전달 |
| F3 | 데이터 정합(Medium) | 미래 시작 예약 단가 행이 있을 때 sync-apply 가 `close_current_pricing` 으로 `effective_until < effective_from` 역전 행 생성 → 예약 영구 소실 | 미래 `effective_from` open 행 감지 시 해당 alias 적용 보류 + errors 명시 |
| F4 | 감사 정합(Medium) | preview 가 카탈로그를 가져오고 apply 가 다시 가져옴 — 사이에 소스가 바뀌면 운영자가 본 값과 다른 단가가 SET_PRICING 감사에 박힘 | `PriceSyncApplyRequest.expected`(승인값 맵, alias→8 필드) 추가 — 서버가 재수집 값과 대조, 불일치·누락 시 해당 alias 거부. `expected` 비어 있으면 레거시 동작(하위호환). UI 가 preview diff 의 proposed 값을 그대로 전송 |
| F5 | 출처(Low) | 파생 캐시 단가(소스 미게시 → input 배수 추정)가 apply/감사에서 게시가와 구별 불가 | `PricingRequest.cache_derived` 필드 → `SET_PRICING` 감사 `changes.after` 에 `cache_derived`+`long_context_threshold` 기록. preview note 문구 소스-중립화 |
| — | 정합 확인 | 카탈로그 절단(230건 cap), Lambda FunctionError, null 가격 미매칭, 배치 원자성, 캐시 배수 현행 유지 | 수정 불요 확인 |

## 자체 병행 발견

- 라이브 DB 실측으로 F2 재확인 — `gpt-6.1-sol` 기본 단가는 94c68440 이후 정상 갱신됐으나 `long_context_*` 전부 NULL, codex 행들은 구 단가 잔존.

## 검증

- `pytest tests/` — **938 passed, 50 skipped** (회귀 6건: lookup 경계 1, 구간 매핑 2, apply 안전 3 + 미래행 1)
- admin-ui `tsc --noEmit` — 0 errors

## 배포

- 커밋: `1c511714` · helm **rev 185**
- 이미지: admin-api + scheduler + admin-ui(expected 승인값 배선) — `1c511714`
- 스모크: **12 PASS / 0 FAIL** · `nds/dev` push 완료

---

# R7 — 1c511714 수정 검증 라운드

리뷰어 재검증에서 11건 보고(내 수정의 미비점 포함):

| # | 심각도 | 발견 | 조치 |
|---|---|---|---|
| F2' | High | 드리프트 가드가 **선택적** — `expected` 생략·빈 맵·미선택 alias 는 무방어로 재수집값 적용 | `expected` 스키마 필수(min_length=1), 라우터 `or None` 폐기, alias 누락 = 거부. API 직접 호출자도 fail-closed(422) |
| F3' | Med-High | 구간 캐시 3필드(5m/1h/read)가 preview 미노출·승인 대조 밖 — 보이지 않는 필드 드리프트 통과 | PriceSyncDiff/PriceSyncExpected/fresh 대조에 3필드 추가, ModelPricingResponse 에 long_* 6필드 노출(현재 구간 비교 가능) |
| F4' | Med | 0 가격 falsy 버그 — `if base_5m_eff:` 등이 0을 결손 처리해 threshold+NULL 조합 생성 가능 | 전 구간 `is not None` 판정, 기본 5m=0 시 표준 배수 파생, 구간 존재 시 5필드 전부 비-NULL 보장 |
| F5' | Med | 감사에 `data.long_*`(이어받기 시 None)만 기록돼 실제 기록값 불명 | 해소된 `long_tier` 전체 + `long_cache_derived` 기록 |
| F6' | Med | threshold-only 요청이 수동 PUT 에 허용(구간 선언만 있고 단가 결손) | threshold `ge=1` + model_validator(threshold 시 in/out 필수) |
| F7' | Med | 시간 역전 가드가 sync apply 에만 — 수동 PUT 은 여전히 역전 행 생성 가능 | `set_pricing` 본체로 이동(`data.effective_from <= prior.effective_from` → ConflictError) — 전 경로 fail-closed |
| F8' | Low | 스택드 프리픽스 `us.global.X` 가 strip 1회로 global 우회 | `base` 가 다시 prefix 지닐 때 미매칭 |
| F9' | Med-Low(수용) | AWS normalizer 가 region/tier 필터 없이 `model` 속성으로 병합 — 리전·구간 SKU 혼합 가능성 | 실데이터 미검증 항목 — AWS 소스는 LiteLLM 보조 경로라 잔여로 문서화 |
| F10' | Low(수용) | 272k 하드코딩 — LiteLLM 스키마가 `above_272k` 만 제공 | 계약 문서화(다른 임계값 변종은 카탈로그 필드 부재) |

## 검증·배포

- `pytest tests/` — **943 passed, 50 skipped** (회귀 7건 추가: 스택드 프리픽스, 0가격 파생, expected 누락, 구간캐시 드리프트, set_pricing 역전, 스키마 단위성)
- admin-ui `tsc --noEmit` 0 errors
- 커밋 `eb0658e8` · helm **rev 186** · 이미지 admin-api/scheduler/admin-ui `eb0658e8` · 스모크 **12 PASS / 0 FAIL** · `nds/dev` push 완료

---

# R8 — eb0658e8 보강분 재검증

| # | 심각도 | 발견 | 조치 |
|---|---|---|---|
| R8-1 | Med | naive `effective_from`(offset 없는 ISO)이 timestamptz 행과 비교돼 TypeError→500 | `effective_from: AwareDatetime` — 422로 거부 |
| R8-2 | Med | 구간 단가가 페이로드엔 있는데 UI 표엔 없음 — 운영자가 안 본 값을 승인 | 미리보기 표에 `>272K tok` 서브행(현재→제안 5열 전부) |
| R8-3 | Med | alias 하나의 예외(중복 alias·범위 밖 카탈로그 값·시간 역전)가 배치 전체 롤백 | alias dedupe(`dict.fromkeys`) + per-alias `begin_nested` savepoint + 도메인 오류만 per-alias skip/errors. 지연 Redis 쓰기는 savepoint 롤백을 따라 폐기(R1 계약) |
| R8-4 | Med | 구간 캐시 파생이 Claude 고정 배수 사용 — GPT 계열(read≈5%)에 과대추정 | 모델 자체 발행 기본비율 스케일(`long_x = base_x × long_in/input`), input=0 이면 표준 배수 폴백 |
| R8-5 | Low | 수동 PUT 이 threshold+in/out 만 싣고 캐시 구간 NULL → cost_recorder 단락요율 폴백 과소청구 | 해소된 구간에 단가 결손 있으면 ValidationError 거부 |
| R8-6 | Low | 예약 행을 같은 `effective_from` 으로 교정 불가 — `<=` 거부 + 삭제 경로 없음 | 가드를 `<` 로 — 같은 시각 허용(제로-길이 행으로 닫히고 대체) |
| R8-7 | Low | AWS 소스는 구간 미제공 — 구간 있는 모델이 기본가만 개정돼도 무경고 | preview note "장문 구간 단가 미동기화(소스 미제공 — 이전 행 유지됨)" |
| R8-8 | Low(수용) | AWS normalizer 의 리전/티어 미필터(model 속성 병합) | 실데이터 미검증 — 잔여로 문서화, AWS 소스는 보조 경로 |
| — | nit(수용) | context_window/max_output_tokens 는 expected 대조 밖 | 비과금 필드 — 수용 |

## 검증·배포

- `pytest tests/` — **944 passed, 50 skipped** · tsc 0 errors
- 커밋·helm·스모크: (아래 기록)
