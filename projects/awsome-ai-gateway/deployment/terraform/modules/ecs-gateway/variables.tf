# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# ecs-gateway 모듈 변수 — ECS Fargate 소규모 배포 경로 (T1/T2)
# ------------------------------------------------------------------------------
# EKS 경로의 IRSA·ESO·helm·ALB-controller 계층을 통째로 없앤다:
#   - 시크릿: task definition `secrets:` 가 Secrets Manager ARN 을 직접 참조
#     (ESO 대신). 실행 role 이 읽고 ECS agent 가 컨테이너 env 로 주입한다.
#   - DB/Redis 비밀번호는 helm 의 `$(DB_PASSWORD)` 치환과 같은 방식으로
#     컨테이너 command 래퍼(sh -c) 안에서 URL 을 조립한다 — 평문 비밀번호가
#     task def JSON 에 남지 않는다.
#   - 내부 서비스 간 호출(admin-ui → admin-api): Cloud Map DNS 서비스 디스커버리.
# ==============================================================================

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type = string
}

# ------------------------------------------------------------------------------
# 네트워크
# ------------------------------------------------------------------------------
variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  description = "Fargate task / ENI 가 뜨는 서브넷 (ECR·SM·Logs 는 NAT 또는 endpoint 경유)"
  type        = list(string)
}

variable "public_subnet_ids" {
  description = "ALB 서브넷. alb_internal=true 이면 무시되고 private 을 쓴다"
  type        = list(string)
  default     = []
}

variable "alb_internal" {
  description = "true 면 내부 ALB (network.mode=private). 사전조건: VPN/DX 경유 클라이언트 접근"
  type        = bool
  default     = false
}

variable "allowed_cidrs" {
  description = "ALB security group ingress 허용 대역. 비우면 0.0.0.0/0 — schema 경고와 같은 의미"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "alb_idle_timeout" {
  description = "ALB idle timeout (초). 반드시 stream_idle_timeout 보다 커야 한다 — 작거나 같으면 ALB 가 먼저 끊어 클라이언트가 truncated SSE 를 본다"
  type        = number
  default     = 300
}

variable "stream_idle_timeout" {
  description = "게이트웨이 SSE idle timeout (초). ALB 보다 작게 유지할 것"
  type        = number
  default     = 240
}

# ------------------------------------------------------------------------------
# 도메인 / TLS
# ------------------------------------------------------------------------------
variable "domain_name" {
  description = "기본 도메인 (예: gw.example.com). 비우면 ALB DNS + 포트별 HTTP 라우팅"
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route53 zone — 있으면 ACM 자동 발급+검증+443 리스너+레코드까지 같은 apply 에 완료"
  type        = string
  default     = ""
}

variable "certificate_arn" {
  description = "기존 ACM cert (ALB 와 같은 리전 필수). 있으면 domain_name + 443 사용, 없고 zone_id 없으면 HTTP 만"
  type        = string
  default     = ""
}

# ------------------------------------------------------------------------------
# 이미지
# ------------------------------------------------------------------------------
variable "image_registry" {
  description = "이미지 레지스트리 (예: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/llm-gateway). 비우면 이 모듈이 만든 ECR repo 를 쓴다"
  type        = string
  default     = ""
}

variable "image_tag" {
  description = "배포 이미지 태그 — 핀 필수. mutable 태그(latest) 금지"
  type        = string
}

variable "run_migration_task" {
  description = "true 면 apply 중 migration task 를 실행하고 서비스가 그 완료를 기다린다 (helm hook 에 해당)"
  type        = bool
  default     = true
}

# ------------------------------------------------------------------------------
# 앱 튜닝 — helm configmap(common/configmap.yaml)의 비민감 계약 이식.
# 기본값은 charts/llm-gateway/values.yaml 과 동일 — deepdive Q50 의 Redis/RL
# 복원력 설정은 load-bearing 이라 생략하면 프로덕션 동작이 달라진다.
# ------------------------------------------------------------------------------
variable "db_pool" {
  type = object({
    size = number, max_overflow = number, timeout = number, recycle = number
  })
  default = { size = 10, max_overflow = 20, timeout = 10, recycle = 3600 }
}

variable "redis_pool" {
  type = object({
    size    = number, socket_timeout = number, connect_timeout = number,
    retries = number, health_check_interval = number, read_from_replicas = bool
  })
  default = {
    size    = 150, socket_timeout = 2.0, connect_timeout = 1.0,
    retries = 1, health_check_interval = 30.0, read_from_replicas = false
  }
}

variable "rate_limit" {
  type = object({
    fail_mode              = string, breaker_enabled = bool,
    breaker_fail_threshold = number, breaker_recovery_timeout = number
  })
  default = {
    fail_mode              = "open", breaker_enabled = true,
    breaker_fail_threshold = 5, breaker_recovery_timeout = 5.0
  }
}

# 도메인 없이 시작할 때 두 번째 apply 가 ALB DNS 를 알아낸 뒤 채우는 override —
# NEXTAUTH_URL 등 task-def env 는 생성 시점에 외부 주소를 모르기 때문이다.
# deploy apply 가 tfvars 에 이 값들을 기록하므로 doctor 의 plan drift 에도 잡히지 않는다.
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

variable "create_ecr_repos" {
  description = "true 면 <project>/<svc> ECR repo 6개를 만든다 (첫 배포 경로)"
  type        = bool
  default     = true
}

# ------------------------------------------------------------------------------
# 서비스 사이징 — tier 프리셋이 이 map 으로 내려온다
# ------------------------------------------------------------------------------
variable "service_sizing" {
  description = "서비스별 cpu(1024=1vCPU)/memory(MiB)/desired_count. 키는 locals.service_names 참조"
  type = map(object({
    cpu           = number
    memory        = number
    desired_count = number
  }))
  default = {}
}

# ------------------------------------------------------------------------------
# 데이터 저장소 (기존 모듈 출력을 그대로 받는다)
# ------------------------------------------------------------------------------
variable "db_host" {
  type = string
}

variable "db_port" {
  type    = number
  default = 5432
}

variable "db_name" {
  type    = string
  default = "gateway"
}

variable "db_app_user" {
  description = "애플리케이션 DB 유저 — migration task 가 생성/권한부여 (run_migration.sh 의 APP_DB_USER)"
  type        = string
  default     = "gateway"
}

variable "db_master_username" {
  type    = string
  default = "postgres_admin"
}

variable "db_master_secret_arn" {
  description = "Aurora managed master secret ARN (module.aurora.master_user_secret_arn)"
  type        = string
}

variable "db_security_group_id" {
  description = "Aurora SG — 5432 ingress 에 task SG 를 추가한다"
  type        = string
}

variable "redis_host" {
  type = string
}

variable "redis_port" {
  type    = number
  default = 6379
}

variable "redis_auth_token_secret_arn" {
  description = "ElastiCache AUTH token secret ARN (module.elasticache.auth_token_secret_arn) — 평문 문자열 secret"
  type        = string
  default     = ""
}

variable "redis_tls_enabled" {
  type    = bool
  default = true # elasticache-valkey 모듈은 transit_encryption_enabled=true 고정
}

variable "redis_security_group_id" {
  type    = string
  default = ""
}

variable "redis_cluster_mode" {
  type    = bool
  default = false
}

# ------------------------------------------------------------------------------
# OIDC / 인증 — gateway.yaml oidc 블록 + Cognito 모듈 출력
# ------------------------------------------------------------------------------
variable "oidc_issuer_url" {
  type    = string
  default = ""
}

variable "oidc_audience" {
  type    = string
  default = ""
}

variable "oidc_client_id" {
  type    = string
  default = ""
}

variable "oidc_client_secret" {
  description = "confidential client secret — SM 의 app secret JSON 에 저장된다"
  type        = string
  sensitive   = true
  default     = ""
}

variable "oidc_authorize_url" {
  type    = string
  default = ""
}

variable "oidc_token_url" {
  type    = string
  default = ""
}

variable "oidc_provider_name" {
  type    = string
  default = "oidc:cognito"
}

variable "oidc_required_group" {
  type    = string
  default = ""
}

variable "dev_login_enabled" {
  description = "OIDC 없을 때 admin-api/admin-ui 의 dev login 허용 — oidc.enabled 면 반드시 false"
  type        = bool
  default     = true
}

variable "cognito_user_pool_arn" {
  description = "admin-api 의 사용자/그룹 동기화 권한 범위 (module.cognito.user_pool_arn)"
  type        = string
  default     = ""
}

variable "cognito_user_pool_id" {
  description = "admin-api COGNITO_USER_POOL_ID — 비우면 Cognito sync 비활성"
  type        = string
  default     = ""
}

# ADMIN 부트스트랩 — helm 의 adminApi.adminBootstrap 과 같은 계약.
# 비워두면 아무도 ADMIN 이 못 되어 콘솔이 잠긴다 (kiro 리뷰 H1/H2).
variable "admin_groups" {
  description = "ADMIN 역할을 부여할 IdP 그룹 — env 는 cognito_groups 로 연결한다"
  type        = list(string)
  default     = []
}

variable "admin_emails" {
  description = "ADMIN 역할을 부여할 이메일 — 그룹 없이 첫 관리자를 올릴 때"
  type        = list(string)
  default     = []
}

variable "allowed_sts_regions" {
  type    = list(string)
  default = ["ap-northeast-2"]
}

variable "allowed_iam_roles" {
  description = "CLI presigned GetCallerIdentity 검증에 허용하는 IAM role 패턴 (빈 리스트 = CLI 인증 불가)"
  type        = list(string)
  default     = []
}

# ------------------------------------------------------------------------------
# Bedrock / cross-account
# ------------------------------------------------------------------------------
variable "bedrock_allowed_model_arns" {
  type    = list(string)
  default = ["*"]
}

variable "mantle_regions" {
  type    = list(string)
  default = ["ap-northeast-1", "us-east-2"]
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
# 기능 플래그가 만드는 app env + 권한
# ------------------------------------------------------------------------------
variable "email_sender_type" {
  description = "mock | ses | smtp | internal_api"
  type        = string
  default     = "mock"
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

variable "body_log_firehose_name" {
  description = "body_logging 모듈의 delivery stream 이름 — 빈 문자열이면 env/IAM 둘 다 미렌더"
  type        = string
  default     = ""
}

variable "body_log_firehose_arn" {
  type    = string
  default = ""
}

variable "body_log_bucket_arn" {
  type    = string
  default = ""
}

variable "agentcore_gateway_url" {
  description = "Web Search AgentCore Gateway URL (us-east-1 전용 — 구조적 리전 제약)"
  type        = string
  default     = ""
}

variable "reporting_timezone" {
  type    = string
  default = "Asia/Seoul"
}

variable "tags" {
  type    = map(string)
  default = {}
}
