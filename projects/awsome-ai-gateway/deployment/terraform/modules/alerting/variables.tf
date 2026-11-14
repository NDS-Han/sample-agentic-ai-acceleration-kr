# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "aurora_cluster_id" {
  description = "Aurora cluster identifier — RDS event subscription 대상"
  type        = string
}

variable "aurora_instance_ids" {
  description = "Aurora instance identifiers — alarm dimension. module.aurora-postgresql.instance_identifiers output 으로 전달 (data source 금지 — bootstrap plan 안전)"
  type        = list(string)
}

variable "elasticache_member_ids" {
  description = "ElastiCache member cluster ids — alarm dimension. module.elasticache-valkey.member_cluster_ids output 으로 전달 (토폴로지에서 결정적 유도)"
  type        = list(string)
}

variable "alert_emails" {
  description = "SNS email subscriptions — created PENDING until the recipient confirms the email (console/email step)"
  type        = list(string)
  default     = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
