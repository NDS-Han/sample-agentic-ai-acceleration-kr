# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "llm-gateway-vanilla"
      DeployPath  = "ecs"
    }
  }
}

# Cognito Managed Login 브랜딩(module.cognito) 전용 — managed_login=false 면
# 리소스가 없어 API 호출도 없다.
provider "awscc" {
  region = var.aws_region
}
