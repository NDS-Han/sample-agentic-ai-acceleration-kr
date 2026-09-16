# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.70"

      # ⚠️ 이 모듈은 **호출자가 넘겨주는 별칭 provider** 로만 동작한다.
      #
      # 이유: invocation logging 은 리전 단위 설정이고, 우리가 켜려는 리전(us-east-2 —
      # GPT-5.6 CRIS 프로파일이 실행되는 곳)은 환경 루트의 기본 provider 리전
      # (ap-northeast-2)과 **다르다**. 기본 provider 를 상속하면 서울에 켜지는데,
      # 서울은 모든 앱의 Claude 호출이 도는 리전이므로 그 **본문 전체** 를 계정 단위로
      # 수집하게 된다 — 되돌릴 수 없는 데이터 수집 사고다.
      #
      # configuration_aliases 를 선언하면 호출자가 providers = { aws = aws.<alias> } 를
      # 빼먹으면 terraform 이 에러를 낸다. 즉 "리전을 잘못 상속" 이라는 실패 모드를
      # 문서가 아니라 타입으로 막는다.
      configuration_aliases = [aws.logs]
    }

    # 갓 만든 배달 role 의 IAM 전파를 기다리는 데만 쓴다(main.tf 의 time_sleep 참조).
    # Bedrock 이 설정 저장 시점에 role assumability 를 인라인 검증하는데 terraform 에는
    # 재시도 훅이 없어, 대기를 리소스로 명시해야 첫 apply 가 통과한다.
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
  }
}
