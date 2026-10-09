# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# IAM — execution role (시크릿 주입·ECR pull·로그) + 서비스별 task role
#
# task role 정책은 modules/irsa 의 두 정책을 그대로 이식한 것이다 — 같은 권한을
# EKS ServiceAccount 가 아니라 ecs-tasks.amazonaws.com trust 로 받는다.
# IRSA 의 external_secrets role 은 필요 없다 (ECS 가 execution role 로 직접 읽음).
# ==============================================================================

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ------------------------------------------------------------------------------
# Task Execution Role — ECS agent 가 이미지 pull + SM secret 읽기 + 로그 쓰기용
# ------------------------------------------------------------------------------
resource "aws_iam_role" "execution" {
  name = "${local.name_prefix}-ecs-execution"

  assume_role_policy = data.aws_iam_policy_document.task_assume.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid    = "SecretsManagerRead"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [
      # 모듈이 만든 app/db 시크릿
      aws_secretsmanager_secret.app.arn,
      aws_secretsmanager_secret.db_app.arn,
      # 외부 모듈 소유 시크릿 — ElastiCache AUTH token, Aurora managed master (rds!cluster-*)
      var.redis_auth_token_secret_arn,
      var.db_master_secret_arn,
      "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:rds!cluster-*",
    ]
  }
}

resource "aws_iam_policy" "execution_secrets" {
  name   = "${local.name_prefix}-ecs-execution-secrets"
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role_policy_attachment" "execution_secrets" {
  role       = aws_iam_role.execution.name
  policy_arn = aws_iam_policy.execution_secrets.arn
}

# ------------------------------------------------------------------------------
# gateway-proxy task role — bedrock 정책 (modules/irsa/main.tf 의 동일 정책 이식)
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "gateway_proxy" {
  statement {
    sid    = "BedrockInvoke"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:CountTokens",
    ]
    resources = var.bedrock_allowed_model_arns
  }

  statement {
    sid    = "BedrockListModels"
    effect = "Allow"
    actions = [
      "bedrock:ListFoundationModels",
      "bedrock:GetFoundationModel",
      "bedrock:ListInferenceProfiles",
      "bedrock:GetInferenceProfile",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "InAccountMantleInference"
    effect = "Allow"
    actions = [
      "bedrock-mantle:CreateInference",
      "bedrock-mantle:GetInference",
    ]
    resources = [
      for r in var.mantle_regions :
      "arn:aws:bedrock-mantle:${r}:${data.aws_caller_identity.current.account_id}:*"
    ]
  }

  statement {
    sid       = "InAccountMantleBearer"
    effect    = "Allow"
    actions   = ["bedrock-mantle:CallWithBearerToken"]
    resources = ["*"]
  }

  # AgentCore Web Search — managed connector 는 us-east-1 구조적 제약 (문서화된 예외)
  statement {
    sid     = "AgentCoreInvokeGateway"
    effect  = "Allow"
    actions = ["bedrock-agentcore:InvokeGateway"]
    resources = [
      "arn:aws:bedrock-agentcore:us-east-1:${data.aws_caller_identity.current.account_id}:gateway/*"
    ]
  }

  dynamic "statement" {
    for_each = var.cowork_role_arn != "" ? [1] : []
    content {
      sid       = "AssumeCoworkMantle"
      effect    = "Allow"
      actions   = ["sts:AssumeRole"]
      resources = [var.cowork_role_arn]
    }
  }

  dynamic "statement" {
    for_each = var.claude_code_role_arn != "" ? [1] : []
    content {
      sid       = "AssumeClaudeCodeBedrock"
      effect    = "Allow"
      actions   = ["sts:AssumeRole"]
      resources = [var.claude_code_role_arn]
    }
  }

  # body_logging — 쓰기 전용 (irsa 모듈과 동일한 이유: 읽기 권한 = 누적 프롬프트 유출 경로)
  dynamic "statement" {
    for_each = var.body_log_firehose_arn != "" ? [1] : []
    content {
      sid    = "BodyLogFirehoseWrite"
      effect = "Allow"
      actions = [
        "firehose:PutRecord",
        "firehose:PutRecordBatch",
      ]
      resources = [var.body_log_firehose_arn]
    }
  }

  dynamic "statement" {
    for_each = var.body_log_bucket_arn != "" ? [1] : []
    content {
      sid       = "BodyLogS3Fallback"
      effect    = "Allow"
      actions   = ["s3:PutObject"]
      resources = ["${var.body_log_bucket_arn}/*"]
    }
  }
}

resource "aws_iam_policy" "gateway_proxy" {
  name   = "${local.name_prefix}-gateway-proxy"
  policy = data.aws_iam_policy_document.gateway_proxy.json
}

resource "aws_iam_role" "gateway_proxy" {
  name               = "${local.name_prefix}-gateway-proxy"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "gateway_proxy" {
  role       = aws_iam_role.gateway_proxy.name
  policy_arn = aws_iam_policy.gateway_proxy.arn
}

# ------------------------------------------------------------------------------
# admin-api task role — STS + Cognito + Pricing (irsa admin_api 정책 이식)
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "admin_api" {
  statement {
    sid       = "StsGetCallerIdentity"
    effect    = "Allow"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }

  dynamic "statement" {
    for_each = var.cognito_user_pool_arn != "" ? [1] : []
    content {
      sid    = "CognitoSync"
      effect = "Allow"
      actions = [
        "cognito-idp:ListGroups",
        "cognito-idp:ListUsersInGroup",
        "cognito-idp:ListUsers",
        "cognito-idp:AdminListGroupsForUser",
        "cognito-idp:AdminGetUser",
      ]
      resources = [var.cognito_user_pool_arn]
    }
  }

  statement {
    sid    = "PriceListRead"
    effect = "Allow"
    actions = [
      "pricing:GetProducts",
      "pricing:DescribeServices",
      "pricing:GetAttributeValues",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "admin_api" {
  name   = "${local.name_prefix}-admin-api"
  policy = data.aws_iam_policy_document.admin_api.json
}

resource "aws_iam_role" "admin_api" {
  name               = "${local.name_prefix}-admin-api"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "admin_api" {
  role       = aws_iam_role.admin_api.name
  policy_arn = aws_iam_policy.admin_api.arn
}

# ------------------------------------------------------------------------------
# notification-worker task role — SES provider 일 때만 send 권한
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "notification_worker" {
  dynamic "statement" {
    for_each = var.email_sender_type == "ses" ? [1] : []
    content {
      sid    = "SesSend"
      effect = "Allow"
      actions = [
        "ses:SendEmail",
        "ses:SendRawEmail",
      ]
      resources = ["*"] # SES 는 발신 identity ARN 지정도 가능하지만 verified domain 임의성을 위해 *
    }
  }
}

resource "aws_iam_policy" "notification_worker" {
  name   = "${local.name_prefix}-notification-worker"
  policy = data.aws_iam_policy_document.notification_worker.json
}

resource "aws_iam_role" "notification_worker" {
  name               = "${local.name_prefix}-notification-worker"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "notification_worker" {
  role       = aws_iam_role.notification_worker.name
  policy_arn = aws_iam_policy.notification_worker.arn
}

# ------------------------------------------------------------------------------
# 나머지 서비스의 기본 task role — 빈 정책 (DB/Redis 만 쓰는 서비스)
# ------------------------------------------------------------------------------
resource "aws_iam_role" "baseline" {
  name               = "${local.name_prefix}-baseline-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
  tags               = var.tags
}

locals {
  task_roles = {
    gateway-proxy        = aws_iam_role.gateway_proxy.arn
    admin-api            = aws_iam_role.admin_api.arn
    admin-ui             = aws_iam_role.baseline.arn
    scheduler            = aws_iam_role.baseline.arn # 순수 DB 작업 — EKS 에서도 IRSA 없음
    cost-recorder-worker = aws_iam_role.baseline.arn
    notification-worker  = aws_iam_role.notification_worker.arn
    migration            = aws_iam_role.baseline.arn
  }
}
