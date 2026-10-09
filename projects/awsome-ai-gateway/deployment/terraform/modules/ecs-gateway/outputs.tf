# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  value = aws_ecs_cluster.this.arn
}

output "alb_dns_name" {
  value = aws_lb.this.dns_name
}

output "alb_zone_id" {
  value = aws_lb.this.zone_id
}

output "alb_security_group_id" {
  value = aws_security_group.alb.id
}

output "task_security_group_id" {
  value = aws_security_group.tasks.id
}

output "service_names" {
  value = { for k, v in aws_ecs_service.svc : k => v.name }
}

output "task_definition_arns" {
  value = { for k, v in aws_ecs_task_definition.svc : k => v.arn }
}

# ------------------------------------------------------------------------------
# 접속 URL — deploy doctor / init 출력 / 클라이언트 설정에 쓰인다
# ------------------------------------------------------------------------------
output "gateway_url" {
  value = var.domain_name != "" ? "https://gateway.${var.domain_name}" : "http://${aws_lb.this.dns_name}:8000"
}

output "admin_api_url" {
  value = var.domain_name != "" ? "https://admin-api.${var.domain_name}" : "http://${aws_lb.this.dns_name}:8080"
}

output "admin_ui_url" {
  value = var.domain_name != "" ? "https://admin.${var.domain_name}" : "http://${aws_lb.this.dns_name}:3000"
}

output "https_enabled" {
  value = local.https_ready
}

output "ecr_repository_urls" {
  value = { for k, v in aws_ecr_repository.svc : k => v.repository_url }
}

output "app_secret_arn" {
  value = aws_secretsmanager_secret.app.arn
}

output "db_app_secret_arn" {
  value = aws_secretsmanager_secret.db_app.arn
}

output "cert_validation_records" {
  description = "zone_id 없이 domain 만 있을 때 수동으로 만들어야 할 DNS 검증 레코드"
  value = var.domain_name != "" && var.certificate_arn == "" ? [
    for dvo in aws_acm_certificate.this[0].domain_validation_options : {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  ] : []
}

output "managed_certificate_arn" {
  description = "모듈이 발급한 ACM cert — ISSUED 후 certificate_arn 으로 다시 넣으면 HTTPS 전환"
  value       = var.domain_name != "" && var.certificate_arn == "" ? aws_acm_certificate.this[0].arn : ""
}
