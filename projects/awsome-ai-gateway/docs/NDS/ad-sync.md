# AD 연동 (ADFS ↔ Cognito SAML 페더레이션)

**한국어** · [English](ad-sync.en.md)

회사 AD 계정으로 admin-ui/gateway-cli에 로그인하는 설정입니다. 사용자가 매번
복사되거나 동기화되는 게 아니라, **로그인할 때마다 SAML assertion으로 속성이
전달**됩니다.

IdP 설정과 배포 순서는 [ADFS SAML 배포 가이드](adfs-sso.md)를 따릅니다.
콘솔의 일반 로그인 절차는 [로그인 가이드](guides/login.md)에서 확인합니다.

```
사용자 로그인 → 브라우저가 ADFS로 이동 → AD 인증
  → ADFS가 서명된 assertion 발급 (email, name, groups 속성)
  → 브라우저가 assertion을 Cognito에 POST (ADFS↔Cognito 직통 통신 없음)
  → Cognito가 federated 사용자 생성 또는 갱신 + 매핑된 custom:groups 저장
  → 토큰 발급 → 웹 로그인 콜백이 게이트웨이 사용자 등록 → 역할·팀 확인
```

- **첫 로그인에 JIT 생성** — 풀에 미리 사용자가 없는 게 정상입니다
- 값이 바뀐 그룹 클레임은 다음 페더레이션 로그인에서 갱신됩니다. IdP가
  클레임을 생략하면 이전 값이 남을 수 있습니다. 그룹 회수 테스트에서 변경과
  생략을 각각 확인합니다. [AWS 속성 매핑 문서](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-specifying-attribute-mapping.html)
- SAML의 AD 그룹 매핑 결과는 사용자 속성 `custom:groups`에 저장됩니다.
  게이트웨이는 환경에 선언된 `oidc.groups_claim`을 읽습니다.

## 역할 결정 — 그룹이 결정하는 것과 아닌 것

| 역할 | 결정 방식 |
|---|---|
| **ADMIN** | `oidc.admin_groups` 정확 일치 또는 검증된 이메일의 `admin_emails` 일치 |
| **DEVELOPER** | 등록 검증을 통과하고 관리자·팀 리더 조건에 해당하지 않는 사용자의 기본 역할 |
| **TEAM_LEADER** | 그룹이 아님 — admin-ui "팀 리더 지정"으로만 부여 (앱 내부 작업) |

팀/부서 배정은 `group_prefix` 규약으로 자동 파싱됩니다:

```
prefix: SSIR_

SSIR_Claude_Admin       → tail "Claude_Admin"      → 부서 Claude, 팀 Admin
SSIR_Claude_Developers  → tail "Claude_Developers" → 부서 Claude, 팀 Developers
SSIR_Platform_Engineers → tail "Platform_Engineers"→ 부서 Platform, 팀 Engineers
```

> ⚠️ 파싱 안 되는 이름(prefix 불일치)은 `reject_unmatched_groups` 정책을
> 따릅니다 — true면 로그인 거부, false면 기본 팀. 부서/팀은 **없으면 자동
> 생성**되니, 첫 로그인 전에 그룹 명명을 확정하세요.

## ① AD 측 작업 (상대방)

1. **그룹 생성·사용자 배정** — `SSIR_<부서>_<팀>` 규약
   (예: `SSIR_Claude_Admin`, `SSIR_Claude_Developers`)
2. **Relying Party Trust 등록**. **RP Identifier (Entity ID)**에는
   `urn:amazon:cognito:sp:<user-pool-id>`를 그대로 등록합니다.
   **ACS (SAML 응답) URL**에는 `https://<hosted-ui-host>/saml2/idpresponse`를 등록합니다.
   Admin 콘솔 주소는 사용자가 로그인 흐름을 시작하는 주소입니다.
3. **클레임 룰** — 아래 속성을 assertion에 실어 보냄:

   | SAML attribute | 내용 | 주의 |
   |---|---|---|
   | `email` | 메일 주소 | |
   | `name` | 표시 이름 | |
   | `groups` | 사용자의 `SSIR_*` 그룹 목록 | **여러 개면 `;`로 join한 단일 문자열** |

4. **`FederationMetadata.xml` 제공** — AWS 측 IdP 등록에 필요

> 여러 값을 받으면 Cognito는 대괄호로 둘러싼 쉼표 구분 문자열로 변환합니다.
> 게이트웨이의 현재 그룹 파서는 `;`와 `,`로 분리하며 대괄호와 인코딩을
> 해제하지 않으므로 ADFS에서 `;`로 연결한 단일 문자열을 전달합니다.
> [AWS 속성 매핑 문서](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-specifying-attribute-mapping.html)

## ② AWS 측 작업 (우리)

1. [ADFS SAML 배포 가이드](adfs-sso.md)의 설치 도구로
   `infra.cognito_saml`과 `oidc.groups_claim: custom:groups`를 선언합니다.
   기존 수동 IdP가 있으면 설치 도구가 안내하는 Terraform 편입 순서를 따릅니다.
2. Cognito IdP의 attribute mapping에서 실제 assertion의 속성 이름 또는 URI가
   `custom:groups`에 연결되는지 확인합니다. 예를 들어 `ds_dm`이 assertion
   **속성 이름**이면 매핑의 원본이고, **그룹 값**이면 현재 팀 접두어와 관리자
   그룹 규칙으로 해석합니다.
3. 로그인에 사용하는 앱 클라이언트의 **ReadAttributes**와 **WriteAttributes**를
   확인합니다. Cognito가 SAML 매핑 값을 저장하려면 `custom:groups`가 mutable이며
   **WriteAttributes**에 포함되어야 합니다. **ReadAttributes**는 토큰에서
   해당 값을 읽는 데 필요합니다. 현재 저장소 Terraform 모듈은 읽기만 선언합니다.
   실제 앱 클라이언트 설정과 선언 상태를 확인하고, 사용자 자신의 그룹 수정이
   허용되지 않는지 검토한 뒤 SAML 오픈 여부를 판단합니다.
   [AWS 속성 매핑 문서](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-specifying-attribute-mapping.html) ·
   [AWS 사용자 속성 변경 API](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_UpdateUserAttributes.html)
4. `./deploy doctor`에서 SAML IdP 등록과 클라이언트 활성화를 점검한 뒤 ADFS
   테스트 계정으로 웹에 로그인합니다. Cognito 사용자 레코드의 `custom:groups`,
   웹 로그인에 사용한 ID 토큰의 그룹 클레임, **Users/Teams**의 팀과 역할을
   순서대로 검증합니다. 토큰 원문은 공유하지 않습니다.

## ③ AD 없이 미리 테스트

`custom:groups`는 네이티브 사용자에게도 수동 부여 가능 — ADFS 없이 전체
파이프라인 검증이 됩니다:

```bash
# ① custom attribute 생성 (위와 동일 — 한 번이면 됨)
# ② 테스트 사용자에 속성 부여
aws cognito-idp admin-update-user-attributes \
  --user-pool-id <user-pool-id> --username <test-user> \
  --user-attributes "Name=custom:groups,Value=SSIR_Claude_Developers" \
  --region <region>

# ③ 기존 관리자에도 — groups_claim 전환 후 네이티브 계정이 잠기지 않게
aws cognito-idp admin-update-user-attributes \
  --user-pool-id <user-pool-id> --username <admin-user> \
  --user-attributes "Name=custom:groups,Value=SSIR_Claude_Admin" \
  --region <region>
```

→ `gateway.yaml` 반영·apply 후 테스트 계정으로 로그인 → 부서 Claude/팀
Developers 생성·배정 확인 → 관리자는 ADMIN 유지 확인.

## 기존 관리자 계정 보존 (AD 전환 시)

`groups_claim`을 `custom:groups`로 바꾸면 admin-api는 **속성만** 봅니다 — 풀
그룹(`ClaudeAdmin` 등) 멤버십은 더 이상 안 보입니다. 기존 네이티브 관리자가
잠기지 않게 전환 전에 속성을 부여하세요:

```bash
aws cognito-idp admin-update-user-attributes \
  --user-pool-id <user-pool-id> --username <기존관리자> \
  --user-attributes "Name=custom:groups,Value=SSIR_NDS_Admin" \
  --region <region>
```

`admin_groups`에 `SSIR_NDS_Admin`을 선언했으면 정확일치로 ADMIN이 유지됩니다 —
관리자가 곧 AD 계정으로 옮길 예정이라 이메일 경로(`admin_emails`)보다 그룹
경로가 일관됩니다. (`admin_emails`를 쓰면 역할만 부여되고 팀 배정은 안 바뀜.)

> eks 참고: `adminBootstrap`은 **선언한 키만** env overlay를 덮습니다 —
> `admin_emails`만 쓰면 env의 `adminBootstrap.groups`(기존 `ClaudeAdmin`
> 부트스트랩)가 그대로 유지되고, `admin_groups`만 쓰면 env의 `emails`가
> 유지됩니다.

## 웹 로그인 사용자 등록

admin-ui 로그인 콜백은 토큰 검증 후 `POST /v1/auth/provision`으로
사용자·팀을 등록하고 `GET /admin/my/profile`로 유효 역할을 조회합니다.
등록 경로는 기존 OIDC JWT 검증·그룹 매핑·비활성 사용자 거부 규칙을 공유하며,
Virtual Key를 발급하지 않으므로 웹 로그인으로 CLI 키가 교체되지 않습니다.
사용자는 팀 매핑용 그룹과 필요한 관리자 그룹에 가입되어 있어야 합니다.

| 경로 | 사용자 등록 | 키 발급 | SAML 사용자 커버 |
|---|---|---|---|
| `POST /admin/users/sync-cognito` | 풀 그룹 순회 **+ 전체 유저 목록** — 각 유저의 `OIDC_GROUPS_CLAIM` **속성**(custom:groups)도 읽어 팀·역할에 반영 | 없음 | ✅ (`groups_claim: custom:groups` 설정 시) |
| `POST /v1/auth/provision` | 검증된 토큰의 그룹 기반(JIT — 웹 로그인 콜백이 자동 호출) | 없음 | ✅ (토큰의 그룹 클레임) |
| `POST /v1/auth/exchange` | 검증된 토큰의 그룹 기반 | 있음(VK) | ✅ |

두 경로는 역할이 다릅니다 — **sync = 배치/정합성**(전체 열거·attr_groups
병합·stale 정리), **provision = JIT**(첫 로그인 자가 등록 — sync 실행을
기다리지 않음). SAML 사용자는 풀 그룹이 없어도 **전체 유저 열거
(`_list_all_users`)에 잡히고** 그 `custom:groups` 속성으로 팀이 해석되며,
로그인 시엔 provision이 토큰의 최신 그룹으로 등록합니다.

기존 버전에서 로그인한 사용자가 `user_not_provisioned`(403)를 받으면
로그아웃 후 다시 로그인해 등록 단계를 실행하세요. 등록이 실패하면
콜백에서 오류를 표시하며 새 로그인 세션을 발급하지 않습니다.

## 네이티브 계정을 사용한 사전 테스트 결과

아래 기록은 SAML IdP를 거치지 않고 수행한 테스트입니다. ADFS에서 실제 그룹
값이 전달되는지는 별도로 검증합니다.

- 네이티브 사용자에 `custom:groups=SSIR_NDS_Users` 부여 → `USER_PASSWORD_AUTH`
  토큰에 `custom:groups` 클레임 확인 ✅
- `/v1/auth/exchange` → 부서 NDS/팀 Users 자동 생성·배정, role DEVELOPER ✅
- `SSIR_NDS_Admin` 부여된 기존 관리자 → ADMIN 유지 ✅
- `reject_unmatched_groups=true` 하에서 prefix 불일치 그룹은 거부됨

## 운영 참고

- AD에서 계정이 해지되면 신규 ADFS 로그인은 차단됩니다. 기존 Cognito 세션과
  이미 발급된 게이트웨이 VK는 별도로 확인합니다. Cognito 세션 정리에는 다음
  명령을 사용합니다.

  ```bash
  aws cognito-idp admin-user-global-sign-out \
    --user-pool-id <user-pool-id> --username ADFS_<id>
  ```

  즉시 게이트웨이 접근을 중단해야 하면 **Users/Teams**에서 계정 상태를 확인하고
  활성 VK가 폐기됐는지 확인합니다. 풀의 federated 사용자 레코드 정리는 별도
  계정 수명주기 작업입니다.
- **네이티브 계정과의 혼용 주의** — `group_prefix`를 `SSIR_`로 바꾸면
  `Claude_*` 풀 그룹은 unmatched가 됩니다. 관리자 계정은 `admin_emails`로
  살리거나 `custom:groups`를 부여하세요.
- **`custom:groups` 대안** — 그룹을 `cognito:groups` 클레임에 넣으려면
  Pre-Token-Generation Lambda가 필요합니다(토큰 발급 전 속성→클레임 변환).
  인프라가 늘고 실패 시 로그인 전체가 멈추니, 이 문서의 속성 방식을
  권장합니다.
