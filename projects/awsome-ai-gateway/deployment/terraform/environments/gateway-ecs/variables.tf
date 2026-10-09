# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# gateway-ecs 환경 변수 — deploy render 가 gateway.yaml 로부터 tfvars 를 생성한다.
# 수동으로 채우려면 terraform.tfvars.example 을 복사.
# ==============================================================================

variable "project" {
  type    = string
  default = "llm-gateway"
}

variable "environment" {
  description = "환경/사이트 이름 — gateway.yaml 의 env"
  type        = string
}

variable "aws_region" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ------------------------------------------------------------------------------
# VPC
# ------------------------------------------------------------------------------
variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "azs" {
  type    = list(string)
  default = [] # 비우면 리전의 처음 두 AZ
}

variable "private_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.1.0/24", "10.40.2.0/24"]
}

variable "public_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.101.0/24", "10.40.102.0/24"]
}

variable "database_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.201.0/24", "10.40.202.0/24"]
}

variable "elasticache_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.211.0/24", "10.40.212.0/24"]
}

# ------------------------------------------------------------------------------
# 데이터 저장소 토폴로지 — tier 프리셋이 여기로 내려온다
# ------------------------------------------------------------------------------
variable "db_mode" {
  description = "serverless | provisioned — t1/t2 는 serverless, t3 는 provisioned"
  type        = string
  default     = "serverless"
}

variable "serverless_min_acu" {
  type    = number
  default = 0.5
}

variable "serverless_max_acu" {
  type    = number
  default = 4.0
}

variable "db_safeguards" {
  description = "deletion protection / final snapshot — 운영 환경은 true"
  type        = bool
  default     = true
}

variable "db_instance_class" {
  description = "db_mode=provisioned 일 때 인스턴스 클래스"
  type        = string
  default     = "db.r7g.large"
}

variable "cache_mode" {
  description = "single | replicated | cluster — t1=single, t2=replicated, t3=cluster"
  type        = string
  default     = "single"
}

variable "cache_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "nat_ha" {
  description = "AZ 당 NAT 게이트웨이 — t1=false, t2+=true"
  type        = bool
  default     = false
}

# ------------------------------------------------------------------------------
# 네트워크 / 도메인
# ------------------------------------------------------------------------------
variable "alb_internal" {
  description = "network.mode=private → true. 선행조건: VPN/DX 경유 접근 경로"
  type        = bool
  default     = false
}

variable "allowed_cidrs" {
  type    = list(string)
  default = [] # 비우면 ecs 모듈 기본 0.0.0.0/0 — schema 가 이미 경고
}

variable "domain_name" {
  type    = string
  default = ""
}

variable "hosted_zone_id" {
  type    = string
  default = ""
}

variable "certificate_arn" {
  type    = string
  default = ""
}

# ------------------------------------------------------------------------------
# 이미지 / 사이징
# ------------------------------------------------------------------------------
variable "image_registry" {
  description = "비우면 모듈이 <account>.dkr.ecr.<region>.amazonaws.com/<project> repo 를 만들어 쓴다"
  type        = string
  default     = ""
}

variable "image_tag" {
  description = "배포 이미지 태그 — 핀 필수"
  type        = string
}

variable "service_sizing" {
  description = "서비스별 cpu/memory/desired_count — tier 프리셋 또는 override"
  type = map(object({
    cpu           = number
    memory        = number
    desired_count = number
  }))
  default = {}
}

# ------------------------------------------------------------------------------
# Cognito (OIDC 소스)
# ------------------------------------------------------------------------------
variable "cognito_domain_suffix" {
  description = "Hosted UI 도메인 suffix — 비우면 account id 로 자동 생성"
  type        = string
  default     = ""
}

variable "cognito_groups" {
  type    = list(string)
  default = ["ClaudeAdmin"]
}

variable "cognito_callback_urls" {
  description = "추가 callback — localhost CLI 콜백이 여기 온다. admin 도메인 콜백은 자동 합성"
  type        = list(string)
  default = [
    "http://localhost:8090/callback",
    "http://localhost:8091/callback",
    "http://localhost:8092/callback",
  ]
}

variable "cognito_logout_urls" {
  type = list(string)
  default = [
    "http://localhost:8090/logout",
    "http://localhost:8091/logout",
    "http://localhost:8092/logout",
  ]
}

variable "oidc_required_group" {
  type    = string
  default = ""
}

# ADMIN 부트스트랩 — admin-api/admin-ui 양쪽에 주입된다.
# admin_groups 비우면 cognito_groups(기본 ["ClaudeAdmin"])가 ADMIN 이 된다.
variable "admin_groups" {
  type    = list(string)
  default = []
}

variable "admin_emails" {
  type    = list(string)
  default = []
}

# ------------------------------------------------------------------------------
# Notifications
# ------------------------------------------------------------------------------
variable "email_sender_type" {
  type    = string
  default = "mock"
}

variable "email_sender_address" {
  type    = string
  default = "noreply@llm-gateway.local"
}

variable "aws_ses_region" {
  type    = string
  default = ""
}

variable "smtp_host" {
  type    = string
  default = ""
}

variable "smtp_port" {
  type    = number
  default = 587
}

variable "smtp_username" {
  type    = string
  default = ""
}

variable "smtp_password" {
  type      = string
  sensitive = true
  default   = ""
}

# ------------------------------------------------------------------------------
# 기능 플래그
# ------------------------------------------------------------------------------
variable "enable_body_logging" {
  type    = bool
  default = false
}

variable "body_log_retention_days" {
  type    = number
  default = 90
}

variable "agentcore_gateway_url" {
  description = "Web Search AgentCore Gateway URL — us-east-1 구조적 제약"
  type        = string
  default     = ""
}

# ------------------------------------------------------------------------------
# 권한 범위
# ------------------------------------------------------------------------------
variable "bedrock_allowed_model_arns" {
  type    = list(string)
  default = ["*"]
}

variable "allowed_sts_regions" {
  type    = list(string)
  default = []
}

variable "allowed_iam_roles" {
  description = "CLI 인증 허용 role 패턴 — 비어 있으면 CLI 로그인 불가"
  type        = list(string)
  default     = []
}

variable "cowork_role_arn" {
  type    = string
  default = ""
}

variable "claude_code_role_arn" {
  type    = string
  default = ""
}

# ------------------------------------------------------------------------------
# deploy apply 의 2-phase 동적 값 — domain=none 이면 첫 apply 가 ALB DNS 를
# 발견하고 extra-vars.json/tfvars 에 이 키들을 기록한다 (직접 편집 아님)
# ------------------------------------------------------------------------------
variable "admin_ui_nextauth_url" {
  type    = string
  default = ""
}

variable "gateway_external_url" {
  type    = string
  default = ""
}

variable "admin_api_external_url" {
  type    = string
  default = ""
}

variable "email_sender_name" {
  type    = string
  default = "LLM Gateway"
}
