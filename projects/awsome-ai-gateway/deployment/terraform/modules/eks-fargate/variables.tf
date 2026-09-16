# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "cluster_version" {
  # 기본값을 일부러 두지 않는다(=필수). 환경 루트가 유일한 진실이어야 하기 때문이다 —
  # 기본값이 있으면 호출부가 값을 빼먹어도 plan 이 조용히 통과하고, 실제로 이 모듈은
  # module 1.29 / env 1.30 / 라이브 1.31 의 3중 불일치 상태로 방치돼 있었다.
  # 호출부는 2곳뿐이다(environments/llm-gateway-{dev,prod}/main.tf).
  # nullable = false: 기본값이 없어도 명시적 `cluster_version = null` 은 타입 검사를
  # 통과해 downstream 으로 흘러간다. 이 키워드가 있으면 "required variable may not be
  # set to null" 로 plan 단계에서 크게 실패한다(조용한 null 전파 차단).
  description = "EKS Kubernetes 버전 (환경 루트에서 필수 전달)"
  type        = string
  nullable    = false
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  description = "Fargate Pod 배치용 private subnet IDs (AZ 3개 이상 권장)"
  type        = list(string)
}

variable "public_access_cidrs" {
  description = "dev에서 kubectl 접근 허용 CIDR"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "application_namespace" {
  description = "LLM Gateway가 설치될 네임스페이스"
  type        = string
  default     = "llm-gateway"
}

variable "addon_versions" {
  # 기본값 없음(=필수). main.tf 가 이 값을 그대로 cluster_addons 의 addon_version 으로
  # 넘기고(:82 coredns / :123 kube_proxy / :128 vpc_cni), upstream 모듈은
  #   addon_version = coalesce(try(each.value.addon_version, null),
  #                            data.aws_eks_addon_version.this[each.key].version)
  # 로 해석한다(terraform-aws-modules/eks/aws 20.x main.tf:746). 즉 여기에 값이 **있으면**
  # coalesce 의 첫 인자가 이겨서 자동 조회가 죽고, 애드온은 클러스터 버전을 절대
  # 따라오지 않는다 → minor 홉마다 이 값도 함께 올려야 한다.
  # (자동 추종을 원하면 핀을 지우는 대신 cluster_addons 에 most_recent = true 를 주는 길도
  #  있으나, 리뷰 없이 버전이 움직이므로 prod 에는 쓰지 않는다.)
  #
  # 옛 기본값이 각 k8s 버전에서 아직 제공되는지 실측(aws eks describe-addon-versions,
  # ap-northeast-2, 2026-09-04). ✗ = 그 버전에선 목록에 없어 apply 가
  # InvalidParameterException 으로 막힌다:
  #   coredns    v1.11.3-eksbuild.1 : 1.32 ✓ / 1.33 ✓ / 1.34 ✓ / 1.35 ✗
  #   kube-proxy v1.29.7-eksbuild.2 : 1.32 ✓ / 1.33 ✗              ← 가장 먼저 막히는 핀
  #   vpc-cni    v1.18.3-eksbuild.1 : 1.32 ✓ / 1.33 ✓ / 1.34 ✓ / 1.35 ✗
  # Fargate 전용 클러스터에서는 kube-proxy·vpc-cni DaemonSet 이 스케줄되지 않아 실 트래픽
  # 영향은 없지만, 애드온 **리소스 자체**는 버전 검증을 받으므로 apply 는 그대로 막힌다.
  description = "EKS add-on 버전 (환경 루트에서 필수 전달. AWS 호환성 표: https://docs.aws.amazon.com/eks/latest/userguide/managing-add-ons.html)"
  # nullable = false: 기본값이 없더라도 명시적 `addon_versions = null` 은 통과해서
  # main.tf 의 `var.addon_versions.coredns` 가 "Attempt to get attribute from null
  # value" 로 죽는다(재현 확인). 이 키워드로 plan 단계에서 원인이 분명한 에러를 낸다.
  type = object({
    coredns    = string
    kube_proxy = string
    vpc_cni    = string
  })
  nullable = false
}

variable "access_entries" {
  description = "EKS Access Entries — 관리자/CI 계정 → RBAC role 매핑"
  type        = any
  default     = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
