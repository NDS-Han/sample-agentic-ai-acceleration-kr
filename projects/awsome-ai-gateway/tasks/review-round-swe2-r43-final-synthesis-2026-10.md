# R43 — 최종 종합: 2차 전수 리뷰 결산 + 기능 갭·UX 백로그 우선순위

날짜: 2026-10 · 범위: R36~R42(2차 전수) + R18~R35(1차) 전 라운드 결산.
목적: (a) 고쳐진 것 vs 남은 것의 최종 대조, (b) 기능 갭 우선순위,
(c) 배포/운영 잔여, (d) UI/UX 백로그 — 구현 로드맵 제안.

## 1. 2차 리뷰에서 실제로 고쳐진 것 (R36~R42)

| 라운드 | 수정 | 성격 |
|---|---|---|
| R36 | ARCHITECTURE.md 마이그레이션 head 0022→0048 | 문서 정합 |
| R37 | `/v1/usage/team/{id}` JWT 전용→DUAL — **TEAM_LEADER VK가 권한 체크에 도달조차 못하던 실 결함** | 인증 경계 |
| R37 | asyncio.iscoroutinefunction 폐기 예고 수정, .env.example 포인터 | 위생 |
| R38 | pytest 마커 등록 (경고 102→13) | 테스트 위생 |
| R39 | LazyCharts 스켈레톤(L6), 404 페이지 | UX |
| R40 | **`notifications:system` 무발행 채널 연결** — 소비자만 있던 채널에 degradation_mode/service_health_change/provider_error 발행 | 기능 갭(실 기능 복원) |
| R41 | `configure` 미질문 필드 침묵 삭제(dev_login/smtp_port/sender_name), eks apply 노트 누락, 문서 정합 3건 | 배포 정확도 |
| R42 | chat-agent pytest 마커, CI stale 파일명 | 위생 |

2차 전수의 실 결함 밀도는 낮음 — R18~R35 1차에서 대부분의 구조적 결함이 이미
소화됐고, 2차는 **횡단 정합**(인증 전략, 채널 발행-구독 계약, 마법사 필드 보존)에서만
의미 있는 발견이 남아 있었다. 코드베이스는 프로덕션 수준으로 수렴했다고 판정.

## 2. 기능 갭 — 우선순위 (구현 후보, 결함 아님)

### HIGH — 운영 공백

| # | 갭 | 현재 | 필요한 것 | 규모 추정 |
|---|---|---|---|---|
| G1 | **메트릭 알림 룰 0건** | Prometheus에 alert rule 없음. R40으로 system 채널(열화·회로 OPEN)은 연결됐으나 메트릭 기반 알림은 전무 | 5xx 비율, cost:stream lag/PEL, dead_letter, Redis/DB DEGRADED 지속, 무효 VK 폭주, Bedrock 429 비율 → Grafana contact point 또는 Alertmanager → notifications:system 연결 | 중 — 인프라는 전부 존재, 룰 정의+배선만 |
| G2 | **Routing profile 변경이 SQL 전용** | `account_role_arn`/`default_model`/`region` 변경 = 손 SQL + 5분 캐시 대기. 장애 시 롤백(in-account 복귀)도 SQL | admin API CRUD + `routing_profile:{client}` 즉시 무효화 + audit + UI 폼. 롤백 운영 절차가 SQL이면 대응 시간을 직접 늘림 | 중대 — API+UI+캐시 무효화+감사 |

### MED — 관리·감사 표면

| # | 갭 | 현재 | 필요한 것 |
|---|---|---|---|
| G3 | audit_logs 조회면 부재 | `audit_logger` 기록은 존재 — 조회 API/UI 없음(SQL만) | `GET /admin/audit/logs` + 액터/액션/기간 필터 + `/admin/audit` 페이지 |
| G4 | DEVELOPER 셀프 VK 관리 | `/my`는 사용량+예산만. 본인 키 목록/만료 확인/폐기 불가 → 만료는 갑자기 401, 유출은 admin 티켓 | `GET/DELETE /admin/my/keys` + `/my` 키 섹션 + 만료 임박 배지 |
| G5 | DR/복구 런북 부재 | 백업 체크리스트만 — restore/RTO/RPO/PITR 검증/시크릿 유실 복구 없음 | restore 리허설 문서 + `.env`/VK 키 유실 시나리오 |
| G6 (선택) | 비용 이상 탐지 | 예산 threshold(80/90/100)만 | daily_aggregates 기반 전주 대비 spike 탐지 → G1 알림 채널 공유 |
| G7 (정책) | 콘텐츠 안전/PII | body_logging은 "로깅 O·마스킹 X" — 활성화 자체가 개인정보 리스크 | Guardrails/마스킹 필드 allowlist — 조직 정책 결정 선행 |

### 배포 대상 결정 항목

| 항목 | 상태 |
|---|---|
| `bi_insight` 플래그 | 렌더러는 정직하게 "미구현" 노트. chat-agent는 코드 완성(275 테스트) + AgentCore 배포 경로가 별도 — **NDS gateway.yaml로 프로비저닝할지** 결정 필요 |
| chat-agent ARM64+us-east-1 | 리전 중립 목표와 충돌 — AgentCore 서비스 리전 가용성에 종속 |
| compose 통합 테스트 CI | pgvector 없는 stock 이미지로 의도적 skip — 라이브 스택 CI 잡은 운영 결정 |
| `/model/*` 와이어 형식 | ARCHITECTURE §9 "확인 필요" 기록된 알려진 계약 — 실측 검증 작업 |

## 3. 배포/운영 잔여 (설계 한계·판정 보류)

| 출처 | 항목 | 판정 |
|---|---|---|
| R35 | 서드파티 이미지 digest 핀 정책 없음(`:latest`, `pg16` 등) | 다음 배포 작업 시 gateway.yaml 스키마 확장 여부 결정 |
| R32/R33 | **installer↔cowork-installer 포크 드리프트 재발 패턴** — R26 수정이 R32에서 다시 필요했던 것처럼 반복 | 공유 모듈화 또는 diff 체크리스트 절차 필요 — 구조적 과제 |
| R31 | doctor `--force` dead flag, eks env↔context 바인딩 미구현, helm upgrade가 외부 `--set` reset, eks `oidc.client_secret` 미렌더(차트 한계), ECS same-account 전제 | 각각 문서화된 한계 — 개선은 선택 |
| R37 | client 스푸핑 → 예산 버킷 선택(설계 한계, R3-7 문서화) | VK↔앱 바인딩은 스키마/발급 플로우 변경 필요 |
| R42 | Actions major 태그 핀(SHA 아님) | 표준 관행 — 공급망 강화는 선택 |

## 4. UI/UX 백로그 (UX#1·#2 + R39 정산)

### 잔여 — 중기

| # | 항목 | 내용 |
|---|---|---|
| M6 | `/analytics` 차트 엔진 이원화 | 추이=recharts, 막대·도넛=Chart.js — 테마 소스 이원화+이중 번들. recharts 통합 리팩터 |

### 잔여 — LOW

| # | 항목 |
|---|---|
| L3 | `by_user` 막대 팀 병기 없음(대시보드 Top은 subtitle 표기) |
| L4 | 모니터링 배지 렌더 이원화(CSS 클래스 vs `<Badge>`) |
| L5 | 대시보드 도넛 범례 3줄 줄바꿈 |
| L8 | custom 기간 마이그레이션 seed 미포함 힌트(동작은 의도 — 안내 문구만) |

### 이미 해소 (검증 완료)

H1 team 라벨 canonical · H2 팔레트 단일화 · M1 모델 표시명 · M2 토큰 포맷터 ·
M3 라벨 정합 · M4 export 필터 전파 · M5 TZ 축 · L1 `0ms`→`—` · L2 ErrorState 통일 ·
L6 스켈레톤(R39) · L7 `/chat` 안내 카드 · 404 페이지(R39) · i18n 1095/1095 패리티 ·
권한표-사이드바 정합 · 모바일 시트 레이아웃.

## 5. 검증된 강도 — 재작업 불필요 (갭 아님)

- **클라이언트 계약**: 3종 전부 실기기 검증 — Anthropic 에러 봉투+SSE 전 집합,
  `/v1/models/{id}` 세션 검증, Codex `/v1/responses` 이형 입력 3종 차단+
  INACTIVE 킬스위치+미등록 폴백, `codex_desktop` 분류.
- **데이터 플레인 경화**: 이중청구 3중 방어, 멱등 소비, 펜싱 토큰, 열화→폴백→
  알림(R40 연결) 체인.
- **배포 도구**: 3타겟 drift 검출, 정직한 feature-note 계약, teardown 소유 경계,
  capture 미유추치 honest notes, 시크릿 생성/보존.
- **SQL 안전(chat-agent)**: 7단계 실행 경계 + 정확도 안전망 + 세션 강제.
- **CI**: 최소 권한, frozen lock, 실 PG 증명 잡+통과 하한, ratchet 대조군.

## 6. 권고 순서

1. **G1 알림 룰** — 운영 공백이 가장 크고 인프라는 전부 존재(룰 정의만)
2. **G2 라우팅 프로필 CRUD** — 장애 대응 시간에 직결, audit와 함께
3. **G3+G4 감사 조회·셀프 키 관리** — 관리 표면의 마지막 공백, 함께 묶기 좋음
4. **G5 DR 런북** — 백업 검증 없이는 백업이 미검증
5. **bi_insight 배포 경로 결정** — chat-agent가 준비됐으므로 gateway.yaml 통합 여부
6. **M6 차트 통합 + LOW UX 잔여** — 여유 시
7. **G6 이상탐지·G7 PII** — 정책 결정과 함께
