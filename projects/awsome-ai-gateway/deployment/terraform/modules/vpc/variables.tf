# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  description = "프로젝트 식별자 (예: llm-gateway)"
  type        = string
}

variable "environment" {
  # 예전엔 dev|staging|prod 로 제한했으나, tier 기반 배포 경로(gateway-ecs)는
  # 임의의 사이트 이름(예: acme-prod)을 환경명으로 쓴다. is_prod 성격의 분기는
  # db_mode/cache_mode/nat_ha 같은 명시 변수로 대체되므로 이름 제한은 푼다.
  description = "환경/사이트 이름 (예: dev, staging, prod, acme-prod)"
  type        = string
  validation {
    condition     = length(var.environment) > 0
    error_message = "environment must not be empty"
  }
}

variable "nat_ha" {
  description = "AZ 당 NAT 게이트웨이 (HA). null 이면 environment=='prod' 로 추론 — t1 은 false, t2+ 는 true 권장"
  type        = bool
  default     = null
}

variable "cidr" {
  description = "VPC CIDR block"
  type        = string
  default     = "10.0.0.0/16"
}

variable "azs" {
  description = "가용 영역 목록 (최소 2개, prod는 3개 권장)"
  type        = list(string)
}

variable "private_subnet_cidrs" {
  description = "워크로드용 private subnet CIDR 리스트 (Fargate Pod 배치)"
  type        = list(string)
}

variable "public_subnet_cidrs" {
  description = "ALB용 public subnet CIDR 리스트"
  type        = list(string)
}

variable "database_subnet_cidrs" {
  description = "Aurora 전용 격리 subnet CIDR 리스트"
  type        = list(string)
}

variable "elasticache_subnet_cidrs" {
  description = "ElastiCache 전용 격리 subnet CIDR 리스트"
  type        = list(string)
}

variable "aws_region" {
  description = "AWS region (VPC Endpoint 서비스명에 사용)"
  type        = string
  default     = "ap-northeast-2"
}

variable "tags" {
  description = "공통 태그"
  type        = map(string)
  default     = {}
}
