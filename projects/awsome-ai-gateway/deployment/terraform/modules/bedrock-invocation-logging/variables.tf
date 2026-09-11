# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "enabled" {
  description = <<-EOT
    이 모듈이 실제로 리소스를 만드는가. **기본 false** 다.

    기본값이 false 인 것은 스타일이 아니라 안전장치다: 아래 `put` 이 켜지는 순간
    그 리전에서 일어나는 **모든 계정 내 Bedrock 호출의 요청/응답 본문** 이 수집된다.
    모델별·주체별 스위치는 존재하지 않으므로(리전이 유일한 격리 수단) "일단 켜 두고
    나중에 좁힌다" 가 불가능하다. 그래서 켜는 것은 항상 명시적 결정이어야 한다.
  EOT
  type        = bool
  default     = false
}

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "log_region" {
  description = <<-EOT
    로깅을 켤 리전. **`providers = { aws = aws.<alias> }` 로 넘긴 provider 의 리전과
    같아야 한다** — 이 변수는 리소스 이름/ARN 문자열에만 쓰이고 API 호출 리전을 바꾸지
    않는다. 어긋나면 "만들어진 곳" 과 "정책이 가리키는 곳" 이 달라져 배달이 조용히 실패한다.
    아래 precondition 이 실제 provider 리전과 대조해 어긋남을 apply 전에 잡는다.

    us-east-2 = `us.openai.gpt-5.6-*` 와 `global.openai.gpt-5.6-*` 가 **둘 다** 착지하는
    유일한 리전(2026-09-03 get-inference-profile 실측).
  EOT
  type        = string
  default     = "us-east-2"

  validation {
    # 서울에 켜면 모든 앱의 Claude 프롬프트 본문이 수집된다. 이 모듈로는 불가능하게 한다 —
    # 정말 필요하면 provision_bedrock_invocation_logging.py --allow-seoul 로 의도를 남긴다.
    condition     = var.log_region != "ap-northeast-2"
    error_message = "ap-northeast-2 는 모든 앱의 Claude 호출이 도는 리전이라 본문 수집 대상이 될 수 없습니다. 예외가 필요하면 provision_bedrock_invocation_logging.py --allow-seoul 를 쓰세요."
  }
}

variable "log_group_name" {
  description = <<-EOT
    CloudWatch log group 이름. 기본값은 AWS 콘솔이 제안하는 이름과 같게 두어
    provision_bedrock_invocation_logging.py 와 **같은 리소스를 가리키도록** 맞춘 것이다.
    바꾸면 두 경로가 서로 다른 group 을 만들고, admin-api 는 그중 하나만 읽는다.
  EOT
  type        = string
  default     = "/aws/bedrock/modelinvocations"
}

variable "log_retention_days" {
  description = <<-EOT
    log group 보존일. 프롬프트 원문이 들어 있으므로 무기한 보존은 기본값이 될 수 없다.
    0 = 무기한(설정하지 않음) — 명시적으로 0 을 넣어야 그렇게 된다.
  EOT
  type        = number
  default     = 30
}

variable "large_body_retention_days" {
  description = <<-EOT
    sidecar S3 에 저장된 **큰 본문** 보존일. log group 보존과 별개다(다른 서비스라
    lifecycle 이 각각 필요하다). 여기서 만료시키지 않으면 CloudWatch 쪽이 30일 뒤
    사라져도 >100 KB 프롬프트만 영구 보존되는 비대칭이 생긴다.
  EOT
  type        = number
  default     = 30
}

variable "modalities" {
  description = <<-EOT
    수집할 데이터 종류. 기본은 text 만 — 우리가 감사해야 하는 것은 프롬프트/응답 텍스트다.
    image/video 를 켜면 sidecar S3 용량이 폭증하고, 우리 GPT-5.6 경로에는 그 modality 가
    없어 얻는 것도 없다.
  EOT
  type        = set(string)
  default     = ["text"]

  validation {
    condition     = length(setsubtract(var.modalities, ["text", "image", "embedding", "video"])) == 0
    error_message = "modalities 는 text, image, embedding, video 중에서만 고를 수 있습니다."
  }

  validation {
    condition     = length(var.modalities) > 0
    error_message = "modalities 가 비면 로깅을 켜 놓고 아무것도 기록하지 않는 상태가 됩니다 — enabled=false 를 쓰세요."
  }
}

variable "large_body_kms_key_arn" {
  description = <<-EOT
    sidecar 버킷 SSE-KMS 용 고객관리 키 ARN. 비우면 SSE-S3(AES256).

    ⚠️ 값을 넣으면 **키 정책에 bedrock.amazonaws.com 의 kms:GenerateDataKey 를 직접
    추가해야 한다**(이 모듈은 공유 KMS 키 정책을 건드리지 않는다 — 키 하나로 여러 서비스가
    묶여 있고 잘못 쓰면 스스로 잠긴다). 그 grant 가 없으면 로깅 설정은 정상으로 보이는데
    큰 본문만 조용히 유실된다. `kms_key_policy_statement` output 에 넣어야 할 문장을
    그대로 출력해 둔다.
  EOT
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
