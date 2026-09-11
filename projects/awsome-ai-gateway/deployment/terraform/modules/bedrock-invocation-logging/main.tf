# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# Bedrock model **invocation logging** — 표준 runtime plane 본문 감사용
# ------------------------------------------------------------------------------
# 왜 필요한가: 우리 `usage.usage_logs` 는 토큰 수와 비용만 남기고 프롬프트/응답 **본문** 은
# 남기지 않는다. "이 사용자가 모델에 정확히 뭘 보냈나" 는 우리 데이터로 답할 수 없다.
# Bedrock 의 invocation logging 이 서비스 쪽에서 그걸 남기고,
# `usage_logs.bedrock_request_id`(= AWS `x-amzn-requestid`)가 조인 키다.
#
# ⚠️ **표준 runtime plane 만** 캡처된다. Mantle(`bedrock-mantle`) 트래픽은 무엇을 설정해도
#    레코드가 0 이다 — live 실측(gateway-proxy/tests/integration/test_invocation_logging_live.py):
#
#        plane    wire       stream   records
#        runtime  responses  no          1
#        runtime  responses  YES         1   ← 스트리밍도 기록된다
#        runtime  chat       YES         1
#        Mantle   responses  no          0   ← plane 음성대조군
#        Mantle   responses  YES         0
#
#    그래서 Mantle 행의 `bedrock_request_id IS NULL` 은 결함이 아니고, GPT-5.6 을 runtime
#    plane 으로도 지원하는 이유 자체가 이 감사 가능성이다.
#
# ⚠️ **리전 단위 + 그 리전 계정 전체** 스위치다. 모델별/주체별 스코프가 없으므로 리전 격리가
#    유일한 통제 수단이다. 그래서 (a) `var.enabled` 기본 false, (b) ap-northeast-2 는
#    variables.tf 의 validation 으로 아예 금지(서울 = 모든 앱 Claude 호출), (c) provider 를
#    호출자가 별칭으로 명시해서 넘기게 강제한다(versions.tf).
#
# ⚠️ **이 모듈과 `deployment/scripts/provision_bedrock_invocation_logging.py` 는 같은
#    싱글턴을 관리한다.** 리전당 로깅 설정은 하나뿐이라, 둘 다 쓰면 마지막에 실행한 쪽이
#    이긴다(terraform 은 다음 plan 에서 되돌리려 한다). 리전마다 **소유자를 하나만** 정할
#    것: IaC 로 관리할 환경은 이 모듈, 임시/수동 실험이나 --dry-run 조사는 스크립트.
#    리소스 이름(log group, role, bucket, 버킷 정책, 스트림 스코프)은 두 경로가 **바이트
#    단위로 같게** 맞춰져 있으므로, 스크립트로 만든 것을 terraform 으로 `import` 해서
#    소유권을 옮기는 것은 안전하다.
# ==============================================================================

data "aws_caller_identity" "logs" {
  provider = aws.logs
}

data "aws_region" "logs" {
  provider = aws.logs
}

locals {
  count = var.enabled ? 1 : 0

  account_id = data.aws_caller_identity.logs.account_id

  # 리전당 sidecar 버킷. S3 이름은 전역이지만 버킷은 리전 자원이고, 문서상 로그 버킷은
  # 로깅 설정과 **같은 계정·같은 리전** 이어야 한다 — 이름에 리전을 넣지 않으면 두 번째
  # 리전을 켤 수 없게 된다. 이름 규칙은 provision_bedrock_invocation_logging.py 와 동일.
  bucket_name = "${var.project}-${var.environment}-bedrock-invlogs-${local.account_id}-${var.log_region}"

  # 리전당 배달 role. 공유 role 이 아닌 이유: inline 정책이 그 리전 log-group ARN 을
  # 직접 가리킨다. 하나로 합치면 리전 하나를 내릴 때 정책을 재작성해 다른 리전의 배달이
  # 조용히 깨진다.
  role_name = "${var.project}-${var.environment}-bedrock-invlog-${var.log_region}"

  # Bedrock 은 모든 레코드를 이 **고정된 이름의 스트림 하나** 에 쓴다(서비스가 정한 이름).
  # 배달 role 의 PutLogEvents 를 정확히 이 스트림에만 허용한다 — `:*` 로 넓히면 role 이
  # 그룹 어디에나 쓸 수 있고, 반대로 틀리게 좁히면 설정은 저장되는데 레코드가 영원히
  # 안 온다(배달 실패는 우리에게 표면화되지 않는다).
  log_stream_name = "aws/bedrock/modelinvocations"

  # 큰 본문(>100 KB, 바이너리)이 떨어지는 prefix. lifecycle 필터와 같은 값이어야 한다.
  large_data_prefix = "large-data"

  tags = merge(var.tags, {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform:bedrock-invocation-logging"
  })
}

# ------------------------------------------------------------------------------
# 1. CloudWatch log group  (+ provider 리전 == var.log_region 대조)
# ------------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "invocations" {
  provider = aws.logs
  count    = local.count

  name = var.log_group_name
  # 프롬프트 원문이 들어가므로 보존 기간을 반드시 건다. 0 = 무기한(AWS 기본)인데,
  # 그건 명시적으로 0 을 넣은 경우에만 되게 한다.
  retention_in_days = var.log_retention_days
  tags              = local.tags

  lifecycle {
    # var.log_region 은 이름/ARN 문자열만 만들고 API 호출 리전을 바꾸지 않는다. 호출자가
    # `providers = { aws = aws.seoul }` 를 넘기고 log_region="us-east-2" 로 두면 리소스는
    # 서울에 생기는데 정책은 오하이오를 가리킨다 — 배달이 조용히 실패하는 조합이다.
    # 이 모듈에서 **가장 먼저 만들어지는 리소스** 에 걸어, 아무것도 생기기 전에 막는다.
    precondition {
      condition = data.aws_region.logs.name == var.log_region
      error_message = format(
        "provider 리전(%s)과 var.log_region(%s)이 다릅니다. providers = { aws = aws.<%s 별칭> } 을 넘기세요.",
        data.aws_region.logs.name, var.log_region, var.log_region
      )
    }
  }
}

# ------------------------------------------------------------------------------
# 2. 큰 본문 sidecar S3 버킷
# ------------------------------------------------------------------------------
resource "aws_s3_bucket" "large_bodies" {
  provider = aws.logs
  count    = local.count

  bucket = local.bucket_name
  tags   = local.tags
}

# ACL 비활성화는 하드닝이 아니라 **필수 조건** 이다: 문서에 "The bucket ACL must be
# disabled in order for the bucket policy to take effect" 라고 못박혀 있다.
resource "aws_s3_bucket_ownership_controls" "large_bodies" {
  provider = aws.logs
  count    = local.count

  bucket = aws_s3_bucket.large_bodies[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "large_bodies" {
  provider = aws.logs
  count    = local.count

  bucket             = aws_s3_bucket.large_bodies[0].id
  block_public_acls  = true
  ignore_public_acls = true
  # block_public_policy 는 **public** 정책만 막는다. 아래 서비스 주체(bedrock.amazonaws.com)
  # 정책은 public 이 아니라서 이 설정과 충돌하지 않는다.
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "large_bodies" {
  provider = aws.logs
  count    = local.count

  bucket = aws_s3_bucket.large_bodies[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "large_bodies" {
  provider = aws.logs
  count    = local.count

  bucket = aws_s3_bucket.large_bodies[0].id
  rule {
    apply_server_side_encryption_by_default {
      # 기본은 SSE-S3: 키 정책이 필요 없으니 "설정은 건강해 보이는데 큰 본문만 유실" 이라는
      # 상태에 빠질 길이 없다. KMS 를 쓰면 output 의 kms_key_policy_statement 를 키 정책에
      # 직접 추가해야 한다(var.large_body_kms_key_arn 설명 참조).
      sse_algorithm     = var.large_body_kms_key_arn != "" ? "aws:kms" : "AES256"
      kms_master_key_id = var.large_body_kms_key_arn != "" ? var.large_body_kms_key_arn : null
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "large_bodies" {
  provider = aws.logs
  count    = local.count

  bucket = aws_s3_bucket.large_bodies[0].id
  # 버킷 정책/소유권 설정과 경합하지 않도록 버전관리 뒤에 온다(noncurrent 규칙이
  # 버전관리를 전제한다).
  depends_on = [aws_s3_bucket_versioning.large_bodies]

  rule {
    id     = "expire-invocation-log-bodies"
    status = "Enabled"

    filter {
      prefix = "${local.large_data_prefix}/"
    }

    expiration {
      days = var.large_body_retention_days
    }

    # 버전관리가 켜져 있으므로 expiration 만으로는 delete marker 만 생기고 실제 본문은
    # noncurrent 버전으로 영구 잔존한다 — 그러면 보존기간 약속이 겉치레가 된다.
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {
      prefix = ""
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "large_bodies_bucket" {
  # enabled=false 면 아래 `aws_s3_bucket.large_bodies[0]` 참조가 인덱스 오류를 낸다 —
  # 정책 문서도 함께 꺼야 한다(정책 문서 자체는 AWS 호출이 아니라 순수 렌더링이지만,
  # 참조는 그래도 평가된다).
  count = local.count

  statement {
    sid    = "AmazonBedrockLogsWrite"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }

    actions = ["s3:PutObject"]

    # 키 경로가 아니라 버킷 전체를 대상으로 둔다. 문서가 정확한 레이아웃을 못박은 것은
    # `s3Config` 목적지(`{prefix}/AWSLogs/{acct}/BedrockModelInvocationLogs/*`)뿐이고
    # `largeDataDeliveryS3Config` 는 "데이터 prefix 아래" 라고만 한다. 그 경로를 잘못
    # 추측하면 큰 본문이 **에러 없이** 전부 유실된다. 이 버킷은 Bedrock 전용이므로 실제
    # 경계는 아래 두 condition 이다 — 다른 계정/다른 리전이 여기 쓰는 것을 막는 게 그것들이다.
    resources = ["${aws_s3_bucket.large_bodies[0].arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock:${var.log_region}:${local.account_id}:*"]
    }
  }
}

resource "aws_s3_bucket_policy" "large_bodies" {
  provider = aws.logs
  count    = local.count

  bucket = aws_s3_bucket.large_bodies[0].id
  policy = data.aws_iam_policy_document.large_bodies_bucket[0].json
  # ACL 이 켜진 상태에서는 정책이 효력이 없다 — 소유권 설정이 먼저 적용되어야 한다.
  depends_on = [
    aws_s3_bucket_ownership_controls.large_bodies,
    aws_s3_bucket_public_access_block.large_bodies,
  ]
}

# ------------------------------------------------------------------------------
# 3. 배달 IAM role
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "delivery_trust" {
  count = local.count

  statement {
    sid    = "AllowBedrockLogDelivery"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }

    actions = ["sts:AssumeRole"]

    # confused-deputy 가드(문서 권장). 없으면 임의 계정의 Bedrock 이 이 role 을 맡을 수 있다.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock:${var.log_region}:${local.account_id}:*"]
    }
  }
}

data "aws_iam_policy_document" "delivery" {
  count = local.count

  statement {
    sid    = "WriteInvocationLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    # 고정 스트림 하나로 좁힌다(local.log_stream_name 주석 참조).
    resources = [
      "arn:aws:logs:${var.log_region}:${local.account_id}:log-group:${var.log_group_name}:log-stream:${local.log_stream_name}",
    ]
  }

  statement {
    # 큰 본문 배달이 이 role 을 쓰는지 버킷 정책의 서비스 주체를 쓰는지 문서가 명시하지
    # 않는다. 한쪽만 주고 틀리면 **>100 KB 프롬프트** — 가장 감사 가치가 높은 것들 — 만
    # 조용히 사라진다. 그래서 두 경로 모두 허용한다(대상은 이 전용 버킷 하나뿐).
    sid       = "WriteLargeBodies"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.large_bodies[0].arn}/*"]
  }
}

resource "aws_iam_role" "delivery" {
  provider = aws.logs
  count    = local.count

  name               = local.role_name
  description        = "Bedrock model-invocation-log delivery (${var.log_region})"
  assume_role_policy = data.aws_iam_policy_document.delivery_trust[0].json
  tags               = local.tags
}

resource "aws_iam_role_policy" "delivery" {
  provider = aws.logs
  count    = local.count

  name   = "bedrock-invocation-log-delivery"
  role   = aws_iam_role.delivery[0].id
  policy = data.aws_iam_policy_document.delivery[0].json
}

# 갓 만든 role 은 서비스가 아직 맡을 수 없고(IAM 전파), Bedrock 은 설정 저장 시점에
# assumability 를 **인라인 검증** 한다. 그래서 create 직후 첫 Put 은 정당하게
# ValidationException 으로 실패한다. python 프로비저너는 12회 재시도로 흡수하지만
# terraform 에는 재시도 훅이 없어, 여기서 전파 시간을 명시적으로 기다린다.
resource "time_sleep" "iam_propagation" {
  count = local.count

  depends_on      = [aws_iam_role_policy.delivery]
  create_duration = "30s"
}

# ------------------------------------------------------------------------------
# 4. 로깅 설정 (리전당 싱글턴)
# ------------------------------------------------------------------------------
# ⚠️ 이 리소스가 그 리전의 **계정 전체** 스위치다. 리전당 하나만 존재할 수 있어서
#    terraform 과 python 프로비저너가 서로를 덮어쓴다(파일 상단 경고 참조).
resource "aws_bedrock_model_invocation_logging_configuration" "this" {
  provider = aws.logs
  count    = local.count

  depends_on = [
    aws_s3_bucket_policy.large_bodies,
    time_sleep.iam_propagation,
  ]

  logging_config {
    text_data_delivery_enabled      = contains(var.modalities, "text")
    image_data_delivery_enabled     = contains(var.modalities, "image")
    embedding_data_delivery_enabled = contains(var.modalities, "embedding")
    video_data_delivery_enabled     = contains(var.modalities, "video")

    cloudwatch_config {
      log_group_name = aws_cloudwatch_log_group.invocations[0].name
      role_arn       = aws_iam_role.delivery[0].arn

      large_data_delivery_s3_config {
        bucket_name = aws_s3_bucket.large_bodies[0].id
        key_prefix  = local.large_data_prefix
      }
    }
    # s3_config(전량을 S3 로 이중 배달)는 일부러 두지 않는다. 우리 감사 경로는 Logs
    # Insights 하나이고, 켜면 같은 프롬프트 본문 사본이 하나 더 생겨 보존/접근통제를
    # 두 곳에서 관리해야 한다.
  }
}
