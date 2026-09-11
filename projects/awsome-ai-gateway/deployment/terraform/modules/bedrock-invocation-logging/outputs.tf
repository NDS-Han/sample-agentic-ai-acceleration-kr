# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "enabled" {
  description = "이 리전에 invocation logging(계정 전체 본문 수집)이 실제로 켜졌는가"
  value       = var.enabled
}

output "log_group_name" {
  description = "admin-api 의 BEDROCK_INVOCATION_LOG_GROUP 에 넣을 값. 미사용이면 빈 문자열"
  value       = var.enabled ? aws_cloudwatch_log_group.invocations[0].name : ""
}

output "log_group_arn" {
  description = <<-EOT
    irsa 모듈의 `bedrock_invocation_log_group_arn` 에 그대로 넣을 값.

    aws_cloudwatch_log_group.arn 은 끝에 `:*` 가 붙은 형태로 오는데, irsa 모듈은 접미사
    **없는** ARN 을 받아 자기가 `:*` 를 붙인다(두 statement 가 서로 다른 형태를 요구한다).
    여기서 미리 떼어 주지 않으면 `...:*:*` 가 되어 어떤 리소스에도 매칭되지 않고, IAM 은
    그걸 문법 오류로 보지 않으므로 **AccessDenied 로만 드러난다**.
  EOT
  value       = var.enabled ? trimsuffix(aws_cloudwatch_log_group.invocations[0].arn, ":*") : ""
}

output "log_region" {
  description = "admin-api 의 BEDROCK_INVOCATION_LOG_REGION — 배포 리전이 아니라 모델이 실행된 리전"
  value       = var.log_region
}

output "large_body_bucket" {
  description = "큰 본문(>100 KB) sidecar 버킷. admin-api 는 이 버킷을 읽지 않는다(권한도 없음)"
  value       = var.enabled ? aws_s3_bucket.large_bodies[0].id : ""
}

output "delivery_role_arn" {
  description = "Bedrock 이 맡는 배달 role. 로깅 설정 안에 박히는 값"
  value       = var.enabled ? aws_iam_role.delivery[0].arn : ""
}

output "kms_key_policy_statement" {
  description = <<-EOT
    var.large_body_kms_key_arn 를 쓸 때 **KMS 키 정책에 직접 추가해야 하는** 문장.
    이 모듈은 공유 KMS 키 정책을 수정하지 않는다(키 하나에 여러 서비스가 묶여 있고, 잘못
    쓰면 스스로 잠긴다). 이 grant 가 없으면 로깅 설정은 정상으로 보이는데 큰 본문만
    조용히 유실된다. SSE-S3 를 쓰는 경우엔 빈 문자열.
  EOT
  value = var.enabled && var.large_body_kms_key_arn != "" ? jsonencode({
    Sid       = "AllowBedrockLargeBodyEncryption"
    Effect    = "Allow"
    Principal = { Service = "bedrock.amazonaws.com" }
    Action    = "kms:GenerateDataKey"
    Resource  = "*"
    Condition = {
      StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.logs.account_id }
      ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock:${var.log_region}:${data.aws_caller_identity.logs.account_id}:*" }
    }
  }) : ""
}

output "admin_api_env" {
  description = <<-EOT
    helm values 의 `aws.bedrockInvocationLogging` 에 그대로 옮길 수 있는 묶음.
    미사용(enabled=false)이면 log_group 이 빈 문자열이고, admin-api 는 그때 감사
    엔드포인트를 404 가 아니라 **503(미설정)** 으로 응답한다 — UI 가 "기능 없음" 과
    "안 켬" 을 구분할 수 있어야 하기 때문이다.
  EOT
  value = {
    log_group = var.enabled ? aws_cloudwatch_log_group.invocations[0].name : ""
    region    = var.log_region
  }
}
