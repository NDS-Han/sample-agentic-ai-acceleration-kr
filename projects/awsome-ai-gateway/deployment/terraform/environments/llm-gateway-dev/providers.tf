# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.32"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.15"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "llm-gateway-vanilla"
    }
  }
}

# GPT-5.6 표준 runtime plane 이 실행되는 리전(us-east-2) 전용 provider.
# 왜 별칭이 필요한가: Bedrock model-invocation logging 은 **리전 단위** 설정이고, 로그는
# 게이트웨이 배포 리전(ap-northeast-2)이 아니라 **모델이 실제로 실행된 리전** 에 쌓인다.
# `us.openai.gpt-5.6-*` 와 `global.openai.gpt-5.6-*` 가 둘 다 착지하는 유일한 리전이
# us-east-2 다(2026-09-03 get-inference-profile 실측).
# bedrock-invocation-logging 모듈은 이 별칭을 configuration_aliases 로 요구하므로,
# 실수로 기본 provider(서울)를 상속해 서울 계정 전체의 Claude 본문을 수집하는 사고가
# 문법 수준에서 막힌다.
provider "aws" {
  alias  = "bedrock_openai"
  region = var.bedrock_invocation_log_region
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "llm-gateway-vanilla"
    }
  }
}

# EKS 인증 토큰 — exec 방식 사용.
# data.aws_eks_cluster_auth 는 plan 시점에 토큰을 1회 fetch 해서 고정하는데
# EKS 토큰은 15분만 유효 → apply 가 길어지거나 중간에 다른 작업 후 재apply 시
# 만료된 토큰으로 호출해 `system:anonymous` 거부가 발생.
# exec 는 provider 가 API 호출할 때마다 `aws eks get-token` 을 실행하여 fresh 토큰 사용.
provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.aws_region]
  }
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.aws_region]
    }
  }
}
