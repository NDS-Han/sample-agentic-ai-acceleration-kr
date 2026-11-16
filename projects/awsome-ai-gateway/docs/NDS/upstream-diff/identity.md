# 인증·인가 — upstream 대비 변경 전체

upstream은 Cognito OIDC 단일 경로 + 관리자 수동 사용자 등록이었다.
NDS는 **SAML/AD FS·dev 로그인·서비스 계정·JIT 프로비저닝·그룹 매핑**을 추가했다.

## 1. OIDC 로그인 경로 — 분리와 확장

| 경로 | 기능 | upstream 대비 |
|---|---|---|
| `POST /v1/auth/exchange` | OIDC JWT → Virtual Key 교환 (CLI용) | 기존 경로 — `provision_user` 추출로 프로비저닝 공유화 |
| `POST /v1/auth/provision` | JWT 검증→사용자/팀/역할 등록, **VK 미발급** | **신규(PR #4)** — 웹 첫 로그인 JIT 등록 |
| `POST /admin/users/sync-cognito` | 풀 그룹 순회 + 전체 유저 열거 + `custom:groups` 속성 읽기 | 신규 — 배치/정합성 경로 |
| `POST /admin/users/sync-cognito/user|group` | 개별 동기화 | 신규 |

`provision_user`는 exchange의 1-7단계(검증→클레임→Cognito 이메일 enrich→
그룹 게이트→팀 해석→역할→upsert→활성 검사)를 그대로 수행하되 자격 발급을
하지 않는다 — **로그인 시도 자체가 동기화 트리거**라 폴링/트리거 인프라
불요. 레이트리밋 `rl:auth_exchange:{ip}` 공유(10/min).

## 2. SAML / AD FS

- **`custom:groups` 클레임** — Cognito 사용자 속성 + 토큰 클레임 양쪽 처리,
  `OIDC_GROUPS_CLAIM` 설정화
- **풀 그룹이 없는 SAML 사용자** — `_list_all_users` 전체 열거 + 속성 읽기로
  sync 커버, 로그인 시 provision이 토큰 최신 그룹으로 등록
- **`attr_groups` 병합** — SAML 유도 팀이 stale-team 정리에서 보존
- **`team_source='manual'` 보호** — 관리자 수동 배정은 그룹-유도 자동 복귀
  대상에서 제외(migration 0048)

## 3. dev_login · 서비스 계정

- **`features.dev_login`** — OIDC 공존 개발자 로그인(평가용, 무인증 dev
  토큰). oidc 미배선 + 미선언이면 `DEV_LOGIN_ENABLED=false` fail-closed
- **`features.service_keys`** — 장기 서비스키 발급 on/off 분리
- **서비스 계정 장기 VK** (migration 0050) — 사람 로그인 경로로 서비스
  계정이 키를 받을 수 없게 거부(`service_account_login_forbidden`) —
  유저당 ACTIVE 1개 규칙상 장기키를 즉시 죽이는 사고 방지
- **`adminBootstrap` groups/emails** — 선언한 키만 env overlay를 덮는
  부분 적용

## 4. 그룹 → 역할/팀 매핑

`gateway.yaml`의 `oidc` 블록에 그룹→역할/팀 매핑 선언 —
`required_group`, `group_prefix`, `groups_claim`, `reject_unmatched_groups`,
`admin_groups`, `admin_emails`, `vk_ttl_hours` 전부 배포 선언에서 제어.
역할 도출은 ADMIN_EMAILS 매칭 시 **검증된 이메일만** 사용
(`email_verified`·preferred_username 미검증 경로 분리).

## 5. CLI 인증

- STS presigned URL 기반 로그인 — admin-api가 STS URL을 **서버측 https
  fetch**로 검증(SSRF 표면이라 http 변형 거부). `ALLOWED_STS_REGIONS`·
  `ALLOWED_AWS_ACCOUNTS`·`ALLOWED_IAM_ROLES` 화이트리스트
- `cookieToken` = id_token 우선(검증된 세션 토큰), admin-ui 콜백이
  provision → profile 순서로 호출, 실패 시 쿠키 미발급

## 6. 세션·로그아웃

- Cognito **id_token 12h** + Hosted UI 브랜딩
- 로그아웃 시 IdP 세션까지 종료 — Cognito `/logout` + 로컬 쿠키 정리
- 로케일/만료 정규화 — 파싱 불가·만료 세션은 실제 `isSessionExpired`
  계약대로 처리(dev 토큰 보존)
