
## R9 확인 라운드 (fbfcd213) — kiro opus 5.5

청구 정확도 결함 0 — 전 수정 항목 정합 확인. 발견 6건(모두 Low/정리):

| # | 발견 | 조치 |
|---|---|---|
| 1 | 스케일 파생 테스트가 구·신 공식에서 우연히 동일값(구별력 없음) + stale 주석 | 비표준 read 비율(5%) 케이스 추가 — 신 공식 0.0002/구 공식 0.0004 분기. 주석·docstring 정정 |
| 2 | 배치 격리 테스트가 미래행 조기스킵만 검증 — savepoint except 미도달 | set_pricing ConflictError 주입으로 except 경로 실검증 + __aexit__ 예외 인수 + 중복 alias 어설션 |
| 3 | IntegrityError 가 caught 튜플에 없어 DB 경합 시 배치 전체 abort(fail-closed) | 튜플에 추가 — savepoint 롤백 후라 안전 |
| 4 | UI가 threshold 변경을 표시 안 함(current 타입에 필드 없음) | 타입 추가 + `> cur → proposed` 렌더 |
| 5 | `==` 허용이 이미 시작된 행을 [t,t)로 지울 수 있음(크래프티드 API 전용) | 동일 시각 허용을 "prior 가 미래 시작"일 때로 한정 |
| 6 | 리뷰 산출물 diff/프롬프트 파일이 커밋됨 | 제거 |

- 결과: **945 passed**(+2), tsc clean
