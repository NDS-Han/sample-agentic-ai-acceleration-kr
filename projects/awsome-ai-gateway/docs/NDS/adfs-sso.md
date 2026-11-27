# ADFS SAML SSO 연동 가이드

Cognito User Pool 에 SAML IdP(ADFS)를 선언적으로 연동하는 절차.
NDS 배포 방식을 따른다 — 설치자가 `gateway.yaml` 을 갱신하면 수렴은 `./deploy apply` 의 표준 경로.

## 배경 — 왜 코드화가 필요한가

콘솔에서 수동으로 SAML IdP 를 등록하고 클라이언트에서 활성화해도,
`supported_identity_providers` 가 terraform 에 `["COGNITO"]` 로 고정돼 있으면
다음 apply 때 되돌아가 hosted UI 의 SAML 버튼이 사라진다. IdP 엔티티는
살아있지만 **로그인 경로가 비활성화**되는 것이다. 이 모듈은 IdP 선언과
클라이언트 활성화를 함께 코드화해 이 drift-revert 를 구조적으로 막는다.

## 사전 준비 (고객사 ADFS 관리자와 협의)

1. ADFS federation metadata URL 또는 XML 파일
2. 게이트웨이용 AD 그룹명 — 파싱 규칙: `<prefix>` 제거 후 나머지를 `_` 로 분해
   - 1세그먼트 → `(Default Department, team)` / 2세그먼트 → `(dept, team)`
   - 예: prefix `ssir_` + 그룹 `ssir_claude_users` → dept `claude`, team `users`
   - **대소문자 구분** — AD assertion 값 · prefix · admin_groups 전부 일치 필요
3. admin 부트스트랩 그룹명 (예: `ssir_claude_admin`)

## 설치

```bash
python3 deployment/scripts/setup-adfs-sso.py setup \
    --provider-name ssir-adfs \
    --metadata-url https://adfs.example.com/federationmetadata/2007-06/federationmetadata.xml \
    --group-prefix ssir_ --admin-group ssir_claude_admin
```

설치자가 하는 일:
1. 메타데이터 fetch + SAML 메타데이터 유효성 검증
2. 풀에 동명 수동 IdP 가 있으면 감지 — adopt(import) 절차 출력 (`--execute-import` 로 직접 실행 가능)
3. `gateway.yaml` 정합 세트 갱신:
   - `infra.cognito_saml.<name>` — IdP 정의 (terraform 리소스)
   - `oidc.groups_claim: custom:groups` — **attribute-mode 필수 연계**
   - `oidc.group_prefix` / `oidc.admin_groups`
4. 고객사 ADFS 관리자용 relying party trust 설정값 출력 (identifier/ACS URL/claim rules)

이후 출력되는 순서대로: `./deploy render` → (수동 IdP 있으면 import) → `./deploy apply` → `./deploy doctor` (saml-idp 점검).

## 고객사 ADFS 측 설정 — 반드시 확인할 함정

설치자가 출력하는 relying party trust 값 외에, claim issuance rule 의 **형식**이
성패를 가른다:

| LDAP attribute | outgoing claim type | 비고 |
|---|---|---|
| `E-Mail-Addresses` | `E-Mail Address` | |
| `Display-Name` | `Name` | |
| **`Token-Groups - Unqualified Names`** | `Group` | ⚠ 아래 함정 참조 |

- ⚠ **SID 함정** — LDAP attribute 를 `Token-Groups`(또는 `as SIDs`)로 선택하면
  그룹명이 아니라 `S-1-5-21-...` 형태의 SID 가 실려 그룹 파싱이 전부 실패한다.
  반드시 `Token-Groups - Unqualified Names` 여야 순수 그룹명(`ssir_claude_users`)이 온다.
  `Qualified by Domain Name` 계열은 `DOMAIN\group` 형태라 prefix 매칭이 깨진다.
- ⚠ **claim URI 정합** — outgoing `Group` 의 URI 는 `http://schemas.xmlsoap.org/claims/Group`
  이다. 이것이 설치자 기본값(`--groups-claim`)과 일치해야 하며, 고객사가 다른 URI
  (예: `http://schemas.microsoft.com/ws/2008/06/identity/claims/groups`)를 쓰면
  설치 시 `--groups-claim` 으로 맞춰야 한다 — attribute mapping 의 값이 이 URI 이다.
- ⚠ **2048자 절단** — custom attribute 는 2048자 제한이 있어, AD 그룹이 많은
  사용자는 목록이 잘린다. 게이트웨이용 그룹만 보내도록 ADFS 에 regex 필터 규칙
  (예: `^ssir_`)을 두는 것을 권장한다.

## 핵심 정합성 — groups_claim

SAML attribute 경로에서 그룹은 토큰의 **`custom:groups`** 클레임에 실린다.
`groups_claim` 이 기본값(`cognito:groups`)이면 로그인 시점에 읽히는 그룹이
없어 `reject_unmatched_groups` 하에서 **전원 403** 이 된다. schema 가 이
조합을 경고하며, 설치자는 항상 정합성 있게 함께 쓴다.

## 동작 구조

- 로그인 화면: hosted UI 에 네이티브 폼 + "Sign in with <IdP>" 버튼 공존 (혼합 기본 지원)
- JIT 프로비저닝: SAML 첫 로그인 시 풀에 federated 사용자 자동 생성, 매 로그인마다 attribute 갱신
- 팀/역할 해석: `custom:groups` attribute → 기존 그룹 파싱 규칙. sync 는 attribute+풀멤버십 이중 경로 backstop
- gateway-cli: 동일 hosted UI/클라이언트라 추가 작업 없음

## AICM 전환 시 cutover (예약 절차)

AICM 이 배포되어 풀 그룹 멤버십이 권위가 되면:
1. `infra.cognito_saml.<name>.attribute_mapping` 에서 `"custom:groups"` 한 줄을 지운다
   → SAML assertion 의 stale AD 그룹이 토큰에서 사라지고 풀 멤버십만 남는다
2. `oidc.groups_claim` 을 `cognito:groups` 로 되돌린다
3. JIT federated 사용자와 AICM pre-provision 사용자의 linking 은
   `COGNITO_FEDERATED_PROVIDER_NAME` 설계가 처리 — 전환 wave 의 검증 항목

## 제거

```bash
python3 deployment/scripts/setup-adfs-sso.py teardown --provider-name ssir-adfs
```

yaml 정리 후 안내되는 env terraform destroy 절차 실행 (managed overlay 는
'켜기'만 소유 — 삭제는 수동 terraform 절차라는 기존 계약과 동일).

## 검증 체크리스트

- [ ] `./deploy doctor` 의 `saml-idp` OK — IdP 등록 + 클라이언트 활성화 수렴
- [ ] hosted UI 에 SAML 버튼 표시 (admin-ui / gateway-cli 동일 화면)
- [ ] SAML 사용자 1명 로그인 — 토큰의 `custom:groups` 에 AD 그룹 존재
- [ ] 팀 배정 정상 + admin 사용자는 ADMIN 역할
