# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# deploy apply / doctor 가 읽는 출력 — 클라이언트 배포와 검증에 필요한 값
# ==============================================================================

output "gateway_url" {
  value       = module.gateway.gateway_url
  description = "Claude Code/Codex/Cowork 가 쓰는 게이트웨이 base URL"
}

output "admin_ui_url" {
  value       = module.gateway.admin_ui_url
  description = "관리자 웹 콘솔"
}

output "admin_api_url" {
  value       = module.gateway.admin_api_url
  description = "관리자 REST API (onboarding config 등)"
}

output "https_enabled" {
  value       = module.gateway.https_enabled
  description = "false 면 HTTP — Cowork 는 https 가 필수라 도메인 확정 전까지 미동작"
}

output "alb_dns_name" {
  value = module.gateway.alb_dns_name
}

output "ecs_cluster_name" {
  value = module.gateway.cluster_name
}

output "cognito_user_pool_id" {
  value = module.cognito.user_pool_id
}

output "cognito_client_id" {
  value       = module.cognito.client_id
  description = "gateway-cli / admin-ui OIDC client id"
}

output "cognito_issuer_url" {
  value = module.cognito.issuer_url
}

output "cognito_hosted_ui_domain" {
  value = module.cognito.hosted_ui_domain
}

output "ecr_repository_urls" {
  value       = module.gateway.ecr_repository_urls
  description = "deploy apply 가 이미지를 push 하는 대상 — 빈 map 이면 외부 registry 사용 중"
}

output "cert_validation_records" {
  value       = module.gateway.cert_validation_records
  description = "zone_id 미제공 시 수동으로 만들어야 할 DNS 검증 레코드 — 만들고 나면 cert 가 ISSUED 되고, certificate_arn 을 tfvars 에 넣어 재apply 하면 HTTPS 전환"
}

output "migration_task_definition_arn" {
  value = module.gateway.task_definition_arns["migration"]
}

# deploy apply 가 migration run-task 를 띄울 때 쓰는 네트워크 설정
output "private_subnet_ids" {
  value = module.vpc.private_subnet_ids
}

output "task_security_group_id" {
  value = module.gateway.task_security_group_id
}

output "service_names" {
  value = module.gateway.service_names
}

output "db_endpoint" {
  value = module.aurora.cluster_endpoint
}

output "redis_endpoint" {
  value = module.elasticache.primary_endpoint_address
}

# ─── admin-chat-agent (enable_chat_agent=true 일 때만) ───
output "chat_agent_ecr_url" {
  value       = try(module.agentcore_runtime[0].ecr_repository_url, null)
  description = "admin-chat-agent 컨테이너 image 를 push 할 ECR URL"
}
output "chat_agent_execution_role_arn" {
  value       = try(module.agentcore_runtime[0].agent_execution_role_arn, null)
  description = "AgentCore CreateAgentRuntime 호출 시 전달할 execution role ARN"
}
output "chat_agent_staging_bucket" {
  value       = try(module.agentcore_runtime[0].staging_bucket_name, null)
  description = "SQL → Code Specialist 데이터 전달용 S3 staging bucket"
}
output "chat_agent_name" {
  value       = try(module.agentcore_runtime[0].agent_name, null)
  description = "AgentCore Runtime 등록 시 사용할 agent name"
}
output "chat_agent_query_db_function" {
  value       = try(module.agentcore_runtime[0].query_db_function_name, null)
  description = "agent/admin-api 의 LAMBDA_QUERY_DB env 값"
}
output "chat_agent_get_schema_function" {
  value       = try(module.agentcore_runtime[0].get_schema_function_name, null)
  description = "agent/admin-api 의 LAMBDA_GET_SCHEMA env 값"
}
output "chat_agent_reader_secret_arn" {
  value       = try(module.agentcore_runtime[0].chat_reader_secret_arn, null)
  description = "gateway_chat_reader 비밀번호 secret ARN — provisioner 가 동기화"
}
output "chat_reader_pw_task_definition_arn" {
  value       = try(module.gateway.chat_reader_pw_task_definition_arn, null)
  description = "chat_reader 비밀번호 동기화 task def — provisioner run-task 대상"
}

output "chat_reader_pw_log_group_name" {
  description = "chat_reader 비밀번호 동기화 task 로그 그룹 — provisioner 실패 힌트용"
  value       = try(module.gateway.chat_reader_pw_log_group_name, null)
}

output "alerting_sns_topic_arn" {
  value = try(module.alerting[0].sns_topic_arn, "")
}
