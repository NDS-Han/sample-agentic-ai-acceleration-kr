# R19 — admin-ui 비채팅 표면 + 장문 티어 수동 입력 리뷰 (2026-10)

리뷰어: kiro `claude-opus-5.5` (read-only) + 자체 검증.
대상: R17 에서 못 본 비채팅 admin-ui 표면(모델/예산/조직/앱 패널, server
actions, utils) + R18 이관 과제 — 카탈로그 자동조회가 3rd party 활성화
의존라 대안으로 채택된 **Admin UI 장문 티어 수동 입력** end-to-end.

## 구현 범위 (선행 작업)

- **admin-api**: `ModelCreateRequest` 에 `long_context_*` 6개 필드 +
  all-or-none validator(threshold 있으면 5개 단가 전부 필수, 명시 null 은
  무티어). `create_model` 은 명시 티어 우선, 없을 때만 `_auto_long_tier`
  폴백. `extra="forbid"` 유지. threshold 상한 `le=2147483647`(Integer 열).
- **admin-ui**: `ModelListItem`/`APIModelItem`/`mapToModelListItem` 티어 필드,
  zod 스키마 all-or-none, CreateModelDialog 장문 섹션(1M 표기→1K 환산,
  base 와 중복 라벨 분리), create POST + pricing PUT 전달, `pricing_changed`
  티어 비교(편집 시 전부 비움 = "기존 티어 유지" 의미 보존), ModelsTable
  상세 행 + 배지, PriceSyncButton 소스 전환 시 프리뷰·선택 리셋.

## 라운드 1 — NOT CONVERGED (S 1, N 5)

- **S1** CREATE_MODEL 감사가 수동 입력 티어도 `auto_derived=true` 로 기록 +
  티어 값 미기록 → `explicit_tier` 플래그 도입, 해소된 티어 dict 를
  `changes.after.long_context` 에 기록(테스트 어서션 포함).
- **N1** 힌트 문구가 자동파생 조건 미명시 → ko/en 정정.
- **N2** threshold 무상한 → `le=2147483647`(스키마+zod).
- **N3** `_per_1k_tokens`→필드 매핑은 도달 불가 경로(서버 422 는 문자열
  경로) → 제거, 테스트를 zod 키 경로로 교체.
- **N4** ModelsTable 티어 임계 표기가 `fmtTokensCompact` 반올림(100,000→"100K") —
  과금 경계는 정확값 필요 → 상세 헤더 `toLocaleString`, 배지 툴팁에 정확값.
- **N5** 편집 시 미리 채워진 티어를 비우면 "유지" — lost-update 트레이드오프로
  문서화(기본 단가와 같은 문제 클래스).

## 라운드 2 — NOT CONVERGED (S 1, N 5)

라운드 1 수정 전부 정확 확인. 신규:

- **S1 AppPolicyPanel 탭 전환 레이스** — 저장 응답이 앱 전환 뒤 도착하면
  `setPolicy(구 앱 전체 정책)` 이 새 탭 화면을 덮어씀 → `clientRef` 가드
  (ref 는 렌더 본문 갱신 — React 19 가 pending async transition 동안
  이펙트를 미뤄 이펙트 갱신 ref 는 정확히 필요한 시점에 구 값을 읽음).
  fetch 경로도 `cancelled` + ref 이중 가드(deferred cleanup 은 cancelled
  를 false 로 남김). **비공백 회귀 테스트 확보** — 가드를 `if(true)` 로
  끄면 stale m2 가 codex 정책을 덮어써 실패 확인.
  발견: jsdom+React 19 환경에서 pending transition 동안 해당 컴포넌트의
  신규 커밋이 전부 미뤄짐 → 테스트는 "codex 응답 먼저, stale 응답 나중"
  순서로 완료시켜 최종 상태를 검증.
- **N1** CREATE_MODEL 의 `long_context` 감사 형식을 SET_PRICING 형식
  (`{threshold:int, input/output/cache_5m/cache_1h/cache_read:str}`)으로 통일.
- **N2** 힌트가 편집 시에도 자동파생되는 것처럼 읽힘 → 생성/편집 분기 명시.
- **N3** PriceSyncButton 소스 전환 시 AWS 프리뷰 잔존 → `setPreview(null);
  setSelected(new Set())`.
- **N4** AppPolicyPanel a11y 2건 — 칩 상태가 색·취소선뿐(sr-only 상태 텍스트
  추가), 웹서치 스위치 focus ring 없음(`peer-focus-visible:ring-*`).
- **N5** budgets 페이지 어드민 `currentUserId` 가 JWT sub(≠DB user_id 가능) —
  `/admin/my/profile` 을 역할 무관하게 조회해 `myUserId` 전달.

## 라운드 3 — NOT CONVERGED (S 2, N 6)

라운드 2 수정 전부 정확 확인. 미검토 표면에서 신규:

- **S1 AutoDowngradeConfig 로드 실패가 "설정 없음"으로 위장** — 실패 시에도
  `setLoaded(true)` → enabled=false·rules=[] 폼 → 저장하면 PUT(전체 교체)이
  기존 규칙 전량 소실. `loadFailed` + 재시도 UI + 저장 경로 차단, 스코프
  전환 리셋 + `cancelled` 가드 추가(스코프 교차 덮어쓰기는 제2의 소실 경로).
  회귀 테스트 2건.
- **S2 TeamAllocationView Σ 배정합계가 Usage 컬럼 아래** — 과금 오독.
  colSpan 3 + Allocated 컬럼 아래로.
- **N1** AppPolicyPanel `policy.client === selectedClient` 인 `shown` 게이트
  추가 — 가드가 뚫려도 구 앱 데이터가 렌더되지 않는 형상 수준 방어
  (ref 가드는 저장 방지용으로 유지 — stale 저장은 탭 복귀 시 다시 노출됨).
- **N2** `setSaveError` 비가드 + 토스트에 앱명 없음 → 가드 + `{app}` 파라미터.
- **N3** 칩 sr-only 가 "CC 허용됨" 으로 읽힘 → 시각 라벨 aria-hidden +
  sr-only 에 풀 앱명:상태.
- **N4** OrgTreeView `onSaved` 재조회가 캡처된 showEmptyTeams 로 덮어씀 —
  `treeGenRef` 세대 카운터(토글 OFF·재사용 경로 포함 전 분기 bump,
  `treeLoading` 소유권 정리).
- **N5** SetBudgetDialog config prefill 이 사용자가 고른 policy/thresholds
  덮어씀 → `policyTouchedRef`/`thresholdsTouchedRef` — baseline 은 항상
  기록(PUT-diff 의미 보존), touched 필드만 표시값 스킵.
- **N6** a11y 라벨 공백 — AutoDowngradeConfig 스위치 aria-label+ring,
  SetBudgetDialog label htmlFor/id(amount·default-cap·per-app), 슬라이더
  aria-label, 임계 × 버튼명(`{pct}` 파라미터 — map 변수 t 섀도잉도 해소),
  TeamAllocationView 멤버 cap `aria-label={t('memberCapInput',{name})}`.

## 검증 상태

- admin-ui vitest: **440** (37→38 files) 전부 통과 — AppPolicyPanel 레이스,
  AutoDowngradeConfig 실패/재시도 회귀 포함.
- `tsc --noEmit` 클린, eslint 변경 파일 클린.
- admin-api: `test_model_service.py` 69 통과(all-or-none·명시 우선·자동파생).
- 0047 마이그레이션은 R18 에서 배포·실 PG 검증 완료.

## 라운드 4 — NOT CONVERGED (S 2, N 3)

- **S1 OrgTreeView 세대-가드 부작용 3건** — stale 경로의 treeLoading 누수,
  onSaved 재조회의 stale `showEmptyTeams` 캡처(→ ref), 재사용 경로의 불필요 세대 bump.
- **S2 잘못된 메시지 키** — `budgets.loadFailed` 는 모델 접근 정책 전용이라
  용도별 `loadErrors.*` 키 추가(tree/models/appAccess/userModels).
- **N1** pendingToggle 에 client 저장 + 탭 전환 시 초기화 — 다이얼로그가 떠
  있는 동안 앱이 바뀌면 확인이 엉뚱한 앱에 저장되던 문제.
- **N2** 딥링크 fetch 동안 treeLoading 미설정 → 토글 가능 상태에서 세대 증가로
  ?node= 응답이 버려지는 경로 — beginTreeLoad 로 잠금.
- **N3** TeamAllocationView 풋터 합계 right-aligned → 컬럼 정렬(좌)과 통일.

## 라운드 5 — NOT CONVERGED (S 1, N 5)

- **S1 treeOverride 가 router.refresh() 결과를 영구 마스킹** — `root` prop 이
  바뀌면 override 폐기(토글 ON 이면 include_empty 재조회로 모드 유지).
- **N1** applyShowEmpty/딥링크 fetch transport 거부 미처리 → try/catch +
  롤백 + 토스트.
- **N2** stale 주석 정정.
- **N3** OrgDetailPanel 부분 성공 저장도 onSaved 호출 — anySaved 기준으로
  변경(전부 성공일 때만 재조회하던 것).
- **N4** KeysTable revokingId 함수형 클리어 — A 완료가 B 의 in-flight 를
  풀지 않게 `cur === keyId` 조건.
- **N5** AppPolicyPanel pending 이 transition 전역이라 다른 앱 탭 컨트롤까지
  잠금 → 앱별 in-flight 추적으로 전환.

## 라운드 6 — CONVERGED (N 6)

material/should-fix 없음. N 전부 조치:
- per-client 저장 추적을 단일 슬롯에서 in-flight 카운터로(A→B→A 복귀 잠금 유지).
- OrgTreeView root-변경 재조회 실패 처리 + onSaved transport 거부 처리.
- KeysTable try/finally.
- README BI 권한 ADMIN 정정(chat_agent 전 엔드포인트 require_admin — sidebar·
  ChatShell·PAGE_PERMISSIONS 모두 일치).
- 대시보드 /users?node= 링크는 TEAM_LEADER 에게 도달 불가로 확인(top-users/
  top-teams 가 require_admin 이라 표 자체가 ErrorState — 변경 없음).

## 라운드 7 — CONVERGED (N 5)

신규 메커니즘 검증 통과. 추가 조치:
- **treeLoading 을 세대별 소유자 집합(loadingGensRef)으로 교체** — 겹친 fetch 의
  조기 해제와 세대 증가 후 영구 잠금 두 클래스를 동시 해소.
- **N1** 로드↔저장 경합: 저장 진행 중 시작된 정책 로드가 커밋 전 읽기로 구
  정책을 되살릴 수 있음 → opRef(seq+in-flight)로 경합 감지, 데드라인 드레인
  후 재조회. **비공백 회귀 테스트 추가**(settled 강제 true 시 m1 덮어씀 재현).
- **N2** 세 저장 경로의 transport 거부가 error boundary 로 탈출 → catch +
  saveUnconfirmed 안내 + 같은 앱이면 reloadNonce 재조회로 실제 상태 동기화.
- **N3** onSaved 재조회 실패가 체크박스 ON + 빈 팀 데이터 없음 불일치를 남김
  → rollbackIfMissingEmpty(이미 include_empty 데이터가 있으면 일관 유지).
- **N5** KeysTable revokingId 단일 슬롯 → Set<keyId> + catch.
- N4(딥링크 응답이 수동 선택 덮어씀 — 마운트 창 한정, 기존 동작)는 의도적 보류.

## 라운드 8 — CONVERGED (N 4)

- **N1** 마지막 시도가 in-flight 저장과 경합해 구 데이터 적용 가능 → 시도 횟수
  대신 10s 데드라인 + settled 조건으로 교체(로드 중 신규 저장 불가라 유한).
- **N2** saveUnconfirmed 후 재확인 수단 없음 → reloadNonce 이펙트 재트리거.
- **N3** loadFailed 재시도 경로 없음 → 재시도 버튼(common.retry).
- **N4** 기본모델 저장 실패 표면 불일치 → 같은 탭은 인라인 saveError.

## 라운드 9 — CONVERGED (N 1)

- **N1** 토글/웹서치 catch 재조회가 미저장 defaultModelInput·saveError 를
  지움 → 이펙트 리셋을 탭 전환 시로 한정(prevClientForResetRef), 로드 성공
  적용도 allowed_models 포함 입력은 보존(doToggle 과 같은 재동기화 규칙).
  이로써 세 catch 의 재조회 동작이 통일됨.

## 최종 검증 상태

- admin-ui vitest: **441** (38 files) 전부 통과 — AppPolicyPanel 레이스 2건
  (stale 응답, 저장 경합 재조회 — 둘 다 가드 제거 시 실패 확인된 비공백),
  AutoDowngradeConfig·OrgDetailPanel·KeysTable 회귀 포함.
- `tsc --noEmit` 클린, eslint 변경 파일 클린.
- admin-api: `test_model_service.py` 69 통과.
