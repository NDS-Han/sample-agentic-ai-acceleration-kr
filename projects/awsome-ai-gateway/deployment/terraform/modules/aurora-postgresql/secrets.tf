# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# DB Secrets — Terraform-managed
# ------------------------------------------------------------------------------
# gateway user 와 관련 secret 을 Terraform 이 일괄 관리. operator 는 openssl 로
# 직접 password 를 생성하지 않고, Terraform 이 random_password 로 생성해 두
# 경로에 동일 값으로 박아둠:
#
#   /llm-gateway/<env>/db/gateway-user   (RDS Proxy auth 용 — {username, password})
#   /llm-gateway/<env>/db                (Helm ExternalSecret 용 — {password})
#
# 두 secret 의 `password` 값은 single source(random_password.gateway_user)에서
# 파생되므로 항상 동기화됨.
#
# 활성화 조건: var.enable_rds_proxy = true 일 때만 생성 (proxy auth 용).
#   Proxy 를 안 쓰는 경우엔 기존 operator 수동 생성 방식(03-secrets.md)을 사용.
# ==============================================================================

resource "random_password" "gateway_user" {
  count  = local.proxy_enabled ? 1 : 0
  length = 32
  # asyncpg URL encoding 복잡성 회피 — alphanumeric only
  special = false
  # 재생성 방지 (password rotation 필요 시 명시적 taint)
  lifecycle {
    ignore_changes = [length, special]
  }
}

# master password 는 여기에 복사하지 않는다 (의도적).
# manage_master_user_password=true 면 RDS 가 rds!cluster-<uuid> 시크릿을 소유하고 자동
# 로테이션한다 — 실측(2026-09-09) dev/prod 모두 RotationEnabled=true,
# AutomaticallyAfterDays=7. Terraform 이 값을 복사해두면 apply 사이에 반드시 stale 해진다:
# /llm-gateway/prod/db 는 2026-07-09 → 2026-09-07 사이 새 버전이 없는데 그 구간에 master 는
# ~8회 로테이션되었다. 게다가 data source 가 매 plan 마다 AWSCURRENT 를 다시 읽으므로
# 로테이션마다 가짜 plan diff(secret_version 은 ForceNew)가 생긴다.
# 그래서 Helm 은 master 비번을 RDS 관리형 시크릿에서 직접 읽는다:
#   database.external.masterPasswordRemoteKey      = "rds!cluster-<uuid>"
#   database.external.masterPasswordRemoteProperty = "password"
# (values-eks-fargate-{dev,prod}.yaml 에 설정됨. ESO IRSA 에 secret:rds!cluster-* read 권한이
#  이미 선언돼 있음 — modules/irsa/main.tf.)
# 시크릿 이름 조회:
#   aws rds describe-db-clusters --db-cluster-identifier <cluster-id> \
#     --query 'DBClusters[0].MasterUserSecret.SecretArn'

# ---- /db/gateway-user ---- (RDS Proxy auth format)
resource "aws_secretsmanager_secret" "gateway_user" {
  count       = local.proxy_enabled ? 1 : 0
  name        = "/${var.project}/${var.environment}/db/gateway-user"
  description = "Application 'gateway' user credentials — used by RDS Proxy auth."
  kms_key_id  = var.kms_key_id
  # destroy 시 즉시 삭제 (기본 30일 recovery window 가 재배포 시 name 충돌 유발).
  recovery_window_in_days = 0

  tags = merge(var.tags, {
    Environment = var.environment
    Module      = "aurora-postgresql"
    Purpose     = "rds-proxy-auth"
  })
}

resource "aws_secretsmanager_secret_version" "gateway_user" {
  count     = local.proxy_enabled ? 1 : 0
  secret_id = aws_secretsmanager_secret.gateway_user[0].id
  secret_string = jsonencode({
    username = "gateway"
    password = random_password.gateway_user[0].result
  })
}

# ---- /db ---- (Helm ExternalSecret format)
# Helm chart `database.external.passwordSecretName` 가 참조하는 기존 경로.
# 기존에 operator 가 수동 생성하던 것을 Terraform 으로 이관.
resource "aws_secretsmanager_secret" "db" {
  count       = local.proxy_enabled ? 1 : 0
  name        = "/${var.project}/${var.environment}/db"
  description = "DB credentials for Helm ExternalSecret (Terraform-managed)."
  kms_key_id  = var.kms_key_id
  # destroy 시 즉시 삭제 (기본 30일 recovery window 가 재배포 시 name 충돌 유발).
  recovery_window_in_days = 0

  tags = merge(var.tags, {
    Environment = var.environment
    Module      = "aurora-postgresql"
    Purpose     = "helm-external-secret"
  })
}

resource "aws_secretsmanager_secret_version" "db" {
  count     = local.proxy_enabled ? 1 : 0
  secret_id = aws_secretsmanager_secret.db[0].id
  secret_string = jsonencode({
    password = random_password.gateway_user[0].result
  })
}
