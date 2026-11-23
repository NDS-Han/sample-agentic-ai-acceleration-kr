# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # user_pool_tier(5.8x)·managed_login_version(5.95) 필요
      version = ">= 5.95"
    }
    # Managed Login 브랜딩 스타일 — hashicorp/aws 는 6.12 부터라(env 는 5.x 고정)
    # 이 리소스 하나만 awscc 로 둔다.
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 1.25.0, < 2.0.0"
    }
  }
}
