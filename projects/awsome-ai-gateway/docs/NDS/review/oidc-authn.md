# 리뷰: OIDC/Cognito/AD 연동 + 로그인 경로

**범위**: `deploy/schema.py` 의 `oidc.*`/`features.dev_login`/`service_keys`,
`deploy/render/eks.py`·`render/compose.py` 의 OIDC env·DEV_LOGIN_ENABLED,
`deploy/eks_apply.py` 의 cognito 자동 배선·`onboard_admin`, `deploy/cli.py`
의 wizard OIDC 섹션, `_sanitize_copied_values` 의 OIDC 중화.

**판정: 수렴**

## 핵심 계약

- OIDC 설정 시 admin-api 의 삼중가드가 dev 토큰을 **무조건 거부** —
  `dev_login` 선언과 공존해도 동작 불가 → 양쪽 모두 `DEV_LOGIN_ENABLED=false`
  로 수렴(15차: 기존 "공존" 테스트가 실제로는 반쪽 상태였음을 계약 정정).
- `oidc.audience` 미기입 → `client_id` 기본 (JWT aud 검증 비활성화 방지).
- `features.dev_login` 미선언 + oidc 비활성 → 로그인 경로 없음
  (fail-closed, schema+compose notes 경고).

## 발견·수정된 결함

### HIGH

- **dev values 사본의 dev 풀 신뢰** (12~15차): `scaffold` 가 dev env
  values 를 복사할 때 `enabled: true`+`issuerUrl` 이 살아남으면 신 env 가
  dev Cognito 풀의 토큰을 신뢰(누출 방향) → `_sanitize_copied_values` 가
  oidc 직계 `enabled` 만 정확히 중화(들여쓰기 백레퍼런스로 손자·형제
  오조작 차단, CRLF/주석 삽입/flow 스타일/YAML 1.1 변형 대응, 재실행
  멱등).
- **onboard_admin 대상 어긋남** (19차): gen 레이어 실효값(`oidc.admin_
  emails`)이 아닌 env 파일 이메일로 Cognito 계정 생성 → 실관리자 로그인
  불가 + 구주소 권한 계정 잔여.
- **allowed_roles wizard 소실** (19차): configure 가 fail-closed 키를
  버려 다음 apply 가 로그인 전량 403.

### MEDIUM

- **OIDC 자동 배선**: `cognito_client_id` 없으면 SystemExit(명시 실패),
  클라이언트 secret 은 Secrets Manager/ESO 경로로만 주입(helm values
  리터럴 금지).
- **capture OIDC 복원**: helm values 에 리터럴이 없는 필드(ESO 경로)는
  terraform output 의 cognito_* 로 복원 — client_secret 은 캡처하지 않음.
- **sanitize 세부**: `allowedIamRoles: [a]` flow/후행주석, `enabled: yes`/
  `'true'`, issuerUrl 재실행 주석 이중부착, `AGENTCORE_REGION: "us-east-1"`
  (websearch 커넥터 상수) 오치환 → 전부 보정.
- **admin-ui env 계층 정합**: `oidcEnv` 정식 키 경유로 중복 env 함정 회피.

## 회귀 테스트

sanitize 실 dev 파일 검증(마커 0건, us-east-1 보존, oidc 재정렬/손자/
형제 케이스), dev_login 억제 회귀, configure 보존.

## 잔여 리스크

- 희귀 YAML 형태(flow oidc 블록·탑레벨 oidc)는 sanitize 커버 밖 — 수동
  검토 필요(LOW).
- AD/SAML 은 OIDC 계약 위에서 동작 — 실 AD IdP 연동 검증은 라이브 환경
  필요.
