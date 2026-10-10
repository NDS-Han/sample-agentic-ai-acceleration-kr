# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# ecs-gateway — ECS Fargate 배포 경로의 컴퓨트/인입점 계층
#
# 서비스 구성 (EKS helm chart 와 동일한 7-워크로드):
#   gateway-proxy(8000) admin-api(8080) admin-ui(3000)
#   scheduler / cost-recorder-worker / notification-worker / migration(run-task)
#
# EKS 경로와의 대응:
#   IRSA          → task role (iam.tf)
#   ESO           → task def `secrets:` + execution role (secrets.tf, iam.tf)
#   helm env      → task def environment/secrets (taskdefs.tf)
#   ALB Ingress   → alb.tf (host 기반 또는 포트 기반)
#   migration Job → migration task def + deploy 측 `aws ecs run-task`
# ==============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  name_prefix = "${var.project}-${var.environment}"

  # ECR repo 를 모듈이 만들면 그 주소를, 아니면 var.image_registry 를 쓴다
  registry = var.image_registry != "" ? var.image_registry : (
    var.create_ecr_repos ? "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.name}.amazonaws.com/${var.project}" : ""
  )

  # 서비스 정의 — helm chart 의 워크로드와 1:1. command 는 sh -c 래퍼로
  # DB_URL/REDIS_URL 을 secret env 로부터 조립한다 (k8s $(VAR) 치환의 ECS 판).
  services = {
    gateway-proxy = {
      port    = 8000
      image   = "gateway-proxy"
      alb     = true
      health  = "/health"
      db      = true
      redis   = true
      command = "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers $${WORKERS:-4}"
    }
    admin-api = {
      port    = 8080
      image   = "admin-api"
      alb     = true
      health  = "/health"
      db      = true
      redis   = true
      command = "exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1"
    }
    admin-ui = {
      port    = 3000
      image   = "admin-ui"
      alb     = true
      health  = "/api/health"
      db      = false
      redis   = false
      command = "exec node -e \"process.env.HOSTNAME='0.0.0.0'; require('./server.js')\""
    }
    scheduler = {
      port    = 0
      image   = "admin-api"
      alb     = false
      health  = ""
      db      = true
      redis   = false
      command = "exec python -m app.scheduler.main"
    }
    cost-recorder-worker = {
      port    = 0
      image   = "cost-recorder-worker"
      alb     = false
      health  = ""
      db      = true
      redis   = true
      command = "exec python -m worker.main"
    }
    notification-worker = {
      port    = 0
      image   = "notification-worker"
      alb     = false
      health  = ""
      db      = true
      redis   = true
      command = "exec python -m worker.main"
    }
    # migration — 서비스가 아니라 `aws ecs run-task` 로 1회 실행하는 task def.
    # run_migration.sh 가 init SQL → app user 생성/GRANT → alembic upgrade head 를 처리.
    migration = {
      port    = 0
      image   = "migration"
      alb     = false
      health  = ""
      db      = false # DB_MASTER_URL 경로 — env 래퍼가 아니라 run_migration.sh 의 계약
      redis   = false
      command = "./run_migration.sh"
    }
  }

  default_sizing = {
    cpu           = 512
    memory        = 1024
    desired_count = 1
  }

  # var.service_sizing 의 객체는 cpu/memory/desired_count 를 모두 포함해야 하므로
  # (map(object) 제약) 서비스가 없으면 default 로 대체한다
  sizing = {
    for name in keys(local.services) :
    name => lookup(var.service_sizing, name, local.default_sizing)
  }

  # 도메인/HTTPS 모드 결정 — 세 갈래:
  #   cert:    domain + (zone_id 자동발급 or certificate_arn 지참) → 443 host 라우팅
  #   http:    domain 없음 → ALB DNS 에 8000/8080/3000 포트 라우팅 (Caddy none 모드와 동일)
  #   pending: domain 있는데 검증 수단 없음 → 80 host 라우팅 + cert 발급 대기
  https_ready = var.domain_name != "" && (var.certificate_arn != "" || var.hosted_zone_id != "")
  http_mode   = var.domain_name == ""

  # 호스트 → 서비스 라우팅 (도메인 모드)
  host_routes = {
    "gateway.${var.domain_name}"   = "gateway-proxy"
    "admin-api.${var.domain_name}" = "admin-api"
    "admin.${var.domain_name}"     = "admin-ui"
  }

  # 포트 → 서비스 라우팅 (도메인 없음 모드)
  port_routes = {
    8000 = "gateway-proxy"
    8080 = "admin-api"
    3000 = "admin-ui"
  }

  # 외부 주소 — domain 모드는 도메인에서 파생, 아니면 두 번째 apply 의 override.
  # NEXTAUTH_URL 은 NextAuth callback 조립에 필수라 domain=none 도 비울 수 없다.
  admin_ui_external_url = var.admin_ui_nextauth_url != "" ? var.admin_ui_nextauth_url : (
  var.domain_name != "" ? "https://admin.${var.domain_name}" : "")
  gateway_external_url = var.gateway_external_url != "" ? var.gateway_external_url : (
  var.domain_name != "" ? "https://gateway.${var.domain_name}" : "")
  admin_api_external_url = var.admin_api_external_url != "" ? var.admin_api_external_url : (
  var.domain_name != "" ? "https://admin-api.${var.domain_name}" : "")

  # ALB 가 받는 포트 — https_ready 면 443(+80 리다이렉트), 아니면 서비스 포트 3개
  alb_ingress_ports = local.https_ready ? [80, 443] : (local.http_mode ? [8000, 8080, 3000] : [80])
}

# ------------------------------------------------------------------------------
# ECS 클러스터
# ------------------------------------------------------------------------------
resource "aws_ecs_cluster" "this" {
  name = local.name_prefix

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = var.tags
}

# ------------------------------------------------------------------------------
# CloudWatch 로그 그룹 — 서비스별 1개
# ------------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "svc" {
  for_each = local.services

  name              = "/ecs/${local.name_prefix}/${each.key}"
  retention_in_days = 30

  tags = var.tags
}

# ------------------------------------------------------------------------------
# ECR repos — image_registry 미지정 시 자기 계정 레포 6개
# ------------------------------------------------------------------------------
resource "aws_ecr_repository" "svc" {
  for_each = var.create_ecr_repos && var.image_registry == "" ? toset(["gateway-proxy", "admin-api", "admin-ui", "cost-recorder-worker", "notification-worker", "migration"]) : toset([])

  name                 = "${var.project}/${each.key}"
  image_tag_mutability = "MUTABLE" # 태그는 deploy 가 핀으로 관리 — mutability 는 push 편의용

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = var.tags
}

# ------------------------------------------------------------------------------
# 내부 서비스 디스커버리 — admin-ui → admin-api 만 필요 (유일한 내부 HTTP 호출).
# Cloud Map private DNS: admin-api.gateway.internal → task IP (A record, multi-value)
# ------------------------------------------------------------------------------
resource "aws_service_discovery_private_dns_namespace" "internal" {
  name        = "${local.name_prefix}.internal"
  description = "llm-gateway ECS 내부 서비스 디스커버리"
  vpc         = var.vpc_id

  tags = var.tags
}

resource "aws_service_discovery_service" "admin_api" {
  name = "admin-api"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  # ECS healthcheck 와 별개로 registry 에서 빠르게 빼기 위한 헬스체크 — Fargate task 는
  # ALB health 체크를 따르므로 실패 시 DNS 가 stale 해지지 않게 한다
  health_check_custom_config {
    failure_threshold = 1
  }
}
