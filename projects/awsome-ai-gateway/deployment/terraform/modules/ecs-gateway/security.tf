# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# 보안 그룹 — alb / task 두 장. task→task (admin-ui→admin-api 디스커버리) 는
# task SG self-reference 로 허용한다.
# ==============================================================================

resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb"
  description = "ALB ingress — allowed_cidrs 만"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${local.name_prefix}-alb" })
}

resource "aws_security_group_rule" "alb_ingress" {
  for_each = {
    for pair in setproduct(var.allowed_cidrs, local.alb_ingress_ports) :
    "${pair[0]}:${pair[1]}" => { cidr = pair[0], port = pair[1] }
  }

  type              = "ingress"
  security_group_id = aws_security_group.alb.id
  cidr_blocks       = [each.value.cidr]
  from_port         = each.value.port
  to_port           = each.value.port
  protocol          = "tcp"
}

locals {
  # ALB 가 실제로 보내는 포트만 — alb=true 서비스의 컨테이너 포트
  alb_service_ports = [for k, v in local.services : v.port if v.alb]
}

resource "aws_security_group_rule" "alb_egress" {
  for_each = toset([for p in local.alb_service_ports : tostring(p)])

  type              = "egress"
  security_group_id = aws_security_group.alb.id
  # task SG 로의 트래픽은 task 측 ingress 가 소스 SG 로 허용하므로 ALB egress 는
  # 목적지 SG + 서비스 포트만 (cidr 0.0.0.0/0 보다 좁다)
  source_security_group_id = aws_security_group.tasks.id
  from_port                = tonumber(each.key)
  to_port                  = tonumber(each.key)
  protocol                 = "tcp"
}

# ------------------------------------------------------------------------------
# task SG — ALB 로부터의 서비스 포트 + self (admin-ui → admin-api 디스커버리 호출)
# ------------------------------------------------------------------------------
resource "aws_security_group" "tasks" {
  name        = "${local.name_prefix}-tasks"
  description = "ECS task ENI — ALB 와 자기 자신, 그리고 DB/Redis egress"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${local.name_prefix}-tasks" })
}

resource "aws_security_group_rule" "tasks_from_alb" {
  for_each = toset([for p in local.alb_service_ports : tostring(p)])

  type                     = "ingress"
  security_group_id        = aws_security_group.tasks.id
  source_security_group_id = aws_security_group.alb.id
  from_port                = tonumber(each.key)
  to_port                  = tonumber(each.key)
  protocol                 = "tcp"
}

resource "aws_security_group_rule" "tasks_self" {
  # 유일한 내부 호출은 admin-ui → admin-api(:8080) — 그 포트만 자기 자신에 연다
  type                     = "ingress"
  security_group_id        = aws_security_group.tasks.id
  source_security_group_id = aws_security_group.tasks.id
  from_port                = local.services["admin-api"].port
  to_port                  = local.services["admin-api"].port
  protocol                 = "tcp"
  description              = "admin-ui to admin-api service-discovery calls"
}

resource "aws_security_group_rule" "tasks_egress" {
  type              = "egress"
  security_group_id = aws_security_group.tasks.id
  cidr_blocks       = ["0.0.0.0/0"]
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  # DB/Redis/ECR/SM/Logs/Bedrock egress — NAT 또는 VPC endpoint 경유
}

# ------------------------------------------------------------------------------
# 데이터 저장소 SG 에 task SG 를 ingress 로 추가
# ------------------------------------------------------------------------------
resource "aws_security_group_rule" "db_from_tasks" {
  type                     = "ingress"
  security_group_id        = var.db_security_group_id
  source_security_group_id = aws_security_group.tasks.id
  from_port                = var.db_port
  to_port                  = var.db_port
  protocol                 = "tcp"
  description              = "ECS tasks to Aurora"
}

# ElastiCache 쪽 규칙은 만들지 않는다 — elasticache-valkey 모듈의 SG 가
# private_subnet_cidrs 전체를 inline ingress 로 이미 허용한다(태스크는 private
# 서브넷에 있다). inline 블록과 외부 aws_security_group_rule 을 같은 SG 에
# 섞으면 apply 마다 규칙이 왕복하는 영구 drift 가 된다 (kiro 리뷰 H3).
