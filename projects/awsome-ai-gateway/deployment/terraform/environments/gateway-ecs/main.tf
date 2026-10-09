# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# gateway-ecs — ECS Fargate 소규모 배포 경로 (T1/T2)
# ------------------------------------------------------------------------------
# EKS 환경(llm-gateway-dev/prod)과 같은 모듈을 쓰되, Kubernetes 전용 계층
# (eks-fargate / irsa / alb-controller / external-secrets / helm)을 빼고
# modules/ecs-gateway 가 그 역할을 한다.
#
# 사이징은 environment 이름이 아니라 명시 변수로 결정한다:
#   db_mode / db_safeguards / cache_mode / nat_ha — deploy render 가
#   gateway.yaml 의 size_tier 에서 이 값들을 생성한다.
# ==============================================================================

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = length(var.azs) > 0 ? var.azs : slice(data.aws_availability_zones.available.names, 0, 2)

  # Cognito 도메인 suffix 미지정이면 account_id 로 자동 생성(전역 unique 보장)
  cognito_domain_suffix = (
    var.cognito_domain_suffix != ""
    ? var.cognito_domain_suffix
    : "gw-auth-${data.aws_caller_identity.current.account_id}"
  )

  # admin-ui callback — localhost CLI 콜백은 tfvars 기본값에 있고, 도메인이
  # 있으면 admin.<domain> 콜백을 자동으로 합친다(additive — 기존 항목 유지).
  callback_urls = concat(
    var.cognito_callback_urls,
    var.domain_name != "" ? ["https://admin.${var.domain_name}/api/auth/callback"] : [],
  )
  logout_urls = concat(
    var.cognito_logout_urls,
    var.domain_name != "" ? ["https://admin.${var.domain_name}/"] : [],
  )
}

# ------------------------------------------------------------------------------
# 네트워크
# ------------------------------------------------------------------------------
module "vpc" {
  source = "../../modules/vpc"

  project                  = var.project
  environment              = var.environment
  aws_region               = var.aws_region
  cidr                     = var.vpc_cidr
  azs                      = local.azs
  private_subnet_cidrs     = var.private_subnet_cidrs
  public_subnet_cidrs      = var.public_subnet_cidrs
  database_subnet_cidrs    = var.database_subnet_cidrs
  elasticache_subnet_cidrs = var.elasticache_subnet_cidrs

  # t1 = 단일 NAT (비용), t2+ = nat_ha=true 권장
  nat_ha = var.nat_ha

  tags = var.tags
}

# ------------------------------------------------------------------------------
# 데이터 저장소
# ------------------------------------------------------------------------------
module "aurora" {
  source = "../../modules/aurora-postgresql"

  project              = var.project
  environment          = var.environment
  vpc_id               = module.vpc.vpc_id
  db_subnet_group_name = module.vpc.database_subnet_group_name
  private_subnet_cidrs = var.private_subnet_cidrs
  availability_zones   = local.azs

  db_mode             = var.db_mode
  serverless_min_acu  = var.serverless_min_acu
  serverless_max_acu  = var.serverless_max_acu
  safeguards          = var.db_safeguards
  prod_instance_class = var.db_instance_class

  tags = var.tags
}

module "elasticache" {
  source = "../../modules/elasticache-valkey"

  project              = var.project
  environment          = var.environment
  vpc_id               = module.vpc.vpc_id
  subnet_group_name    = module.vpc.elasticache_subnet_group_name
  private_subnet_cidrs = var.private_subnet_cidrs

  cache_mode    = var.cache_mode
  dev_node_type = var.cache_node_type

  tags = var.tags
}

# ------------------------------------------------------------------------------
# Cognito — admin-ui SSO + CLI 로그인의 OIDC 소스
# ------------------------------------------------------------------------------
module "cognito" {
  source = "../../modules/cognito"

  project       = var.project
  environment   = var.environment
  aws_region    = var.aws_region
  domain_suffix = local.cognito_domain_suffix
  callback_urls = local.callback_urls
  logout_urls   = local.logout_urls
  groups        = var.cognito_groups

  tags = var.tags
}

# ------------------------------------------------------------------------------
# 요청/응답 본문 로깅 sink — EKS 경로와 같은 모듈, ecs task role 에 ARN 주입
# ------------------------------------------------------------------------------
module "body_logging" {
  source = "../../modules/body-logging"

  enabled            = var.enable_body_logging
  project            = var.project
  environment        = var.environment
  log_retention_days = var.body_log_retention_days
  force_destroy      = false

  tags = var.tags
}

# ------------------------------------------------------------------------------
# ECS 게이트웨이 — ALB + 서비스 + task def + 시크릿 + task role
# ------------------------------------------------------------------------------
module "gateway" {
  source = "../../modules/ecs-gateway"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region

  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  public_subnet_ids  = module.vpc.public_subnet_ids
  alb_internal       = var.alb_internal
  allowed_cidrs      = var.allowed_cidrs

  domain_name     = var.domain_name
  hosted_zone_id  = var.hosted_zone_id
  certificate_arn = var.certificate_arn

  image_registry = var.image_registry
  image_tag      = var.image_tag
  service_sizing = var.service_sizing

  # 데이터 저장소 — 기존 모듈 출력을 그대로 연결
  db_host                     = module.aurora.cluster_endpoint
  db_name                     = "gateway"
  db_master_username          = "postgres_admin"
  db_master_secret_arn        = module.aurora.master_user_secret_arn
  db_security_group_id        = module.aurora.security_group_id
  redis_host                  = var.cache_mode == "cluster" ? module.elasticache.configuration_endpoint_address : module.elasticache.primary_endpoint_address
  redis_auth_token_secret_arn = module.elasticache.auth_token_secret_arn
  redis_tls_enabled           = true
  redis_security_group_id     = module.elasticache.security_group_id
  redis_cluster_mode          = var.cache_mode == "cluster"

  # OIDC — Cognito 모듈 출력이 소스 (PKCE public client — client_secret 없음)
  oidc_issuer_url       = module.cognito.issuer_url
  oidc_client_id        = module.cognito.client_id
  oidc_authorize_url    = "https://${module.cognito.hosted_ui_domain}/oauth2/authorize"
  oidc_token_url        = "https://${module.cognito.hosted_ui_domain}/oauth2/token"
  oidc_provider_name    = "oidc:cognito"
  oidc_required_group   = var.oidc_required_group
  dev_login_enabled     = false # OIDC 를 쓰는 배포에서 dev login 은 항상 끈다
  cognito_user_pool_arn = module.cognito.user_pool_arn
  cognito_user_pool_id  = module.cognito.user_pool_id
  # admin-api/admin-ui 의 ADMIN 부트스트랩 — 비우면 아무도 관리자가 못 된다.
  # 기본은 생성하는 Cognito 그룹(ClaudeAdmin) 전체를 ADMIN 으로.
  admin_groups = length(var.admin_groups) > 0 ? var.admin_groups : var.cognito_groups
  admin_emails = var.admin_emails

  # 권한 범위
  allowed_sts_regions        = length(var.allowed_sts_regions) > 0 ? var.allowed_sts_regions : [var.aws_region]
  allowed_iam_roles          = var.allowed_iam_roles
  bedrock_allowed_model_arns = var.bedrock_allowed_model_arns
  cowork_role_arn            = var.cowork_role_arn
  claude_code_role_arn       = var.claude_code_role_arn

  # 2-phase 동적 주소 — domain=none 이면 deploy apply 가 tfvars 에 기록
  admin_ui_nextauth_url  = var.admin_ui_nextauth_url
  gateway_external_url   = var.gateway_external_url
  admin_api_external_url = var.admin_api_external_url

  # 기능
  email_sender_type    = var.email_sender_type
  email_sender_address = var.email_sender_address
  email_sender_name    = var.email_sender_name
  aws_ses_region       = var.aws_ses_region
  smtp_host            = var.smtp_host
  smtp_port            = var.smtp_port
  smtp_username        = var.smtp_username
  smtp_password        = var.smtp_password

  body_log_firehose_name = var.enable_body_logging ? module.body_logging.firehose_stream_name : ""
  body_log_firehose_arn  = var.enable_body_logging ? module.body_logging.firehose_stream_arn : ""
  body_log_bucket_arn    = var.enable_body_logging ? module.body_logging.bucket_arn : ""
  agentcore_gateway_url  = var.agentcore_gateway_url

  tags = var.tags
}
