# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# Secrets — task def `secrets:` 가 참조하는 두 개의 SM secret
#
#   /<project>/<env>/app  — 앱 시크릿 JSON (VK DEK, nextauth, oidc client secret, smtp)
#   /<project>/<env>/db   — 앱 DB 유저 자격증명 JSON {username, password}
#
# ⚠️ random_password/random_bytes 값은 **terraform state 에 평문**으로 남는다 —
#    state backend(S3+DynamoDB) 접근 통제가 비밀 보호의 실질 경계다.
#    (EKS 경로의 elasticache auth_token 도 같은 방식으로 state 에 있다.)
# ⚠️ VK DEK 를 잃으면 발급된 Virtual Key 전부 무효 — secret 삭제/교체 주의.
#    recovery_window_in_days=7 로 즉시삭제를 막는다.
# ==============================================================================

# AES-256-GCM DEK — 앱이 64-hex 를 기대하므로 random_bytes.hex 사용
resource "random_bytes" "vk_dek" {
  length = 32
}

resource "random_password" "nextauth" {
  length  = 48
  special = false
}

resource "random_password" "db_app_password" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name                    = "/${var.project}/${var.environment}/app"
  description             = "llm-gateway 앱 시크릿 (VK DEK / nextauth / oidc / smtp)"
  recovery_window_in_days = 7

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    virtual_key_encryption_key = random_bytes.vk_dek.hex
    nextauth_secret            = random_password.nextauth.result
    internal_api_token         = random_password.internal_api_token.result
    oidc_client_secret         = var.oidc_client_secret
    smtp_password              = var.smtp_password
  })
}

resource "random_password" "internal_api_token" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "db_app" {
  name                    = "/${var.project}/${var.environment}/db"
  description             = "앱 DB 유저 — migration task 가 이 자격으로 role 을 만든다"
  recovery_window_in_days = 7

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "db_app" {
  secret_id = aws_secretsmanager_secret.db_app.id
  secret_string = jsonencode({
    username = var.db_app_user
    password = random_password.db_app_password.result
  })
}
