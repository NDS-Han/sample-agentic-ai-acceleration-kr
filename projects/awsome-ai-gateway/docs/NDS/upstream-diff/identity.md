# 인증·인가와 자격 증명

**한국어** · [English](identity.en.md) · [비교 목차](README.md)

## Cognito와 게이트웨이 사용자

| 과정 | NDS 동작 |
|---|---|
| 관리자·직원 웹 첫 로그인 | 토큰 검증 → `POST /v1/auth/provision` → 사용자·팀·역할 결정 → `/admin/my/profile` 조회. VK 발급은 하지 않으며 등록 실패 시 웹 세션 생성을 거부합니다. |
| CLI 로그인 | `POST /v1/auth/exchange`에서 공통 프로비저닝을 거쳐 직원 VK를 교환합니다. 승인된 STS 경로에도 계정·리전·역할 제한이 적용됩니다. |
| 일괄 Cognito 동기화 | Cognito 그룹과 사용자 전체 목록을 순회해 풀 그룹이 없는 페더레이션 사용자도 매핑합니다. 사용자·그룹 단위 동기화를 제공합니다. |
| 관리자 및 수동 팀 배정 | 정확히 일치하는 관리자 그룹 또는 설정된 검증 이메일을 확인합니다. 수동 배정 출처와 보호된 관리자 권한은 자동 동기화의 영향을 별도로 검사합니다. |

기존 OIDC 교환에 웹 등록을 분리한 변경은 `1ab19b29`, 그룹 클레임 선언은 `033d0632`를 참고합니다. `required_group`과 팀 접두사·미일치 거부 규칙은 사용자 도입 전에 확인합니다. [OIDC 라우터](../../../admin-api/src/app/routers/auth_oidc.py) · [그룹 매핑](../ad-sync.md)

## ADFS SAML과 그룹 전달

`85f608a9`와 `6d382cd4`를 통해 IdP·메타데이터·매핑·provider details를 `infra.cognito_saml`에서 관리합니다. ADFS에는 RP Identifier `urn:amazon:cognito:sp:<pool-id>`와 HTTPS ACS `/saml2/idpresponse`를 별개로 전달합니다. ADFS에서 내보낸 속성을 Cognito의 가변 `custom:groups`로 매핑할 경우, 사용하는 앱 클라이언트에 해당 속성 쓰기 허용이 필요합니다. 비교 종료점의 Terraform 모듈은 `custom:groups`를 읽기 목록에만 선언합니다. 가동 중인 앱 클라이언트와 선언을 함께 검증합니다. [Cognito 앱 클라이언트 정의](../../../deployment/terraform/modules/cognito/main.tf)

SSO 변경은 실제 사용자의 assertion 속성 → Cognito 프로필 → 설정된 JWT 그룹 클레임 → 게이트웨이 등록·역할을 확인합니다. 재로그인 시 빠진 속성이 오래된 값으로 남을 수 있으므로 권한 제거 시험도 수행합니다. 환경이 한 가지 그룹 claim을 선택할 때 비상 네이티브 로그인 계정의 권한 경로도 확인합니다. [ADFS 런북](../adfs-sso.md)

## 권한별 자격 증명

직원 VK는 OIDC/CLI를 사용합니다. 서비스 계정용 장기 VK는 **Agents**에 있으며 서비스 계정의 사람이 쓰는 로그인 경로는 차단됩니다. 일반 `svc-` 토큰과 AICM scoped `svc-` 토큰은 동일한 접두사라도 승인 경계가 다릅니다. dev 로그인은 평가용이며 환경 플래그로 제어합니다. Cognito 로그아웃은 Hosted UI와 로컬 쿠키 정리를 수행합니다. ADFS에서 남은 세션으로 재인증되는지는 IdP 설정을 별도로 확인합니다. [관리 API 비교](admin-api.md) · [운영 인수인계](../op-guide/README.ko.md)
