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
