# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ─── partial backend config ───
# bucket / dynamodb_table 은 계정마다 다르므로 init 시 -backend-config 로 주입.
# deploy apply 가 deployment/gen/<env>/ecs/backend.hcl 을 만들어 자동 주입한다.
# 수동 사용 시:
#   terraform init -backend-config=<path>/backend.hcl
terraform {
  backend "s3" {
    key     = "ecs/terraform.tfstate"
    encrypt = true
    # region 은 -backend-config 로 주입 (리전 중립 — US 전용 가정 금지)
  }
}
