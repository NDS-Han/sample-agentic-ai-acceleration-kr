# AD 연동 (ADFS ↔ Cognito SAML 페더레이션)

회사 AD 계정으로 admin-ui/gateway-cli에 로그인하는 설정입니다. 사용자가 매번
복사되거나 동기화되는 게 아니라, **로그인할 때마다 SAML assertion으로 속성이
전달**됩니다.

```
사용자 로그인 → 브라우저가 ADFS로 이동 → AD 인증
  → ADFS가 서명된 assertion 발급 (email, name, groups 속성)
  → 브라우저가 assertion을 Cognito에 POST (ADFS↔Cognito 직통 통신 없음)
  → Cognito가 JIT 사용자 생성 + custom:groups 속성에 그룹 저장
  → 토큰 발급 → admin-api가 custom:groups로 역할·팀 판별
```

- **첫 로그인에 JIT 생성** — 풀에 미리 사용자가 없는 게 정상입니다
- **AD 그룹 변경은 다음 로그인에 반영** — 동기화 주기 없음
- **풀 Groups에는 안 들어갑니다** — SAML 사용자는 `cognito:groups` 멤버십이
  비어 있고, 그룹은 `custom:groups` **속성**으로만 옵니다 (풀 그룹은 네이티브
  사용자용)

## 역할 결정 — 그룹이 결정하는 것과 아닌 것

| 역할 | 결정 방식 |
|---|---|
| **ADMIN** | `oidc.admin_groups` 정확일치 (또는 `admin_emails`) — **AD 그룹으로 결정** |
| **DEVELOPER** | 기본값 — 로그인만 하면 부여 |
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
2. **Relying Party Trust 등록** — Entity ID:
   `urn:amazon:cognito:sp:<user-pool-id>` / ACS:
   `https://<hosted-ui-domain>.auth.<region>.amazoncognito.com/saml2/idpresponse`
3. **클레임 룰** — 아래 속성을 assertion에 실어 보냄:

   | SAML attribute | 내용 | 주의 |
   |---|---|---|
   | `email` | 메일 주소 | |
   | `name` | 표시 이름 | |
   | `groups` | 사용자의 `SSIR_*` 그룹 목록 | **여러 개면 `;`로 join한 단일 문자열** |

4. **`FederationMetadata.xml` 제공** — AWS 측 IdP 등록에 필요

> 참고: `groups`가 `;` join이 아니라 속성 반복 방식이면 마지막 값만 남을 수
> 있습니다 — join 룰로 보내는지 반드시 확인하세요.

## ② AWS 측 작업 (우리)

아래 값은 예시입니다 — 실제 풀 ID·클라이언트 ID·리전으로 바꿔 실행하세요.

### 1. custom attribute 생성 + ReadAttributes 추가

terraform 모듈(`deployment/terraform/modules/cognito`)은 `custom:groups`
schema와 클라이언트 `read_attributes`를 **이미 선언**하고 있습니다 — 정석
경로는 targeted apply입니다:

```bash
cd deployment/terraform/environments/<env명>
terraform apply -target=module.cognito
# → aws_cognito_user_pool.this(schema) + user_pool_client.cli(read_attributes)
#   두 건 update만 나와야 정상
```

`domain.name`을 쓰는 배포는 `./deploy apply`가 `module.cognito` 타깃 apply를
자동 실행합니다(`deploy.tf_env_dir`이 가리키는 env — Cognito 풀 소유 env와
일치하는지 확인). 수동으로 만들 수도 있습니다:

```bash
aws cognito-idp add-custom-attributes \
  --user-pool-id <user-pool-id> \
  --custom-attributes Name=groups,AttributeDataType=String,Mutable=true \
  --region ap-south-1
```

> ⚠️ **클라이언트 ReadAttributes에 `custom:groups`가 없으면 토큰에 실리지
> 않습니다** — app client가 속성 allowlist를 쓰면(`["email","email_verified",
> "name"]` 등) 여기에 `custom:groups`를 추가해야 합니다. 콘솔로만 만들면
> terraform plan이 되돌리려다 `cannot modify or remove schema items`
> 에러를 냅니다 — 위 targeted apply가 선언·상태를 정합으로 맞춥니다.

### 2. SAML IdP 등록

```bash
aws cognito-idp create-identity-provider \
  --user-pool-id <user-pool-id> \
  --provider-name ADFS --provider-type SAML \
  --provider-details MetadataFile=file://FederationMetadata.xml \
  --attribute-mapping '{"email":"email","name":"name","custom:groups":"groups"}' \
  --region ap-south-1
```

| SAML attribute | User pool attribute |
|---|---|
| `email` | `email` |
| `name` | `name` |
| `groups` | `custom:groups` |

### 3. App client에서 ADFS 활성화

콘솔 권장 — App integration → client `llm-gateway-prod-cli` → Edit → Hosted
UI의 Identity providers에 `ADFS` 체크. (CLI `update-user-pool-client`는 설정
전체를 교체하니 주의. 이 클라이언트는 terraform 소유 — drift 감안해 변수로
반영하면 더 좋음.)

### 4. `gateway.yaml` 선언 → `./deploy apply`

```yaml
oidc:
  groups_claim: custom:groups        # 속성을 그룹 클레임으로 (Lambda 불요)
  group_prefix: SSIR_                # AD 그룹 prefix → 부서/팀 파싱
  admin_groups: [SSIR_Claude_Admin]  # ADMIN 역할 정확일치
```

eks는 `adminApi.oidc.*`/`adminBootstrap` 키로, compose는 env로, ecs는 tfvars로
자동 렌더됩니다 — env values 직접 편집 불요 (`configure` 마법사에도 해당
질문 있음).

### 5. 검증

hosted UI에 `ADFS` 버튼 표시 → 로그인 →
`aws cognito-idp admin-get-user --user-pool-id <user-pool-id>
--username ADFS_<id>`로 `custom:groups` 확인 → 대시보드에서 부서/팀·역할 확인.

## ③ AD 없이 미리 테스트

`custom:groups`는 네이티브 사용자에게도 수동 부여 가능 — ADFS 없이 전체
파이프라인 검증이 됩니다:

```bash
# ① custom attribute 생성 (위와 동일 — 한 번이면 됨)
# ② 테스트 사용자에 속성 부여
aws cognito-idp admin-update-user-attributes \
  --user-pool-id <user-pool-id> --username <test-user> \
  --user-attributes "Name=custom:groups,Value=SSIR_Claude_Developers" \
  --region ap-south-1

# ③ 기존 관리자에도 — groups_claim 전환 후 네이티브 계정이 잠기지 않게
aws cognito-idp admin-update-user-attributes \
  --user-pool-id <user-pool-id> --username <admin-user> \
  --user-attributes "Name=custom:groups,Value=SSIR_Claude_Admin" \
  --region ap-south-1
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
  --region ap-south-1
```

`admin_groups`에 `SSIR_NDS_Admin`을 선언했으면 정확일치로 ADMIN이 유지됩니다 —
관리자가 곧 AD 계정으로 옮길 예정이라 이메일 경로(`admin_emails`)보다 그룹
경로가 일관됩니다. (`admin_emails`를 쓰면 역할만 부여되고 팀 배정은 안 바뀜.)

> eks 참고: `adminBootstrap`은 **선언한 키만** env overlay를 덮습니다 —
> `admin_emails`만 쓰면 env의 `adminBootstrap.groups`(기존 `ClaudeAdmin`
> 부트스트랩)가 그대로 유지되고, `admin_groups`만 쓰면 env의 `emails`가
> 유지됩니다.

## 프로비저닝 경로 주의 — admin-ui는 사용자를 만들지 않습니다

admin-ui 로그인은 `GET /admin/my/profile`로 역할만 조회합니다 — **DB에
없는 사용자는 `user_not_provisioned`(403)가 됩니다.** 사용자 생성 경로:

| 경로 | 프로비저닝 | SAML 사용자 커버 |
|---|---|---|
| `POST /admin/users/sync-cognito` | 풀 그룹 순회 | ❌ 풀 그룹에 없어 못 봄 |
| `POST /v1/auth/exchange` | 자동 생성 (VK 발급) | ✅ |
| admin-ui 로그인 | 없음 | ❌ |

SAML 사용자는 풀 그룹이 없으므로 `sync-cognito`가 잡지 못합니다 — **첫
admin-ui 로그인 전에 프로비저닝이 필요**합니다. 운영 선택지:

- sync가 사용자의 `custom:groups` 속성도 읽도록 확장 (정석 — 미구현)
- admin-ui 첫 로그인 시 provisioning 호출 (미구현)
- 임시: 사용자에게 `/v1/auth/exchange` 호출 한 번 시키거나 관리자가 수동 생성

## 검증된 테스트 결과 (prod, AD 없이)

- 네이티브 사용자에 `custom:groups=SSIR_NDS_Users` 부여 → `USER_PASSWORD_AUTH`
  토큰에 `custom:groups` 클레임 확인 ✅
- `/v1/auth/exchange` → 부서 NDS/팀 Users 자동 생성·배정, role DEVELOPER ✅
- `SSIR_NDS_Admin` 부여된 기존 관리자 → ADMIN 유지 ✅
- `reject_unmatched_groups=true` 하에서 prefix 불일치 그룹은 거부됨

## 운영 참고

- **사용자가 AD에서 삭제되면** — ADFS가 로그인 자체를 거부해 신규 세션은
  차단됩니다. 다만 **기존 토큰은 만료까지 유효**(refresh 기본 30일) —
  즉시 차단이 필요하면:

  ```bash
  aws cognito-idp admin-user-global-sign-out \
    --user-pool-id <user-pool-id> --username ADFS_<id>
  ```

  풀의 `ADFS_<id>` 레코드는 자동 정리되지 않습니다 — 정리하려면
  `admin-delete-user` (housekeeping).
- **네이티브 계정과의 혼용 주의** — `group_prefix`를 `SSIR_`로 바꾸면
  `Claude_*` 풀 그룹은 unmatched가 됩니다. 관리자 계정은 `admin_emails`로
  살리거나 `custom:groups`를 부여하세요.
- **`custom:groups` 대안** — 그룹을 `cognito:groups` 클레임에 넣으려면
  Pre-Token-Generation Lambda가 필요합니다(토큰 발급 전 속성→클레임 변환).
  인프라가 늘고 실패 시 로그인 전체가 멈추니, 이 문서의 속성 방식을
  권장합니다.
