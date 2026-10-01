# Plan: `/users` S2 후속 수정 — Opus 구현 리뷰 버그 15건

작성일: 2026-09-30 · 대상 커밋: `3147c9e` (S2 섹션 구조) · 리뷰어: Opus 5.5 (kiro-cli, --agent-engine v1)

## 현재 상태

| 항목 | 상태 |
|---|---|
| Phase 0 dead code | 완료 `0cd0f62` |
| S1 안전장치 (`?node=`, key 리마운트, dirty-guard, 취소가드) | 완료 `9a8873f` |
| S2 섹션 구조 (PolicySection, 배지, 통합 Apply, 무제한 표현) | 완료 `3147c9e` — **아래 버그 존재** |
| **S3 수정 (F1–F15 전부)** | **구현 완료 — 커밋 대기** |
| 의미 결정 | **Option A 승인** — "체크한 목록을 그대로 저장" |
| 검증 | tsc 통과, vitest 317 통과, lint 경고만(기존), build 통과 |

### S3 구현 메모 (2026-10-01)

- **Option A 확정**: 유저/팀 앱 접근에서 전체 체크 = 명시 `[3개]` 목록 저장. `[]`(0행)은 모든 앱 접근 축에서 "이 레벨 정책 없음" = 상위 상속(user·team) / 무제한(org). 백엔드 `replace_for_user([])` = 행 삭제 = 상속 — deny-all 로 해석되는 경로는 없음(확인: `allowed_client_repository.py`, `effective_policy_service.py:117-160`, `key_service.py:219` `client_rows if client_rows else None`).
- **F1** TeamPanel에 `catalogStatus('loading'|'ready'|'failed')` 추가 — 카탈로그 미준비 시 `TeamModelPermissionPanel` 미마운트, 실패 시 섹션에 오류 표시. 자식 `save()`도 `catalogReady` 가드.
- **F2** `toSaved`·로드 모두 `activeSet` 멤버십으로 정규화 — 비활성/외부 alias 자연 정리, phantom dirty 제거.
- **F3** `loadFailed` 상태 분리 — 실패 시 loaded=false 유지 + 토스트 + 섹션 오류 텍스트, 요약은 loaded:false.
- **F4** `selectedToClients` = 체크한 목록 그대로(전체=명시 [3개], 빈=[]=상속). 저장 후 `[]`면 effective-policy 재조회 결과(팀/조직 상속 목록)로 체크 상태를 다시 채움.
- **F5** `ScopeAppAccessPanel`에 `orgScopeId` prop — 팀 자체 행 없으면 조직 정책 조회 → 상속 목록 프리필 + 유효 배지 보고. 조직 조회 실패 시 배지 미표시(확정 오표기 방지), 편집 자체는 가능.
- **F6** 0개 체크 = "이 레벨 정책 해제(상속/무제한)"로 통일 + 스코프별 amber 힌트(`appAccess.emptyHint`, `scopeAppAccess.emptyHint{Team,Org}`). 팀 모델만 예외 — 0=전체와 저장값 동일해 모호하므로 저장 차단+힌트 유지.
- **F7** 토글 `disabled={busy || !loaded}` — 로드 실패 시 편집 불가.
- **F8** `!clientsLoaded` early-return 제거 — `accessDirty`가 `clientsLoaded` 내포, 앱 로드 실패가 모델 저장을 막지 않음.
- **F9** 모델 저장 실패 분기에서도 effective-policy 재조회 + 앱 표시 동기화. 표시 폴백은 p2 → 전체 카탈로그(stale policy 제거).
- **F10** policy 미로드 + 자체 행 없음 → `policyState.inherit`(상속) neutral 배지. 유효 목록이 카탈로그 전체를 덮으면 "제한 없음" 표시.
- **F11** `!dirty` 시 `failedSection` 자동 해제 — 두 패널 모두.
- **F12** `ref.current?.save()` 가 null 이면 해당 섹션 실패로 명시 처리(조용한 스킵 아님) — 기존 코드가 이미 `!undefined`=true 분기로 failedSection 마킹.
- **F13** `allAliasKey` = `JSON.stringify(activeAliases)`(구분자 충돌 제거). 카탈로그 게이트(F1)로 미저장 소실 시나리오도 해소.
- **F15** PolicySection docstring 정정 — 기본 eager mount(배지가 자식 fetch 의존), `lazyMount`는 명시적 opt-in + 헤더 배지는 부모 조회 필요 경고.

### S3에서 발견·수정한 추가 결함

- **테스트 무한 루프**: `OrgDetailPanel.test.tsx`의 `useTranslations` mock이 매 렌더 새 `t`를 반환 → effect deps의 `t`로 무한 재실행. 실제 next-intl의 `t`는 안정 참조이므로 mock을 모듈 스코프 상수로 교체(컴포넌트 코드는 deps 정합 유지).
- **`models.loadFailed` i18n 키 누락** — en/ko 추가.

### 미해결 (다음 사이클)

- `/models` 페이지의 `TeamModelPermissionPanel` 잔존 — Phase 2(todo #3)에서 제거 예정.
- F7 후속 UX: 앱 로드 실패 시 비활성 토글 자리에 섹션 내 에러 텍스트(현재는 토스트만).
- **dev 전용 이슈**: dev 서버에서 유저 노드 클릭 시 간헐적으로 `/users` 라우트가 loading.tsx 상태로 멈춤 (동일 `?node=`에 대한 RSC POST 반복). prod 빌드(`next start`)에서는 동일 시나리오가 재현되지 않아 dev 환경 한정으로 판정 — 배포 후 스모크에서 재확인 필요.

## ★ 핵심 의미 교정 (리뷰에서 발견 — 배경 설명이 틀렸음)

앱 접근(allowed_clients)의 폴백 체인은 **user → team → org → none**
(`admin-api/src/app/services/effective_policy_service.py:117-160`):

- 유저 `[]` = 팀/조직 상속 (무제한 아님)
- **팀 `[]` = 조직 상속** (무제한 아님)
- 조직 `[]` = 무제한
- 팀 allowed_models `[]` = 무제한 (모델에는 org 단계 없음)
- 유저 allowed_models `[]` = override 없음(팀 상속), DELETE와 동일

즉 S2에서 "팀 앱 접근 [] = 제한 없음"으로 배지를 표시한 것이 오표기 — 상속된 조직 제한이 있으면 틀린다.

## 수정 목록 (리스크 순)

### 높음 — 잘못된 데이터 저장

- [ ] **F1** `TeamModelPermissionPanel`: 카탈로그 미도착/실패 시 `toSaved()`가 무조건 `[]` 반환
  - `OrgDetailPanel.tsx` TeamPanel이 `teamModels=[]`로 자식을 즉시 마운트(실패 시 토스트도 없음)
  - `toSaved` (`TeamModelPermissionPanel.tsx:75-76`): `sel.length >= activeAliases.length` → 빈 카탈로그면 항상 `[]`
  - 증상: 카탈로그 실패 시 phantom dirty → Apply 누르면 팀 제한이 무제한으로 **지워짐**
  - 수정: TeamPanel에 카탈로그 상태(`loading|ok|error`) 추적 → `ok` 전엔 패널 미마운트, `error`면 섹션에 오류 표시. 자식 `save()`도 `models.length===0`이면 차단.
- [ ] **F2** `TeamModelPermissionPanel`: 전체 체크 판정이 멤버십이 아닌 개수 비교
  - 저장 목록에 비활성/외부 alias 섞이면 로드 직후 phantom dirty + 저장 시 `[]`
  - 수정: `activeSet` 멤버십으로 정규화 — 로드 시 `saved.filter(a => activeSet.has(a))`를 `loadedAliases`·`selected` 양쪽에 적용(비활성 잔재는 다음 저장 시 자연 정리). `toSaved`도 카탈로그 내 멤버만 비교.
- [ ] **F3** `TeamModelPermissionPanel`: 로드 실패를 성공으로 처리
  - `setLoaded(true)`가 success 분기 밖(`:95`), 실패 시 토스트 없음 → `!loaded` 가드가 절대 안 걸림 + 요약이 `{loaded:true,restricted:false}`로 헤더에 "제한 없음"
  - 수정: `loadFailed` 상태 분리 — 성공 시만 loaded, 실패 시 토스트 + 섹션에 오류 표시, 요약은 loaded:false.
- [ ] **F4** `UserPanel` 앱 접근: 전체 체크 저장 → `[]` → 상속인데 성공 토스트
  - `selectedToClients`(`OrgDetailPanel.tsx:155`)가 전체 체크를 `[]`로 변환 → 팀 `[cc]` 상속 유저에 전체 체크해도 팀 제한 그대로인데 "저장됨"
  - 저장 후 `selected=전체체크`인데 p2 배지는 "1개 제한·팀"으로 모순
  - ★ 결정 필요(아래 "의미 결정" 참조): 제안 = **체크한 목록을 그대로 저장** — 상속 단계에서 전체 체크는 명시 `[3개]` 목록으로 저장(명시적 전체허용 override 표현 가능, 백엔드 변경 불필요). 0개 체크 = 상속으로 통일 + 힌트 표시.

### 중간 — 배지 오표기 / 의미 불일치

- [ ] **F5** `ScopeAppAccessPanel`(팀): 배지가 조직 상속 제한을 "제한 없음"으로 표기 (`:115-121`, 주석 `:26`도 폴백 규칙과 어긋남)
  - 수정: 팀 스코프는 조직 정책도 조회(`getScopeAllowedClientsAction('organization', ...)`) — 자체 행 없으면 조직 목록 프리필 표시 + 요약은 유효 상태 보고
- [ ] **F6** 0개 체크 의미가 패널마다 다름: 팀 모델=저장 차단, 앱 패널=무음 상속, 유저 모델=상속
  - 수정: 의미 통일 — **빈 선택 = 이 레벨 정책 없음(상위 상속 또는 무제한)**. 스코프별 동적 힌트 문구 추가. `TeamModelPermissionPanel`의 "앱 접근 패널과 같은 규칙" 주석 정정(같지 않음).
- [ ] **F7** `ScopeAppAccessPanel`: 로드 실패 시 토글은 활성 + dirty 조건이 `loaded &&`라 Apply 바가 안 뜸 → 클릭해도 무반응
  - 수정: 토글에 `disabled={!loaded}` 추가
- [ ] **F8** `UserPanel`: `if (!clientsLoaded) return`(`:315`)이 accessDirty 무관하게 먼저 실행 → 모델만 수정해도 저장 전면 차단
  - 수정: `accessDirty = clientsLoaded && ...`라 앱 저장은 어차피 못 탐 — early return 제거(또는 accessDirty 안으로 이동)
- [ ] **F9** `UserPanel`: 모델 저장 실패 시 policy 미재조회 → 앱 출처 배지 stale / override 해제 후 p2 실패 시 stale `policy.allowed_models`(=삭제한 override)로 표시 복원
  - 수정: 모델 실패 분기에서도 `getEffectivePolicyAction` 재조회. 표시 폴백에서 stale policy 제거 → `p2 → models.map(전체)` 순으로.
- [ ] **F10** `UserPanel`: policy 로드 실패 시 상속 불명인데 배지가 "제한 없음"으로 확정 표기
  - 수정: `!policy && 자체 행 없음`이면 `policyState.inherit`(상속, neutral) 배지 표시 — 자체 행 여부 플래그 필요(앱: `hasOwnAppPolicy`, 모델: `loadedModelAliases.length>0` 재사용). i18n 키 추가(en "Inherited" / ko "상속").

### 낮음 — UX/계약

- [ ] **F11** `failedSection`이 Apply 재시작 전까지 해소 안 됨 — 되돌리기로 dirty 사라져도 빨간 배지·sticky 스트립 잔존
  - 수정: `useEffect(() => { if (!dirty) setFailedSection(null) }, [dirty])` 두 패널 모두
- [ ] **F12** `ref.current?.save()`가 null이면 `undefined` → `!undefined`=true → 조용히 실패 처리(토스트 없음). 저장 중 노드 전환 시 앱만 저장되고 모델 저장 스킵 가능
  - 수정: `(await ref.current?.save()) !== true`로 명시적 실패 처리
- [ ] **F13** `allAliasKey = activeAliases.join('')` — 구분자 없어 충돌 가능(`["ab","c"]` vs `["a","bc"]`). 카탈로그 변경 시 `setSelected([])`로 미저장 편집 무통보 소실
  - 수정: `JSON.stringify(activeAliases)`로. 카탈로그 로드 후에만 fetch하는 구조로 바뀌면(F1) 자연 해소
- [ ] **F15** `PolicySection` docstring "최초 펼침 시 lazy mount" — 실제 기본은 eager mount(`:44`). 문구 정정

### 스킵 판정 (Opus도 확인된 정상)

- F14 sticky 스트립과 Apply 바 칩의 정보 중복 — 허용 범위, 유지
- UserPanel의 unmount 후 저장 완료 + 성공 토스트 — 허용

## 의미 결정 (F4·F5·F6 공통 — 사용자 승인 대기)

**제안 (Option A — "체크한 목록을 그대로 저장"):**

| 레벨 | 빈 선택 | 부분 선택 | 전체 선택 |
|---|---|---|---|
| 유저 앱/모델 | `[]` → 팀 상속 (override 해제) | 명시 목록 | **명시 전체 목록** (상속 끊고 전체 허용) |
| 팀 앱 | `[]` → 조직 상속 | 명시 목록 | **명시 전체 목록** |
| 팀 모델 | `[]` → 무제한 | 명시 목록 | `[]` → 무제한 (리프 정규화) |
| 조직 앱 | `[]` → 무제한 | 명시 목록 | `[]` → 무제한 (리프 정규화) |

- "전체 체크 = 명시 목록"이면 "팀은 제한하지만 이 유저만 전부 허용"이 표현 가능(백엔드 변경 없음 — 행에 전체 id 저장)
- 배지: 유효 목록이 카탈로그 전체를 덮으면 "N개 제한"이 아니라 "제한 없음" 표시(명시적 전체허용 override 구분)
- 대안 (Option B — 현행 유지): 전체 체크→`[]`=상속. 간단하지만 명시적 전체허용 override 표현 불가
- 0개 체크는 모든 패널에서 허용하되 스코프별 힌트("모두 해제 = 팀/조직 정책 상속" 또는 "= 제한 없음"). 단 팀 모델은 0=전체와 저장값이 같아 모호 — 차단+힌트 유지할지 통일(허용+힌트)할지 선택

## 관련 참고

- 상속 폴백 근거: `admin-api/src/app/services/effective_policy_service.py:117-160`
- 팀 모델 `[]`=무제한 근거: `model_repository.py` `set_for_team`, `team_allowed_model_service.py`
- Opus 정상 확인: PolicySection autoOpen/unmount, Apply 순차저장·disable 전파·자식 토스트 억제, key 리마운트·cancelled 가드, i18n 키 존재, tsc 통과
- 검증: tsc + vitest + `next lint --file` + 수동 시나리오(팀 제한→카탈로그 실패 시 Apply 안 뜨는지, 유저 앱 전체체크 저장 후 배지·체크 상태 일치)

## 구현 순서 제안

1. F1+F2+F3 (TeamModelPermissionPanel 데이터 보호) — 카탈로그 게이트 포함
2. F8+F9 (UserPanel 저장 경로) — 의미 결정 전에도 무해
3. F11+F12+F13+F15 (저위험 정리)
4. F4+F5+F6+F7+F10 (의미 결정 후 일괄 — ScopeAppAccessPanel 조직 조회 포함)
