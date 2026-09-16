# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }

    # final_snapshot_identifier 의 타임스탬프를 state 에 고정하는 데만 쓴다
    # (main.tf 의 time_static.final_snapshot 참조).
    # 양쪽 환경 lock 파일에 이미 hashicorp/time 0.14.0 (constraints ">= 0.9.0") 이
    # 들어 있어 ">= 0.9" 는 같은 문자열로 정규화된다 — lock 변경/재init 불필요.
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
  }
}
