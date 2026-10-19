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

  # desired_count 의 소유자는 아래 appautoscaling_target 이다 — 오토스케일된
  # 값을 terraform apply 가 리셋하지 않게 무시한다. 기저(desired) 변경은
  # target 의 min/max 경계로 전파된다(min 올리면 즉시 스케일아웃,
  # max 내리면 클램프로 스케일인).
  lifecycle {
    ignore_changes = [desired_count]
  }

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

# ==============================================================================
# Service Autoscaling — 모든 서비스에 target 을 달아 desired_count 소유권을
# 통일한다(싱글톤은 min=max=desired 로 핀). max_count > desired 인 서비스만
# 정책이 붙는다 — 렌더러는 gateway-proxy(×4)/admin-api(×2) 에만 상한을 준다.
#
# 지표: CPU 60% + Memory 70% target tracking. SSE 장시간 커넥션은
# ALBRequestCountPerTarget 이 실제 동시성을 과소계측(스트림은 완료 전까지
# 요청으로 세지 않음)하므로 커넥션 버퍼를 반영하는 메모리가 더 나은 프록시다.
# scale-in 은 deregistration_delay(300s) 동안 드레인 — stream_idle_timeout
# (240s) 보다 길어 진행 중 스트림은 안전하게 종료된다.
#
# ⚠️ DB 커넥션 예산: max_count 상한은 deploy 도구가 worst-case pool 수요
#    (pool+overflow × 프로세스 × task) ≤ Aurora max_acu × ~225conn 의 80% 로
#    검증한다. 수동으로 max_count 를 키우면 같은 예산을 넘지 않는지 확인할 것 —
#    구조적 해결은 RDS Proxy(eks prod 가 이미 사용)다.
# ==============================================================================

resource "aws_appautoscaling_target" "svc" {
  for_each = aws_ecs_service.svc

  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${each.value.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  # optional 키는 누락 시 null 이라 try() 단독으로는 null 을 그대로 돌려준다 —
  # coalesce 로 desired_count 폴백 (default_sizing 대체분도 키 자체가 없어 동일 경로)
  min_capacity = coalesce(try(local.sizing[each.key].min_count, null), local.sizing[each.key].desired_count)
  max_capacity = coalesce(try(local.sizing[each.key].max_count, null), local.sizing[each.key].desired_count)
}

resource "aws_appautoscaling_policy" "cpu" {
  for_each = {
    for k, v in local.sizing : k => v
    if k != "migration" && coalesce(try(v.max_count, null), v.desired_count) > v.desired_count
  }

  name               = "${local.name_prefix}-${each.key}-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.svc[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.svc[each.key].scalable_dimension
  service_namespace  = aws_appautoscaling_target.svc[each.key].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value = 60
  }
}

resource "aws_appautoscaling_policy" "memory" {
  for_each = aws_appautoscaling_policy.cpu

  name               = "${local.name_prefix}-${each.key}-memory"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.svc[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.svc[each.key].scalable_dimension
  service_namespace  = aws_appautoscaling_target.svc[each.key].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageMemoryUtilization"
    }
    target_value = 70
  }
}
