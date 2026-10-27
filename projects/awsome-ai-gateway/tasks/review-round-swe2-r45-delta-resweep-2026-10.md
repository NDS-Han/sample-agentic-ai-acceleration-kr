# R45 — 델타 기반 전체 재검증 (post-R43 재스윕)

날짜: 2026-10-26 · 방식: 변하지 않은 코드 재독 대신 "마지막 리뷰 이후 diff" +
횡단 불변식 + 문서 드리프트 점검.

## Phase 1 — R43(cd09b674) 이후 델타 목록과 커버 여부

| 커밋 | 내용 | R44 커버 |
|---|---|---|
| 25e78be3 | 배포 환경 시각 결함(display_name trim, 범례 정렬, i18n) | 본 라운드 — UI 표층만, 저위험 확인 |
| b38045c3·17cb9f80·bdf088ed·8d645e41 | 모델 레벨 계정 라우팅 | ✔ |
| 0517a4be·ac3a566f·525122b0·6c5396ff | 문서 동기화 | ✔ |
| fce8c30c | /icon.svg 공개 | ✔ |
| ee2d1ef5 | eks apply --plan diff | ✔ |
| 7bdaf099 | L4/L5/L8 + 카드 높이 | 본 라운드 — 검증 통과 확인 |
| ac836b9d | G2 클라이언트 라우팅 API+UI + G5 런북 | ✔ (F1 수정 포함) |
| 6229870d·e00df1b9 | M6 recharts 통합 + 다크 툴팁 | 본 라운드 — 포팅 정확성+다크 모드 검증 |

## Phase 2 — 횡단 불변식

| 불변식 | 결과 |
|---|---|
| 신규 admin 엔드포인트 = require_admin + audit + post-commit invalidate | ✔ (PUT /routing 동일 패턴) |
| 크로스계정 라우팅 시 비용 귀속 | ✔ 비용은 `calculate_cost(usage, pricing)` — 모델당 단가라 VK 사용자/팀 귀속은 계정 무관 |
| ⚠️ 정보: 단가가 리전 비민감 | 기록 — Bedrock 리전간 단가 차이는 모델링 안 됨. 라우팅된 리전의 실제 단가가 다르면 표시 비용은 설정 단가 기준. 허용 수준의 근사치로 판정 |
| 새 UI 폼의 401 | ✔ adminAPI server action → ActionResult error 반환, 패널이 실패 시 서버 상태 재로드 — web-search와 동일 패턴 |
| 마이그레이션 체인 | ✔ 0049 → down_revision 0048, 연속, 신규 없음 |
| 늦은 응답이 선택 앱을 덮어쓰지 않음 | ✔ clientRef + op.routing 카운터 drain 확인 |

## Phase 3 — 문서 드리프트

- R44에서 `aurora_cluster_id` 허위 output 수정 완료. 이번 스윕에서 추가 드리프트 없음.

## 결론

R43 이후 전 델타 재검증 완료. 발견·수정: R44-F1(런북 tf output).
신규 발견 없음 — 잔여는 모두 의도된 한계(backend/enabled SQL 전용,
리전 무관 단가, 조각 팝아웃 미재현).

미커버: CI 워크플로우(사용자 제외), admin-chat-agent(제외 결정), L3/G1/G3·G4·G6/G7(스코프 아웃 확정).
