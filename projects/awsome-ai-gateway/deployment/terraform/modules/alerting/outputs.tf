# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "sns_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "sns_topic_name" {
  value = aws_sns_topic.alerts.name
}

output "alarm_count" {
  value = (length(aws_cloudwatch_metric_alarm.aurora_cpu)
    + length(aws_cloudwatch_metric_alarm.aurora_memory)
    + length(aws_cloudwatch_metric_alarm.cache_cpu)
    + length(aws_cloudwatch_metric_alarm.cache_memory)
  + length(aws_cloudwatch_metric_alarm.cache_connections))
}
