# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# ALB + ACM — 세 라우팅 모드
#
#   https_ready : 443 host 기반 (gateway./admin-api./admin.) + 80→443 리다이렉트
#   pending     : domain 있으나 cert 미발급 → 80 host 기반 + 출력에 검증 레코드
#   none        : domain 없음 → 8000/8080/3000 포트별 HTTP (ALB DNS 이름)
#
# ⚠️ ACM cert 는 ALB 와 **같은 리전** 이어야 한다 (CloudFront cert 와 달리
#    us-east-1 고정이 아니다). var.certificate_arn 지참 시 리전 확인할 것.
# ==============================================================================

resource "aws_lb" "this" {
  name               = local.name_prefix
  internal           = var.alb_internal
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.alb_internal ? var.private_subnet_ids : var.public_subnet_ids

  idle_timeout = var.alb_idle_timeout

  tags = var.tags
}

# ------------------------------------------------------------------------------
# ACM — domain_name + hosted_zone_id 면 발급+DNS검증을 같은 apply 에 완료.
# zone_id 없으면 cert 만 만들고 검증 레코드를 output 으로 내보낸다 (수동 DNS).
# ------------------------------------------------------------------------------
resource "aws_acm_certificate" "this" {
  count = var.domain_name != "" && var.certificate_arn == "" ? 1 : 0

  domain_name = "*.${var.domain_name}"
  # wildcard 하나로 gateway./admin-api./admin. 세 호스트를 다 덮는다
  subject_alternative_names = [var.domain_name]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = var.tags
}

resource "aws_route53_record" "cert_validation" {
  for_each = var.domain_name != "" && var.certificate_arn == "" && var.hosted_zone_id != "" ? {
    for dvo in aws_acm_certificate.this[0].domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  } : {}

  allow_overwrite = true
  name            = each.value.name
  records         = [each.value.record]
  ttl             = 60
  type            = each.value.type
  zone_id         = var.hosted_zone_id
}

resource "aws_acm_certificate_validation" "this" {
  count = var.domain_name != "" && var.certificate_arn == "" && var.hosted_zone_id != "" ? 1 : 0

  certificate_arn         = aws_acm_certificate.this[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

locals {
  # 실제로 리스너에 붙일 cert — 지참 cert > 자동발급(검증 완료)
  effective_cert_arn = var.certificate_arn != "" ? var.certificate_arn : (
    var.hosted_zone_id != "" && var.domain_name != "" ? aws_acm_certificate_validation.this[0].certificate_arn : ""
  )
}

# ------------------------------------------------------------------------------
# Target Groups — ip target (Fargate ENI)
# ------------------------------------------------------------------------------
resource "aws_lb_target_group" "svc" {
  for_each = { for k, v in local.services : k => v if v.alb }

  # TG 이름은 32자 상한 — 긴 env 이름이 잘리면 서비스 간 충돌할 수 있어
  # prefix + md5 suffix 조합으로 유일성을 보장한다 (총 31자 이하)
  name        = "${substr("${local.name_prefix}-${each.key}", 0, 24)}-${substr(md5("${local.name_prefix}-${each.key}"), 0, 6)}"
  port        = each.value.port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  health_check {
    path                = each.value.health
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
    matcher             = "200"
  }

  tags = var.tags
}

# ------------------------------------------------------------------------------
# 리스너 — https_ready 모드
# ------------------------------------------------------------------------------
resource "aws_lb_listener" "https" {
  count = local.https_ready ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.effective_cert_arn

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      status_code  = "404"
      message_body = "not found"
    }
  }
}

resource "aws_lb_listener_rule" "https_hosts" {
  for_each = local.https_ready ? local.host_routes : {}

  listener_arn = aws_lb_listener.https[0].arn
  priority     = index(keys(local.host_routes), each.key) + 1

  condition {
    host_header {
      values = [each.key]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.svc[each.value].arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  count = local.https_ready ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# ------------------------------------------------------------------------------
# 리스너 — pending 모드 (domain 있으나 cert 미발급): 80 host 기반.
# cert 가 발급되면 certificate_arn 을 넣거나 zone_id 를 채워 재apply → https_ready 로 전환.
# ------------------------------------------------------------------------------
resource "aws_lb_listener" "http_hosts" {
  count = !local.https_ready && !local.http_mode ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      status_code  = "404"
      message_body = "not found"
    }
  }
}

resource "aws_lb_listener_rule" "http_hosts" {
  for_each = !local.https_ready && !local.http_mode ? local.host_routes : {}

  listener_arn = aws_lb_listener.http_hosts[0].arn
  priority     = index(keys(local.host_routes), each.key) + 1

  condition {
    host_header {
      values = [each.key]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.svc[each.value].arn
  }
}

# ------------------------------------------------------------------------------
# 리스너 — none 모드 (domain 없음): 포트별 HTTP 라우팅
# ------------------------------------------------------------------------------
resource "aws_lb_listener" "http_ports" {
  for_each = local.http_mode ? local.port_routes : {}

  load_balancer_arn = aws_lb.this.arn
  port              = each.key
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.svc[each.value].arn
  }
}

# ------------------------------------------------------------------------------
# Route53 레코드 — zone_id 가 있으면 서비스 호스트를 ALB 로 alias
# ------------------------------------------------------------------------------
resource "aws_route53_record" "svc" {
  for_each = var.domain_name != "" && var.hosted_zone_id != "" ? local.host_routes : {}

  zone_id = var.hosted_zone_id
  name    = each.key
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}
