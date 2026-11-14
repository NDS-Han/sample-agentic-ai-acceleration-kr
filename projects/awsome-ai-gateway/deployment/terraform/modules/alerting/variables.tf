# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "aurora_cluster_id" {
  description = "Aurora cluster identifier — per-instance alarms + event subscription target"
  type        = string
}

variable "elasticache_replication_group_id" {
  description = "ElastiCache replication group id — member clusters are resolved via data source"
  type        = string
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
