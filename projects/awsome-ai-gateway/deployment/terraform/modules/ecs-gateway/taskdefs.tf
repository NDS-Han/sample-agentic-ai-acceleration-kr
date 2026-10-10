# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# Task Definitions — helm `_helpers.tpl` 의 env 계약을 ECS 형태로 이식
#
# 비밀번호가 URL 에 포함되는 값(DB_URL/REDIS_URL)은 helm 의 `$(DB_PASSWORD)` 치환과
# 같은 방식으로 컨테이너 command(sh -c) 안에서 조립한다. 이렇게 하면:
#   - task def JSON / terraform state 에 비밀번호 평문이 남지 않는다
#   - SM secret 값 변경 시 task 재시작만으로 새 값이 주입된다
#
# ⚠️ command 문자열 안의 $${...} 는 terraform 이 아니라 컨테이너 셸이 확장한다 —
#    $${ 는 template escape.
# ==============================================================================

locals {
  oidc_enabled = var.oidc_issuer_url != ""

  # 모든 python 서비스 공통 env — helm commonEnv + configmap 비민감 계약
  common_env = {
    APP_ENV             = var.environment
    LOG_FORMAT          = "json"
    LOG_LEVEL           = "INFO"
    AWS_REGION          = var.aws_region
    AWS_DEFAULT_REGION  = var.aws_region
    ALLOWED_STS_REGIONS = join(",", var.allowed_sts_regions)
    ALLOWED_IAM_ROLES   = join(",", var.allowed_iam_roles)
    REPORTING_TIMEZONE  = var.reporting_timezone
    REDIS_CLUSTER_MODE  = tostring(var.redis_cluster_mode)
    # ECS 경로엔 OTel collector 가 없다 — 명시 비활성화하지 않으면 SDK 가
    # localhost:4317 재시도를 계속한다 (관측스택 도입 시 endpoint 변수로 교체)
    OTEL_SDK_DISABLED = "true"
    # configmap DB pool 계약
    DB_POOL_SIZE    = tostring(var.db_pool.size)
    DB_MAX_OVERFLOW = tostring(var.db_pool.max_overflow)
    DB_POOL_TIMEOUT = tostring(var.db_pool.timeout)
    DB_POOL_RECYCLE = tostring(var.db_pool.recycle)
    DB_ECHO         = "false"
    DEFAULT_TEAM_ID = "00000000-0000-4000-a000-000000000003"
  }

  # gateway-proxy 전용 — configmap 의 Redis/RL 복원력 계약 (deepdive Q50,
  # 생략 시 느린 Redis 노드가 풀을 고갈시키는 과거 장애 패턴 재현)
  gw_resilience_env = {
    REDIS_POOL_SIZE             = tostring(var.redis_pool.size)
    REDIS_SOCKET_TIMEOUT        = tostring(var.redis_pool.socket_timeout)
    REDIS_CONNECT_TIMEOUT       = tostring(var.redis_pool.connect_timeout)
    REDIS_RETRIES               = tostring(var.redis_pool.retries)
    REDIS_HEALTH_CHECK_INTERVAL = tostring(var.redis_pool.health_check_interval)
    REDIS_READ_FROM_REPLICAS    = tostring(var.redis_pool.read_from_replicas)
    # helm 은 minReplicas 추종 — ECS 에서는 gateway desired_count 가 해당
    RL_FALLBACK_REPLICAS        = tostring(local.sizing["gateway-proxy"].desired_count)
    RL_BREAKER_ENABLED          = tostring(var.rate_limit.breaker_enabled)
    RL_BREAKER_FAIL_THRESHOLD   = tostring(var.rate_limit.breaker_fail_threshold)
    RL_BREAKER_RECOVERY_TIMEOUT = tostring(var.rate_limit.breaker_recovery_timeout)
    RL_FAIL_MODE                = var.rate_limit.fail_mode
  }

  # DB/Redis 연결을 위한 sh 래퍼 — secret env 는 task 시작 시점에 주입되므로
  # 이 문자열이 실행될 때는 이미 env 에 있다
  db_env_setup = join(" ", [
    "export DB_URL=\"postgresql+asyncpg://$${DB_USER}:$${DB_PASSWORD}@$${DB_HOST}:${var.db_port}/$${DB_NAME}?ssl=require\";",
    "export DATABASE_URL=\"$${DB_URL}\";",
    # ElastiCache AUTH token 은 user 부분 없이 password 만 (helm 의 rediss://:$(REDIS_PASSWORD)@ 과 동일)
    "export REDIS_URL=\"${var.redis_tls_enabled ? "rediss" : "redis"}://:$${REDIS_PASSWORD}@$${REDIS_HOST}:${var.redis_port}/0\";",
  ])

  # DB_URL 을 쓰는 서비스의 공통 environment
  conn_env = {
    DB_USER    = var.db_app_user
    DB_HOST    = var.db_host
    DB_NAME    = var.db_name
    REDIS_HOST = var.redis_host
  }

  email_env = {
    EMAIL_SENDER_TYPE    = var.email_sender_type
    EMAIL_SENDER_ADDRESS = var.email_sender_address
    EMAIL_SENDER_NAME    = var.email_sender_name
  }

  # ------------------------------------------------------------------------
  # 서비스별 environment
  # ------------------------------------------------------------------------
  svc_env = {
    gateway-proxy = merge(local.common_env, local.gw_resilience_env, {
      DB_USER                         = var.db_app_user
      DB_HOST                         = var.db_host
      DB_NAME                         = var.db_name
      REDIS_HOST                      = var.redis_host
      REDIS_TLS_ENABLED               = tostring(var.redis_tls_enabled)
      WORKERS                         = "2"
      COST_STREAM_KEY                 = "cost:stream"
      BUDGET_DEFAULT_POLICY           = "HARD_BLOCK"
      RATE_LIMIT_CONFIG_TTL_SEC       = "300"
      STREAM_IDLE_TIMEOUT             = tostring(var.stream_idle_timeout)
      STREAM_DISCONNECT_DRAIN_TIMEOUT = "30"
      DB_STATEMENT_CACHE_SIZE         = "100"
      JWT_ALGORITHM                   = "RS256"
      JWT_ISSUER                      = var.oidc_issuer_url
      JWT_AUDIENCE                    = var.oidc_audience != "" ? var.oidc_audience : var.oidc_client_id
      JWT_JWKS_URI                    = local.oidc_enabled ? "${var.oidc_issuer_url}/.well-known/jwks.json" : ""
      },
      var.body_log_firehose_name != "" ? {
        FIREHOSE_STREAM_NAME = var.body_log_firehose_name
        BODY_LOG_S3_BUCKET   = local.body_log_bucket_name
      } : {},
      var.agentcore_gateway_url != "" ? {
        WEB_SEARCH_ENABLED    = "true"
        AGENTCORE_GATEWAY_URL = var.agentcore_gateway_url
      } : {},
    )

    # admin-api — helm oidcEnv 전체 계약 (어긋나면 관리자 잠김/그룹 매핑 실패)
    admin-api = merge(local.common_env, local.conn_env, local.email_env, {
      DEV_LOGIN_ENABLED            = tostring(var.dev_login_enabled && !local.oidc_enabled)
      OIDC_ISSUER_URL              = var.oidc_issuer_url
      OIDC_AUDIENCE                = var.oidc_audience != "" ? var.oidc_audience : var.oidc_client_id
      OIDC_PROVIDER_NAME           = var.oidc_provider_name
      OIDC_JWKS_CACHE_TTL_SECONDS  = "3600"
      OIDC_USER_ID_CLAIM           = "sub"
      OIDC_EMAIL_CLAIM             = "email"
      OIDC_NAME_CLAIM              = "name"
      OIDC_GROUPS_CLAIM            = var.oidc_provider_name == "oidc:cognito" ? "cognito:groups" : "groups"
      OIDC_GROUP_PREFIX            = "Claude_"
      OIDC_REJECT_UNMATCHED_GROUPS = "true"
      OIDC_REQUIRED_GROUP          = var.oidc_required_group
      OIDC_VK_TTL_HOURS            = "1"
      ADMIN_GROUPS                 = join(",", var.admin_groups)
      ADMIN_EMAILS                 = join(",", var.admin_emails)
      DEFAULT_DEPT_ID              = "00000000-0000-4000-a000-000000000002"
      SYSTEM_USER_ID               = "00000000-0000-4000-a000-000000000010"
      COGNITO_USER_POOL_ID         = var.cognito_user_pool_id
      COGNITO_REGION               = var.aws_region
      DB_STATEMENT_CACHE_SIZE      = "100"
      CLI_DIST_DIR                 = "/app/cli-dist"
      },
      var.email_sender_type == "ses" ? { AWS_SES_REGION = var.aws_ses_region != "" ? var.aws_ses_region : var.aws_region } : {},
      var.email_sender_type == "smtp" ? { SMTP_HOST = var.smtp_host, SMTP_PORT = tostring(var.smtp_port), SMTP_USERNAME = var.smtp_username } : {},
    )

    admin-ui = merge(local.common_env, {
      ADMIN_API_URL           = "http://admin-api.${aws_service_discovery_private_dns_namespace.internal.name}:8080"
      NODE_ENV                = "production"
      NEXT_TELEMETRY_DISABLED = "1"
      DEV_LOGIN_ENABLED       = tostring(var.dev_login_enabled && !local.oidc_enabled)
      CLI_DIST_DIR            = "/app/cli-dist"
      # /cli 페이지의 사용자 측 주소 + 쿠키 정책 — helm admin-ui 와 같은 계약
      SECURE_COOKIES        = tostring(local.https_ready)
      GATEWAY_INGRESS_URL   = local.gateway_external_url
      ADMIN_API_INGRESS_URL = local.admin_api_external_url
      OIDC_ISSUER_URL       = var.oidc_issuer_url
      OIDC_AUDIENCE         = var.oidc_audience != "" ? var.oidc_audience : var.oidc_client_id
      # adminUiIdpEnv — admin-api 와 같은 소스여야 한다. 어긋나면 API 는 되는데
      # 화면 전체가 /403 (helm 주석의 명시 경고)
      OIDC_GROUPS_CLAIM = var.oidc_provider_name == "oidc:cognito" ? "cognito:groups" : "groups"
      ADMIN_GROUPS      = join(",", var.admin_groups)
      },
      local.admin_ui_external_url != "" ? { NEXTAUTH_URL = local.admin_ui_external_url } : {},
      local.oidc_enabled ? {
        OIDC_CLIENT_ID     = var.oidc_client_id
        OIDC_AUTHORIZE_URL = var.oidc_authorize_url
        OIDC_TOKEN_URL     = var.oidc_token_url
      } : {},
    )

    scheduler = merge(local.common_env, local.conn_env, {})

    cost-recorder-worker = merge(local.common_env, local.conn_env, {
      COST_STREAM_KEY         = "cost:stream"
      COST_STREAM_GROUP       = "cost-recorder-workers"
      COST_STREAM_CONSUMER    = "worker-1" # desired_count=1 전제 — 늘리면 중복 소비 주의
      BATCH_MAX_SIZE          = "100"
      BATCH_MAX_INTERVAL_SEC  = "5"
      DAILY_USAGE_AGG_CRON    = "10 0 * * *"
      DB_STATEMENT_CACHE_SIZE = "100"
    })

    notification-worker = merge(local.common_env, local.conn_env, local.email_env, {
      DB_STATEMENT_CACHE_SIZE = "100"
      REDIS_TLS_ENABLED       = tostring(var.redis_tls_enabled)
      },
      var.email_sender_type == "ses" ? { AWS_SES_REGION = var.aws_ses_region != "" ? var.aws_ses_region : var.aws_region } : {},
      var.email_sender_type == "smtp" ? {
        SMTP_HOST     = var.smtp_host
        SMTP_PORT     = tostring(var.smtp_port)
        SMTP_USERNAME = var.smtp_username
        SMTP_STARTTLS = "true"
      } : {},
    )

    migration = {
      # run_migration.sh 의 cloud 모드 계약 — init SQL + app user 생성 + alembic head.
      # password 는 URL 이 아니라 DB_MASTER_PASSWORD env/secret 으로 (PGPASSWORD 경로)
      DB_MASTER_URL  = "postgresql://${var.db_master_username}@${var.db_host}:${var.db_port}/${var.db_name}?sslmode=require"
      DB_MASTER_USER = var.db_master_username
      APP_DB_USER    = var.db_app_user
    }
  }

  # ------------------------------------------------------------------------
  # 서비스별 secrets (valueFrom → SM ARN:JSON-key::)
  # ------------------------------------------------------------------------
  db_secrets = [
    { name = "DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.db_app.arn}:password::" },
    { name = "REDIS_PASSWORD", valueFrom = var.redis_auth_token_secret_arn },
  ]

  svc_secrets = {
    gateway-proxy = concat(local.db_secrets, [
      { name = "VIRTUAL_KEY_ENCRYPTION_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:virtual_key_encryption_key::" },
    ])

    admin-api = concat(local.db_secrets, [
      { name = "VIRTUAL_KEY_ENCRYPTION_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:virtual_key_encryption_key::" },
      { name = "INTERNAL_API_TOKEN", valueFrom = "${aws_secretsmanager_secret.app.arn}:internal_api_token::" },
    ])

    admin-ui = concat([
      { name = "NEXTAUTH_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:nextauth_secret::" },
      ], local.oidc_enabled ? [
      { name = "OIDC_CLIENT_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:oidc_client_secret::" },
    ] : [])

    scheduler = local.db_secrets

    cost-recorder-worker = local.db_secrets

    notification-worker = concat(local.db_secrets,
      var.email_sender_type == "smtp" && var.smtp_password != "" ? [
        { name = "SMTP_PASSWORD", valueFrom = "${aws_secretsmanager_secret.app.arn}:smtp_password::" },
    ] : [])

    migration = [
      { name = "DB_MASTER_PASSWORD", valueFrom = "${var.db_master_secret_arn}:password::" },
      { name = "APP_DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.db_app.arn}:password::" },
    ]
  }

  # command 래퍼 — db 를 쓰는 서비스는 URL 조립 후 exec, 나머지는 그대로 exec
  svc_command = {
    for name, svc in local.services :
    name => ["sh", "-c", svc.db ? "${local.db_env_setup} ${svc.command}" : svc.command]
  }

  body_log_bucket_name = var.body_log_bucket_arn != "" ? element(split(":::", var.body_log_bucket_arn), 1) : ""
}

# ------------------------------------------------------------------------------
# Migration — helm 의 pre-install/pre-upgrade hook 에 해당.
#
# run_migration_task=true 면 image_tag 가 바뀔 때마다 run-task 로 실행하고,
# 서비스들은 그 완료(alembic head + app user 생성)를 기다린 뒤 뜬다.
# EKS 의 helm hook 과 같은 계약 — task 실패 시 apply 가 여기서 멈춰
# 헬스하지 않은 서비스 롤아웃을 막는다 (circuit breaker 보다 앞선 게이트).
# ⚠️ local-exec 은 apply 하는 머신의 aws CLI/자격증명을 쓴다 — deploy apply
#   가 그 전제를 이미 요구한다.
# ------------------------------------------------------------------------------
resource "terraform_data" "migration" {
  count = var.run_migration_task ? 1 : 0

  # 이미지 태그가 바뀌면 새 마이그레이션이 있을 수 있으므로 재실행
  triggers_replace = [var.image_tag]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      TASK_ARN=$(aws ecs run-task \
        --cluster ${aws_ecs_cluster.this.name} \
        --task-definition ${aws_ecs_task_definition.svc["migration"].arn} \
        --launch-type FARGATE \
        --network-configuration '{"awsvpcConfiguration":{"subnets":${jsonencode(var.private_subnet_ids)},"securityGroups":["${aws_security_group.tasks.id}"],"assignPublicIp":"DISABLED"}}' \
        --region ${data.aws_region.current.name} \
        --query 'tasks[0].taskArn' --output text)
      echo "migration task: $TASK_ARN"
      aws ecs wait tasks-stopped --cluster ${aws_ecs_cluster.this.name} \
        --tasks "$TASK_ARN" --region ${data.aws_region.current.name}
      EXIT_CODE=$(aws ecs describe-tasks --cluster ${aws_ecs_cluster.this.name} \
        --tasks "$TASK_ARN" --region ${data.aws_region.current.name} \
        --query 'tasks[0].containers[0].exitCode' --output text)
      echo "migration exitCode: $EXIT_CODE"
      [ "$EXIT_CODE" = "0" ] || {
        echo "migration 실패 — 로그: aws logs tail /ecs/${local.name_prefix}/migration --region ${data.aws_region.current.name}"
        exit 1
      }
    EOT
  }

  depends_on = [aws_ecs_task_definition.svc]
}

resource "aws_ecs_task_definition" "svc" {
  for_each = local.services

  family                   = "${local.name_prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.sizing[each.key].cpu
  memory                   = local.sizing[each.key].memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = local.task_roles[each.key]

  container_definitions = jsonencode([{
    name      = each.key
    image     = "${local.registry}/${each.value.image}:${var.image_tag}"
    essential = true

    # SIGTERM 후 스트림 드레인 시간 — gateway-proxy 의 SSE 가 길다
    stopTimeout = each.key == "gateway-proxy" ? 60 : 30

    command = local.svc_command[each.key]

    portMappings = each.value.port > 0 ? [{
      containerPort = each.value.port
      protocol      = "tcp"
    }] : []

    environment = [
      for k, v in local.svc_env[each.key] : { name = k, value = tostring(v) }
      if v != "" && v != null
    ]

    secrets = local.svc_secrets[each.key]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.svc[each.key].name
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = each.key
      }
    }
  }])

  tags = var.tags
}
