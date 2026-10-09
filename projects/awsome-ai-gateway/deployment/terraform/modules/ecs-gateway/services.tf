# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# ECS Services — ALB 부착 서비스 3 + 백그라운드 3 (migration 은 서비스가 아님)
#
# deployment_circuit_breaker: 헬스하지 않은 배포를 자동 롤백 — EKS 의 수동
# helm rollback 과 달리 배포 실패가 서비스 중단으로 번지지 않는다.
# ==============================================================================

resource "aws_ecs_service" "svc" {
  for_each = { for k, v in local.services : k => v if k != "migration" }

  name            = each.key
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.svc[each.key].arn
  desired_count   = local.sizing[each.key].desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = each.value.alb ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.svc[each.key].arn
      container_name   = each.key
      container_port   = each.value.port
    }
  }

  # 헬스하지 않은 신규 배포를 자동으로 이전 task def 로 되돌린다
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  health_check_grace_period_seconds = each.value.alb ? 90 : null

  # admin-ui → admin-api 유일한 내부 호출의 디스커버리 등록
  dynamic "service_registries" {
    for_each = each.key == "admin-api" ? [1] : []
    content {
      registry_arn = aws_service_discovery_service.admin_api.arn
    }
  }

  # migration terraform_data 가 완료돼야 서비스가 뜬다 — 스키마 없는 상태의
  # 크래시루프 + circuit breaker 오작동 방지 (EKS helm hook 과 같은 순서)
  depends_on = [terraform_data.migration]

  tags = var.tags
}
