# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "engine_version" {
  description = "Aurora PostgreSQL 엔진 버전. 참고: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Updates.html"
  type        = string
  default     = "16.11"
}

variable "database_name" {
  type    = string
  default = "gateway"
}

variable "master_username" {
  description = "마스터 유저명. 실제 gateway 애플리케이션 유저는 마이그레이션 단계에서 별도 생성"
  type        = string
  default     = "postgres_admin"
}

variable "vpc_id" {
  type = string
}

variable "db_subnet_group_name" {
  description = "VPC 모듈의 db subnet group name"
  type        = string
}

variable "private_subnet_cidrs" {
  description = "Aurora 접근 허용 CIDR (EKS private subnet)"
  type        = list(string)
}

variable "availability_zones" {
  # 비워두면 AWS 가 DBSubnetGroup 의 AZ 중 자동 선택. 기본적으로 비워두는 것을 권장:
  # `availability_zones` 는 create-only 속성이라 subnet group 과 불일치가 생기면
  # cluster 전체 replace 가 트리거되어 데이터 손실 위험이 생기기 때문.
  type    = list(string)
  default = []
}

variable "prod_instance_class" {
  description = "Prod용 인스턴스 클래스 (dev는 Serverless v2)"
  type        = string
  default     = "db.r7g.large"
}

# ------------------------------------------------------------------------------
# 명시적 토폴로지 — environment=="prod" 이진 분기의 대체/보조
# ------------------------------------------------------------------------------
# "" 이면 기존 동작(environment=="prod" → provisioned). 명시하면 그 값이 이긴다:
#   "serverless"   → Serverless v2 (serverless_min/max_acu 사용)
#   "provisioned"  → prod_instance_class + Multi-AZ (기존 prod 경로)
# ECS gateway 같은 새 배포 경로는 environment 이름과 사이징을 분리하기 위해 이
# 변수를 쓴다 — 예: environment="acme-prod" 여도 T1 크기로 시작할 수 있다.
variable "db_mode" {
  description = "'' | 'serverless' | 'provisioned' — DB 토폴로지 명시 (기본: environment 기반)"
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "serverless", "provisioned"], var.db_mode)
    error_message = "db_mode 는 '', 'serverless', 'provisioned' 중 하나여야 합니다."
  }
}

variable "serverless_min_acu" {
  description = "Serverless v2 최소 ACU — T1 권장 0.5 (저빈도 admin 쿼리 지연이 싫으면 1.0)"
  type        = number
  default     = 0.5
}

variable "serverless_max_acu" {
  description = "Serverless v2 최대 ACU — T1 권장 4.0, T2 는 16 까지 여유"
  type        = number
  default     = 4.0
}

variable "safeguards" {
  description = "데이터 안전장치(deletion protection / final snapshot / 백업 14d) 명시 override. null 이면 db_mode/provisioned·environment 로 추론 — serverless+운영환경이면 true 권장"
  type        = bool
  default     = null
}

variable "kms_key_id" {
  description = "스토리지 암호화용 KMS key ID"
  type        = string
  default     = null # null 이면 AWS 관리 키 (alias/aws/rds)
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ------------------------------------------------------------------------------
# RDS Proxy (선택) — Aurora 앞단 connection pool
# ------------------------------------------------------------------------------
# EKS Pod 가 수백~수천 연결을 만들어도 Aurora 실제 연결은 Proxy 가 풀링.
# 애플리케이션 코드 변경 없음 (엔드포인트만 Proxy 로 교체).
variable "enable_rds_proxy" {
  description = "Aurora 앞단에 RDS Proxy 배치 여부 (connection pool 목적). prod 부하 테스트/운영 시 권장."
  type        = bool
  default     = false
}

variable "proxy_private_subnet_ids" {
  description = "RDS Proxy 가 배치될 private subnet ID (EKS 와 같은 VPC)."
  type        = list(string)
  default     = []
}

variable "proxy_max_connections_percent" {
  description = "Aurora max_connections 대비 Proxy 가 사용할 최대 비율 (%)"
  type        = number
  default     = 90
}

variable "proxy_max_idle_connections_percent" {
  description = "Aurora max_connections 대비 Proxy 가 유휴로 유지할 최대 비율 (%)"
  type        = number
  default     = 50
}

variable "proxy_connection_borrow_timeout" {
  description = "클라이언트가 Proxy 연결을 대기할 최대 시간 (초)"
  type        = number
  default     = 120
}

variable "proxy_idle_client_timeout" {
  description = "유휴 클라이언트 연결을 Proxy 가 닫기까지 대기 시간 (초)"
  type        = number
  default     = 1800
}

variable "proxy_require_tls" {
  description = "클라이언트 → Proxy TLS 요구 여부"
  type        = bool
  default     = true
}
