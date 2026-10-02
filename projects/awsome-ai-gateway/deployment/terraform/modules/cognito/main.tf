# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# Cognito User Pool — OIDC IDP for LLM Gateway
# ------------------------------------------------------------------------------
# - User Pool 1개 (env 별)
# - Public App Client (PKCE 강제, refresh token rotation)
# - Hosted UI 도메인 (`{prefix}.auth.{region}.amazoncognito.com`)
# - 사용자 그룹:
#   - Claude_<team>             → Default Department 하위 <team> 팀 자동 매핑
#   - Claude_<department>_<team> → <department> 부서 자동 생성 + 그 아래 <team> 팀
#   - ClaudeAdmin               → admin role 부트스트랩 (팀 매핑 제외)
#   - "Claude_" prefix 없는 그룹은 OIDC 로그인 거부 (REJECT_UNMATCHED_GROUPS=true)
#   (admin 이 콘솔에서 사용자를 그룹에 직접 추가)
#   team_leader role 은 Cognito 그룹으로 부트스트랩하지 않는다 — Claude_<team> 으로
#   정상 배정된 팀원 중 한 명을 admin-ui 에서 관리자가 리더로 지정한다
#   (PUT /admin/teams/{id}/leader). 그룹 두 개를 맞춰야 하는 운영 부담이 없다.
#
# 토큰 형식 주의:
#   - Cognito access_token: 표준 OIDC `aud` claim 없음 (대신 client_id claim).
#     admin-api 의 OIDC_AUDIENCE 는 비워두고 audience 검증 skip 사용.
#   - issuer URL: https://cognito-idp.{region}.amazonaws.com/{user_pool_id}
# ==============================================================================

locals {
  user_pool_name = "${var.project}-${var.environment}-userpool"
}

# ------------------------------------------------------------------------------
# User Pool
# ------------------------------------------------------------------------------
resource "aws_cognito_user_pool" "this" {
  name = local.user_pool_name

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  # MFA off (MVP — v2 에서 검토)
  mfa_configuration = "OFF"

  # 사용자 self-signup 차단 — admin 이 콘솔에서 직접 생성
  admin_create_user_config {
    allow_admin_create_user_only = true
    invite_message_template {
      email_subject = "[LLM Gateway] 임시 자격증명"
      email_message = "안녕하세요, {username}.\n임시 패스워드: {####}\n\n첫 로그인 시 패스워드를 변경해주세요."
      sms_message   = "{username} 임시 패스워드: {####}"
    }
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 1
      max_length = 320
    }
  }

  schema {
    name                = "name"
    attribute_data_type = "String"
    required            = false
    mutable             = true
    string_attribute_constraints {
      min_length = 0
      max_length = 255
    }
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = merge(var.tags, {
    Name        = local.user_pool_name
    Environment = var.environment
  })
}

# ------------------------------------------------------------------------------
# Hosted UI 도메인
# ------------------------------------------------------------------------------
resource "aws_cognito_user_pool_domain" "this" {
  domain       = "${var.project}-${var.environment}-${var.domain_suffix}"
  user_pool_id = aws_cognito_user_pool.this.id
}

# ------------------------------------------------------------------------------
# Hosted UI 브랜딩 — 로고 + CSS (classic Hosted UI 가 허용하는 선택자만 유효)
# ------------------------------------------------------------------------------
resource "aws_cognito_user_pool_ui_customization" "this" {
  user_pool_id = aws_cognito_user_pool.this.id
  client_id    = aws_cognito_user_pool_client.cli.id
  image_file   = filebase64("${path.module}/logo.png")
  css          = file("${path.module}/hosted-ui.css")
}

# ------------------------------------------------------------------------------
# App Client — Public (PKCE 강제), gateway-cli 용
# ------------------------------------------------------------------------------
resource "aws_cognito_user_pool_client" "cli" {
  name         = "${var.project}-${var.environment}-cli"
  user_pool_id = aws_cognito_user_pool.this.id

  # Public client — secret 없음 (PKCE 로 보안)
  generate_secret = false

  # Authorization Code flow (PKCE 강제) + refresh
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  # Callback / logout URLs
  callback_urls = var.callback_urls
  logout_urls   = var.logout_urls

  # Token TTL — 우리 spec 의 권장값
  #
  # id_token: admin-ui 는 이 토큰을 admin_jwt 쿠키로 쓰고 만료를 토큰 exp 에 맞춘다.
  # 1시간이던 것을 12시간으로 올린다 — 콜백의 쿠키 수명 상한(MAX_COOKIE_MAX_AGE)과
  # 동일한 값이며, 그 이상 줘도 쿠키가 12h 에 잘라버리므로 의미가 없다.
  # access_token 은 gateway-cli 가 쓰며 ALLOW_REFRESH_TOKEN_AUTH 로 갱신하므로 1h 유지.
  access_token_validity  = 1  # 1시간
  id_token_validity      = 12 # 12시간 — 하루 업무 세션 동안 재로그인 없게
  refresh_token_validity = 7  # 7일
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  # Refresh token rotation
  enable_token_revocation       = true
  prevent_user_existence_errors = "ENABLED"

  # API 호출용 Auth flows (테스트 ROPC 허용)
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_PASSWORD_AUTH", # admin 이 직접 사용자 생성 후 임시 PW 사용
  ]

  read_attributes  = ["email", "email_verified", "name"]
  write_attributes = ["email", "name"]
}

# ------------------------------------------------------------------------------
# 사용자 그룹 (terraform 으로 미리 생성, admin 이 사용자를 콘솔에서 추가)
# ------------------------------------------------------------------------------
resource "aws_cognito_user_group" "groups" {
  for_each = toset(var.groups)

  user_pool_id = aws_cognito_user_pool.this.id
  name         = each.value
  description  = "LLM Gateway team mapping group"
}
