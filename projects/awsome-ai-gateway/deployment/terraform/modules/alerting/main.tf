# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ------------------------------------------------------------------------------
# alerting — infra health notifications
#
# SNS topic + CloudWatch alarms for the managed-service layer (Aurora,
# ElastiCache). ALB/pod-level signals are deliberately out of scope — pod
# health is the Prometheus/Alertmanager path (deployment/observability),
# and ALB dimensions are controller-generated (not knowable at plan time).
#
# member id 목록은 data source 로 읽지 않고 호출 env 가 모듈 output
# (instance_identifiers / member_cluster_ids — 토폴로지에서 결정적)으로 넘긴다.
# data source 는 대상 리소스를 같은 plan 에서 만들 때 for_each plan-time
# 에러를 내므로 bootstrap 경로를 깬다.
# ------------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-${var.environment}-alerts"
  tags = var.tags
}

# Email subscriptions stay PENDING until the recipient clicks the
# confirmation link — that step is manual by design (cannot be automated).
resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(var.alert_emails)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

# ------------------------------------------------------------------------------
# Aurora — RDS event subscription (failover/failure/maintenance) + per-instance
# CPU/memory alarms.
# ------------------------------------------------------------------------------

resource "aws_db_event_subscription" "aurora" {
  name             = "${var.project}-${var.environment}-aurora-events"
  sns_topic        = aws_sns_topic.alerts.arn
  source_type      = "db-cluster"
  source_ids       = [var.aurora_cluster_id]
  event_categories = ["failover", "failure", "maintenance", "notification"]
}

resource "aws_cloudwatch_metric_alarm" "aurora_cpu" {
  for_each            = toset(var.aurora_instance_ids)
  alarm_name          = "${var.project}-${var.environment}-aurora-${each.value}-cpu"
  alarm_description   = "Aurora instance ${each.value} CPU > 85%"
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  dimensions          = { DBInstanceIdentifier = each.value }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# FreeableMemory alarm 은 의도적으로 없다 — serverless v2(min_acu 0.5)에서는
# 유휴 상태에서도 상시 저수위라 첫날부터 오탐이 된다. failover/failure 는 위
# event subscription 이 덮고, provisioned env 의 메모리 압박은 필요 시 env
# tfvars 로 추가한다.

# ------------------------------------------------------------------------------
# ElastiCache — per-member CPU/memory/connections alarms.
# ------------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "cache_cpu" {
  for_each            = toset(var.elasticache_member_ids)
  alarm_name          = "${var.project}-${var.environment}-cache-${each.value}-cpu"
  alarm_description   = "ElastiCache ${each.value} EngineCPUUtilization > 75%"
  namespace           = "AWS/ElastiCache"
  metric_name         = "EngineCPUUtilization"
  dimensions          = { CacheClusterId = each.value }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 75
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "cache_memory" {
  for_each            = toset(var.elasticache_member_ids)
  alarm_name          = "${var.project}-${var.environment}-cache-${each.value}-mem"
  alarm_description   = "ElastiCache ${each.value} DatabaseMemoryUsagePercentage > 85%"
  namespace           = "AWS/ElastiCache"
  metric_name         = "DatabaseMemoryUsagePercentage"
  dimensions          = { CacheClusterId = each.value }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "cache_connections" {
  for_each            = toset(var.elasticache_member_ids)
  alarm_name          = "${var.project}-${var.environment}-cache-${each.value}-conn"
  alarm_description   = "ElastiCache ${each.value} CurrConnections > 50,000 (노드 상한 ~65k 기준 80%)"
  namespace           = "AWS/ElastiCache"
  metric_name         = "CurrConnections"
  dimensions          = { CacheClusterId = each.value }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 50000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}
