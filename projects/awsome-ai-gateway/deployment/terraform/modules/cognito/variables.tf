# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type        = string
  description = "Project prefix (예: dslg)"
}

variable "environment" {
  type        = string
  description = "환경 식별자 (dev | prod)"
}

variable "aws_region" {
  type        = string
  description = "AWS region (issuer URL 빌드에 사용)"
}

variable "domain_suffix" {
  type        = string
  description = "Hosted UI 도메인 prefix suffix. 최종 도메인: {project}-{env}-{suffix}.auth.{region}.amazoncognito.com (전 세계 unique 해야 함)"
  default     = "auth"
}

variable "callback_urls" {
  type        = list(string)
  description = "OIDC redirect URI 화이트리스트. gateway-cli 가 사용. 기본: localhost callback (PKCE)"
  default = [
    "http://localhost:8090/callback",
    "http://localhost:8091/callback",
    "http://localhost:8092/callback",
  ]
}

variable "logout_urls" {
  type        = list(string)
  description = "OIDC logout redirect URI 화이트리스트"
  default = [
    "http://localhost:8090/logout",
    "http://localhost:8091/logout",
    "http://localhost:8092/logout",
  ]
}

variable "groups" {
  type        = list(string)
  description = "User groups. 'Claude_<team>' 은 Default Department 하위 팀, 'Claude_<dept>_<team>' 은 dept 자동 생성 후 team 매핑, ClaudeAdmin 은 admin 부트스트랩. team_leader 는 Cognito 그룹이 아니라 admin-ui 에서 지정."
  default     = ["Claude_AI-Center_S/W-Culture-Office", "Claude_test-department_aws-test", "ClaudeAdmin"]
}

variable "tags" {
  type        = map(string)
  description = "Common tags"
  default     = {}
}

variable "deletion_protection" {
  # prod 는 ACTIVE — terraform destroy/-target 한 방에 admin 사용자 풀이
  # 영구 삭제되는 사고 방지. dev 는 재구축 편의로 INACTIVE.
  description = "User Pool 삭제 보호 (prod=ACTIVE 권장)"
  type        = string
  default     = "INACTIVE"
}

variable "managed_login" {
  # true: 도메인을 Managed Login(v2)으로 전환하고 managed-login/ 의 브랜딩
  # (settings.json·background.svg·favicon.svg)을 앱 클라이언트에 적용한다.
  # Essentials 기능이라 user_pool_tier 를 ESSENTIALS 로 고정한다.
  # false(기본): classic Hosted UI 그대로 — 기존 배포는 변화 없음.
  description = "Cognito Managed Login(v2) + 브랜딩 사용 여부 (gateway.yaml branding.managed_login)"
  type        = bool
  default     = false
}

variable "saml_providers" {
  # SAML IdP 맵 (키 = provider 이름). 빈 맵(기본)이면 아무 리소스도 만들지
  # 않아 기존 배포와 diff 가 없다. 선언하면:
  #   - aws_cognito_identity_provider.saml[<이름>] 생성
  #   - 앱 클라이언트 supported_identity_providers 에 자동 포함 — 콘솔에서
  #     수동 활성화해도 apply 때 ["COGNITO"] 로 되돌아가 SAML 버튼이
  #     사라지는 drift-revert 를 구조적으로 막는다.
  # metadata_url / metadata_xml 은 택일 — url 은 Cognito 서비스가 fetch
  # 하므로 ADFS 메타데이터 엔드포인트가 AWS 에서 reachable 해야 하고,
  # 내부망 전용이면 xml 내용을 직접 넣는다.
  # attribute_mapping: { <cognito 속성> = <SAML assertion 속성명> }.
  #   AD 그룹은 "custom:groups" 로 매핑 — 게이트웨이 팀/역할 해석 소스.
  #   AICM 전환(cutover) 시 이 키 한 줄을 지우면 attribute 경로가 닫힌다.
  description = "SAML IdP 맵 (ADFS 연동). gateway.yaml infra.cognito_saml 에서 managed overlay 로 내려옴"
  type = map(object({
    metadata_url      = optional(string, null)
    metadata_xml      = optional(string, null)
    attribute_mapping = map(string)
    idp_identifiers   = optional(list(string), [])
  }))
  default = {}

  validation {
    condition = alltrue([
      for name, p in var.saml_providers :
      (p.metadata_url != null) != (p.metadata_xml != null)
    ])
    error_message = "saml_providers: metadata_url 과 metadata_xml 은 정확히 하나만 지정해야 합니다."
  }

  validation {
    condition = alltrue([
      for name, p in var.saml_providers :
      length(p.attribute_mapping) > 0 && contains(keys(p.attribute_mapping), "email")
    ])
    error_message = "saml_providers: attribute_mapping 에 email 은 필수입니다 (사용자 식별)."
  }
}
