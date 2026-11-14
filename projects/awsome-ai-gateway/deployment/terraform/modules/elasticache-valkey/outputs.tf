# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "primary_endpoint_address" {
  description = "Writer endpoint (non-cluster mode)"
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "configuration_endpoint_address" {
  description = "Cluster mode configuration endpoint (prod)"
  value       = aws_elasticache_replication_group.this.configuration_endpoint_address
}

output "reader_endpoint_address" {
  value = aws_elasticache_replication_group.this.reader_endpoint_address
}

output "port" {
  value = aws_elasticache_replication_group.this.port
}

output "auth_token_secret_arn" {
  description = "AUTH 토큰이 저장된 Secrets Manager ARN"
  value       = aws_secretsmanager_secret.auth_token.arn
}

output "auth_token_secret_name" {
  description = "AUTH 토큰 Secrets Manager name (ESO에서 참조)"
  value       = aws_secretsmanager_secret.auth_token.name
}

output "security_group_id" {
  value = aws_security_group.this.id
}

# CloudWatch alarm dimension 용 멤버 클러스터 id — AWS 명명이 토폴로지에서
# 결정적이므로 plan 시점에 계산 가능 (non-cluster: <rg>-001…, cluster mode:
# <rg>-0001-001…). resource attribute(member_clusters)는 apply 후라 못 쓴다.
output "member_cluster_ids" {
  value = (local.is_prod
    ? flatten([
      for g in range(var.prod_num_node_groups) : [
        for i in range(var.prod_replicas_per_node_group + 1) :
        "${var.project}-${var.environment}-${format("%04d-%03d", g + 1, i + 1)}"
      ]
    ])
    : [for i in range(local.num_cache_clusters) :
  "${var.project}-${var.environment}-${format("%03d", i + 1)}"])
}
