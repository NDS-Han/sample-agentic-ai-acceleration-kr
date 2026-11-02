# AD Sync — ADFS(SAML) ↔ Cognito 연동

> 사내 AD 계정으로 LLM Gateway 로그인을 하게 하는 절차입니다. **디렉토리 동기화가
> 아니라 로그인 위임(SAML federation)**입니다 — 사용자·비밀번호·그룹은 AD에만
> 존재하고, 로그인 순간 브라우저가 SAML assertion을 운반합니다. ADFS ↔ Cognito
> 간 직접 통신은 없습니다(메타데이터도 파일 업로드).
>
> 첫 로그인 시 Cognito가 JIT로 연동 사용자를 만들고(`ADFS_<NameID>`), 이후
> 로그인마다 최신 assertion 값으로 갱신합니다. AD에서 비활성화하면 ADFS 단계에서
> 바로 차단됩니다.

```
사용자 → admin-ui / gateway-cli → Cognito hosted UI → "ADFS" 버튼
  → 사내 ADFS 로그인 → SAML assertion (email + Claude_* 그룹)
  → Cognito → Cognito JWT (cognito:groups 채움) → 앱 콜백
```

## 전제

- Cognito hosted UI 로그인이 이미 동작 중일 것 (도메인 또는 selfsigned-temp —
  [domain.md](domain.md))
- admin-ui는 `Claude_` prefix 그룹으로 역할을 판별합니다
  (`OIDC_GROUPS_CLAIM=cognito:groups`, `REJECT_UNMATCHED_GROUPS=true`)

## ① AD 측 작업 (ADFS 담당자에게 요청)

1. **AD 그룹 생성** — Cognito 풀 그룹과 **동일 이름**, `Claude_` prefix 필수:
   - `ClaudeAdmin` — 어드민
   - `Claude_NDS_Developers` / `Claude_SSIR_Developers` — 개발자
   - 사용자를 역할에 맞는 그룹에 배정
2. **Relying Party Trust 등록** (Cognito 용):
   - Entity ID: `urn:amazon:cognito:sp:<user-pool-id>`
   - ACS / SSO URL: `https://<cognito-domain>/saml2/idpresponse`
   - prod 예: `urn:amazon:cognito:sp:ap-south-1_htFYPGP0m`,
     `https://llm-gateway-prod-ssir-auth-796378120371.auth.ap-south-1.amazoncognito.com/saml2/idpresponse`
3. **클레임 룰**:
   - email → `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress`
   - name → `.../claims/name`
   - 그룹 → `Claude*` 그룹만 골라 **하나의 문자열로 join** (`;` 구분) 해서
     `groups` 라는 이름의 attribute로 전송 — SAML multi-value는 Cognito가
     제대로 못 받는 경우가 있어 단일 문자열이 확실합니다
4. **`FederationMetadata.xml` 제공** —
   `https://<adfs>/FederationMetadata/2007-06/FederationMetadata.xml` 에서 다운로드

## ② AWS 측 작업 (우리)

1. **SAML IdP 등록** — User pool → Federated sign-in → Add IdP → SAML →
   메타데이터 파일 업로드, name=`ADFS`. CLI:

   ```bash
   aws cognito-idp create-identity-provider \
     --user-pool-id <pool-id> --provider-name ADFS --provider-type SAML \
     --provider-details "MetadataFile=<xml>" \
     --attribute-mapping '{"email":"http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress","name":"http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name","custom:groups":"groups"}'
   ```

   `custom:groups` 매핑을 쓰려면 풀에 custom attribute `groups`(문자열)를
   먼저 추가해야 합니다 (콘솔: Sign-up experience → Custom attributes).

2. **app client에 IdP 활성화** — `SupportedIdentityProviders`에 `ADFS` 추가.
   ⚠ client는 terraform env 소유입니다 — 콘솔로 바꾸면 다음 `terraform apply`가
   되돌리니, 가능하면 모듈의 client 리소스에 `supported_identity_providers`를
   추가해서 반영하세요.

3. **그룹 브릿지 — Pre-Token-Generation Lambda** — SAML의 그룹 attribute는
   `cognito:groups`에 자동으로 안 들어갑니다(풀 로컬 그룹 전용). 아래
   Lambda를 Pre-Token-Generation 트리거(V2)로 붙이면 토큰의 `cognito:groups`가
   AD 그룹으로 채워집니다:

   ```python
   def handler(event, context):
       raw = event["request"]["userAttributes"].get("custom:groups", "")
       groups = [g for g in raw.split(";") if g.startswith("Claude_")]
       event["response"]["claimsAndScopeOverrideDetails"] = {
           "groupOverrideDetails": {"groupsToOverride": groups}}
       return event
   ```

   소수 어드민만 쓴다면 대안: JIT 사용자 생성 후 콘솔에서 수동으로 풀 그룹
   배정 — 신규 사용자마다 반복이 필요합니다.

## ③ 검증

1. hosted UI(또는 admin-ui)에서 **ADFS 버튼**이 보이는지 확인
2. AD 계정으로 로그인 → 콜백 정상 복귀 → 풀에 `ADFS_<id>` 사용자 생성 확인
3. 발급된 JWT의 `cognito:groups`에 `Claude_*`가 있으면 역할 판별 완료
   - 로그인은 되는데 권한 거부 → 그룹 클레임/매핑 문제 (③-1 확인)
4. gateway-cli 로그인도 같은 hosted UI라 별도 설정 없이 동작합니다

## 주의

- 토큰 발급 후 AD 그룹을 바꿔도 **다음 로그인/토큰 갱신 때** 반영됩니다
- SAML 사용자의 username은 `ADFS_<NameID>` — audit/사용자 목록에서 이
  접두어로 구분됩니다
- IdP·client 변경은 terraform 관리 밖 작업이라 drift가 생길 수 있습니다 —
  cognito 모듈에 `aws_cognito_identity_provider` 등 리소스로 옮기는 것이 정석
