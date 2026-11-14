# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ------------------------------------------------------------------------------
# alerting — infra health notifications
#
# SNS topic + CloudWatch alarms for the managed-service layer (Aurora,
# ElastiCache). ALB/pod-level signals are deliberately out of scope — pod
# health is the Prometheus/Alertmanager path (deployment/observability),
# and ALB dimensions are controller-generated (not knowable at plan time).
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
# CPU/memory alarms. Member instance ids come from the live cluster so the
# module works for serverless and provisioned clusters alike.
# ------------------------------------------------------------------------------

data "aws_rds_cluster" "this" {
  cluster_identifier = var.aurora_cluster_id
}

resource "aws_db_event_subscription" "aurora" {
  name             = "${var.project}-${var.environment}-aurora-events"
  sns_topic        = aws_sns_topic.alerts.arn
  source_type      = "db-cluster"
  source_ids       = [var.aurora_cluster_id]
  event_categories = ["failover", "failure", "maintenance", "notification"]
}

resource "aws_cloudwatch_metric_alarm" "aurora_cpu" {
  for_each            = toset(data.aws_rds_cluster.this.cluster_members)
  alarm_name          = "${var.project}-${var.environment}-aurora-${each.key}-cpu"
  alarm_description   = "Aurora instance ${each.key} CPU > 85%"
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

resource "aws_cloudwatch_metric_alarm" "aurora_memory" {
  for_each            = toset(data.aws_rds_cluster.this.cluster_members)
  alarm_name          = "${var.project}-${var.environment}-aurora-${each.key}-mem"
  alarm_description   = "Aurora instance ${each.key} FreeableMemory < 256MiB"
  namespace           = "AWS/RDS"
  metric_name         = "FreeableMemory"
  dimensions          = { DBInstanceIdentifier = each.value }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 268435456 # 256 MiB
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# ------------------------------------------------------------------------------
# ElastiCache — per-member CPU/memory/connections alarms. Member cache-cluster
# ids resolved via data source (covers non-cluster and cluster-mode layouts).
# ------------------------------------------------------------------------------

data "aws_elasticache_replication_group" "this" {
  replication_group_id = var.elasticache_replication_group_id
}

resource "aws_cloudwatch_metric_alarm" "cache_cpu" {
  for_each            = data.aws_elasticache_replication_group.this.member_clusters
  alarm_name          = "${var.project}-${var.environment}-cache-${each.key}-cpu"
  alarm_description   = "ElastiCache ${each.key} EngineCPUUtilization > 75%"
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
  for_each            = data.aws_elasticache_replication_group.this.member_clusters
  alarm_name          = "${var.project}-${var.environment}-cache-${each.key}-mem"
  alarm_description   = "ElastiCache ${each.key} DatabaseMemoryUsagePercentage > 85%"
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
  for_each            = data.aws_elasticache_replication_group.this.member_clusters
  alarm_name          = "${var.project}-${var.environment}-cache-${each.key}-conn"
  alarm_description   = "ElastiCache ${each.key} CurrConnections > 80% of node max (65k)"
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
