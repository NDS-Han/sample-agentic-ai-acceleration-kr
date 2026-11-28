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
4. ADFS Relying Party Trust 값. **RP Identifier**는 `urn:amazon:cognito:sp:<user-pool-id>`이며,
   **ACS URL**은 `https://<hosted-ui-host>/saml2/idpresponse`입니다. 설치 도구가 환경별 값을 출력합니다.

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
- **형식과 길이**. Cognito가 다중 값을 대괄호로 감싸면 현재 그룹 파서가 양 끝의
  대괄호와 인코딩을 제거하지 않습니다. 게이트웨이용 그룹만 골라 `;`로 연결한
  단일 문자열을 보내고 2,048자 이내인지 확인합니다. 제한을 넘으면 Cognito가 로그인 중
  오류를 반환할 수 있습니다. [AWS 속성 매핑 문서](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-specifying-attribute-mapping.html)

## 핵심 정합성 — groups_claim

SAML attribute 경로에서 그룹은 토큰의 **`custom:groups`** 클레임에 실린다.
`groups_claim` 이 기본값(`cognito:groups`)이면 로그인 시점에 읽히는 그룹이
없어 `reject_unmatched_groups` 하에서 **전원 403** 이 된다. schema 가 이
조합을 경고하며, 설치자는 항상 정합성 있게 함께 쓴다.

Cognito에서 SAML 값을 저장하려면 mutable `custom:groups`와, 로그인에 사용하는 앱
클라이언트의 `WriteAttributes`에 `custom:groups`가 필요합니다. 토큰에서 읽으려면
`ReadAttributes`에도 포함되어야 합니다. 현재 저장소의 Terraform 모듈은
`custom:groups`를 읽기 목록에만 선언합니다. `saml-idp` 점검이 통과해도 실제
클레임 저장과 사용자 등록을 확인해야 합니다. 그룹 값은 권한 결정에 쓰이므로,
쓰기 권한을 변경하기 전에 사용자가 자기 그룹 속성을 수정할 수 없는지 함께 검토합니다.
[AWS 속성 매핑 문서](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-specifying-attribute-mapping.html) ·
[AWS 사용자 속성 변경 API](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_UpdateUserAttributes.html)

## 동작 구조

- 로그인 화면: hosted UI 에 네이티브 폼 + "Sign in with <IdP>" 버튼 공존 (혼합 기본 지원)
- JIT 프로비저닝: SAML 첫 로그인 시 풀에 federated 사용자 자동 생성, 매 로그인마다 attribute 갱신
- 팀/역할 해석: `custom:groups` attribute → 기존 그룹 파싱 규칙. sync 는 attribute+풀멤버십 이중 경로 backstop
- 웹 로그인: 로그인 콜백이 게이트웨이의 팀과 역할을 등록하고 세션을 발급합니다. VK는 `gateway-cli login`에서 발급합니다.

## 혼합 운영 — 네이티브 + federated 사용자 공존

hosted UI 는 네이티브 폼과 SAML 버튼이 공존한다. 그런데 `groups_claim` 은
하나뿐이라 두 종류 사용자의 그룹 경로를 동시에 읽지 못한다:

| 사용자 종류 | 그룹 소스 | 토큰 클레임 |
|---|---|---|
| federated (ADFS) | SAML assertion → attribute | `custom:groups` |
| 네이티브 (풀 직접 생성) | 풀 그룹 멤버십 | `cognito:groups` |

`custom:groups` 모드에서 네이티브 사용자는 로그인 시점에 풀 멤버십을 못
읽는다 (sync 가 이중 경로로 교정하는 backstop 이지만 첫 로그인 판정은 먼저다).

### break-glass 네이티브 계정 절차 (현재 — 코드 변경 없음)

운영 사용자가 federated 인증을 사용하는 환경에서는 초기 부트스트랩과 비상 복구에
네이티브 계정을 사용합니다. 이 계정의 ADFS 모드 호환성을 다음과 같이 확인합니다.

1. 풀에서 해당 네이티브 사용자의 속성에 `custom:groups` 를 수동 설정
   (예: `ssir_claude_admin`) — attribute 는 사용자 종류와 무관하게 읽힌다
2. 해당 계정이 사용하는 앱 클라이언트와 인증 흐름에서 사용자가 그룹 속성을 직접
   변경할 수 없는지 확인합니다.
3. 전환 전에 네이티브 로그인으로 팀과 관리자 역할이 정상인지 확인합니다.

### 구조적 해결 (AICM 전환 프로젝트 범위로 예약) — union 병합

혼합을 설정 없이 닫는 정답은 로그인 프로비저닝이 **두 클레임을 항상
union 으로 읽는 것**이다. 두 소스는 같은 AD 라는 단일 trust root 에서
나오므로 union 에 보안 문제가 없다. 검토하고 기각한 대안:

- 사용자 타입별 분기 (`identities` 클레임) — AICM 전환기에 federated
  사용자가 풀 멤버십을 갖기 시작하면 양쪽을 다 읽어야 해서 부적합
- 복수 클레임 목록 설정 — 설정 가능 = misconfiguration 가능. 정답이
  하나라면 설정으로 뺄 이유가 없다

union 병합의 유일한 함정은 **stale attribute** — cutover 로 attribute
mapping 을 제거하면 갱신이 멈춘 마지막 값이 사용자에게 남는다. 따라서
cutover 절차에는 아래 decommission 규율이 필수다: 소스를 끌 때 데이터도
지운다.

## AICM 전환 시 cutover (예약 절차)

AICM 이 배포되어 풀 그룹 멤버십이 권위가 되면:
1. `infra.cognito_saml.<name>.attribute_mapping` 에서 `"custom:groups"` 한 줄을 지운다
   → SAML assertion 의 stale AD 그룹이 토큰에서 사라지고 풀 멤버십만 남는다
2. **decommission 규율** — 매핑 제거로 갱신이 멈춘 `custom:groups` 의 마지막
   값이 사용자 속성에 남는다. 전체 사용자의 해당 속성을 지우는 일회성
   배치를 같은 절차에서 실행한다 (union 병합 도입 후엔 필수 — 소스를 끌
   때 데이터도 지운다)
3. `oidc.groups_claim` 을 `cognito:groups` 로 되돌린다
4. JIT federated 사용자와 AICM pre-provision 사용자의 linking 은
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
- [ ] 테스트 사용자가 처음 웹 로그인한 뒤 Cognito 사용자 속성과 ID 토큰의 `custom:groups` 확인
- [ ] Sync Cognito 실행 전에 **Users/Teams**에서 팀과 역할 확인, 관리자 계정과 일반 계정을 각각 검증
- [ ] ADFS 그룹 변경 또는 제거 후 재로그인하여 갱신 및 권한 회수 확인
- [ ] `gateway-cli login`에서 별도로 VK 발급 확인
- [ ] 네이티브 비상 계정의 접근 확인
