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

variable "aurora_engine_version" {
  # 기존 클러스터 env 의 실측 메이저 핀 (NDS: infra.db.engine_version → tfvars).
  # null 이면 모듈이 AWS 기본 버전의 메이저로 family 를 맞춘다(신규 env 용).
  description = "Parameter group family 메이저 핀 — null 이면 AWS 기본 버전의 메이저"
  type        = string
  default     = null
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
  description = "ElastiCache node type — non-cluster(dev)와 cluster(prod) 경로 모두에 적용"
  type        = string
  default     = "cache.t4g.small"
}

variable "cache_num_clusters" {
  description = "non-cluster 노드 수. single=1(기본), replicated=2 이상(primary+replica — failover/Multi-AZ 자동). cluster mode 에서는 무시"
  type        = number
  default     = 1
}

variable "cache_num_node_groups" {
  description = "cluster mode 샤드 수 (cache_mode=cluster 일 때만)"
  type        = number
  default     = 3
}

variable "cache_replicas_per_node_group" {
  description = "cluster mode 샤드당 replica 수 (cache_mode=cluster 일 때만)"
  type        = number
  default     = 1
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
  type = list(string)
  # 미지정 시 [] — 인그레스 규칙 0개로 fail-closed. ALB 를 실제로 열려면 운영
  # 대역을 명시한다(평문 http_mode + 0.0.0.0/0 조합은 precondition 이 거부).
  default = []
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
  description = "서비스별 cpu/memory/desired_count + 선택 min/max_count(오토스케일) — tier 프리셋 또는 override"
  type = map(object({
    cpu           = number
    memory        = number
    desired_count = number
    min_count     = optional(number)
    max_count     = optional(number)
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

variable "oidc_vk_ttl_hours" {
  type    = number
  default = 1
}

# features.service_keys — 기본 꺼짐. true 면 서비스 계정 장기키 발급을 허용한다
variable "service_keys_enabled" {
  type    = bool
  default = false
}

# admin-ui 사이드바·탭 제목의 서비스 이름 (gateway.yaml branding.name).
# 비우면 admin-ui 기본값("AWSome AI Gateway").
variable "admin_brand_name" {
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

variable "email_api_url" {
  type    = string
  default = ""
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

# ─── admin-chat-agent (features.bi_insight) ───
# EKS env 와 같은 플래그 — module.agentcore_runtime 게이트.
variable "enable_chat_agent" {
  description = "admin-chat-agent 인프라 (ECR + S3 staging + IAM + KMS) 생성 여부"
  type        = bool
  default     = false
}

variable "enable_chat_db_tools" {
  description = "admin-chat-agent 의 query_db/get_schema Lambda + reader secret + SG 생성 여부"
  type        = bool
  default     = false
}

# AgentCore Runtime 리전 — provisioner(provision_agentcore_chatagent.py REGION)
# 와 반드시 같아야 한다. admin-api 코드 기본값(ap-northeast-2)과 일치시켰다.
variable "chat_agent_region" {
  description = "admin-chat-agent AgentCore Runtime 을 만들 리전 (task role statement 스코프용)"
  type        = string
  default     = "ap-northeast-2"
}

# provisioner 가 만든 Runtime ARN — deploy apply 가 extra-vars 로 tfvars 에
# 기록한다 (agentcore_gateway_url 과 같은 2-phase 패턴).
variable "agentcore_runtime_arn" {
  description = "AgentCore Runtime ARN — admin-api task env 주입용. 비면 /chat 은 503"
  type        = string
  default     = ""
}

variable "litellm_pricing_lambda" {
  description = "LiteLLM 단가 조회 Lambda 이름 — 비면 litellm 소스 비활성 (ops/NDS-01)"
  type        = string
  default     = ""
}

# ------------------------------------------------------------------------------
# 권한 범위
# ------------------------------------------------------------------------------
variable "bedrock_allowed_model_arns" {
  type = list(string)
  # 기본값 없음 — ["*"] 는 계정 내 모든 foundation model/프로파일 호출을 허용해
  # 스코핑 변수의 존재 의의를 무력화한다. dev/prod 의 명시 ARN 목록 참조.
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

# ------------------------------------------------------------------------------
# Alerting — managed overlay (features.alerting)
# ------------------------------------------------------------------------------
# ── 비용 정확성 비즈니스 알림 (notification-worker) — 전부 기본 꺼짐 ──
variable "business_alerts_enabled" {
  type    = bool
  default = false
}

variable "business_alert_reconciliation_mismatch" {
  type    = bool
  default = false
}

variable "business_alert_dead_letter_growth" {
  type    = bool
  default = false
}

variable "business_alert_pricing_missing" {
  type    = bool
  default = false
}

# slack=true 면 app secret 의 json key `slack_webhook_url` 을 valueFrom 으로 배선.
# 활성화 전에 그 key 를 SM 에 만들어 두어야 한다 — 없으면 태스크 기동 실패(ECS 계약).
variable "business_alerts_slack" {
  type    = bool
  default = false
}

variable "enable_alerting" {
  type        = bool
  default     = false
  description = "managed overlay: SNS topic + Aurora/ElastiCache CloudWatch alarms"
}

variable "alert_emails" {
  type        = list(string)
  default     = []
  description = "managed overlay: SNS email subscriptions — pending until recipient confirms"
}

variable "alb_lb_dimensions" {
  type    = list(string)
  default = []
}

variable "alb_target_groups" {
  type    = list(object({ lb = string, tg = string }))
  default = []
}

variable "cognito_managed_login" {
  description = "Cognito Managed Login(v2) + 브랜딩 — gateway.yaml branding.managed_login 이 managed overlay 로 내린다"
  type        = bool
  default     = false
}
