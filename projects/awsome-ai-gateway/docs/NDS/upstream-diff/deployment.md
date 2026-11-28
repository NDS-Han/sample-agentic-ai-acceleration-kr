# 배포와 환경 수명주기

**한국어** · [English](deployment.en.md) · [비교 목차](README.md)

## 선언에서 배포까지

NDS는 기존 Terraform·Helm·배포 스크립트 위에 `gateway.yaml`과 `./deploy`를 추가했습니다. `init/configure`로 입력을 만들고 `validate/render`로 산출물을 검증합니다. 신규 환경은 `bootstrap`, 변경은 `apply --plan`과 `apply`, 상태 확인은 `doctor`를 사용합니다. `doctor --capture`는 대상의 현재 상태로 설정 초안을 만들며, 운영자가 미확인·보존 항목과 source of truth를 검토합니다. `client-values`의 결과는 직원 설정에 전달합니다. [설정 스키마](../gateway-yaml.md)

| 대상 | 배포 산출물 | 인수·운영에서 확인할 점 |
|---|---|---|
| Compose | 단일 호스트 Compose, env, Caddy | 백업은 호스트 외부에 보관. 볼륨 삭제 옵션 확인 |
| ECS | Terraform tfvars 및 Fargate 서비스 | 서비스·태스크별 배포, 계정과 상태, IAM, 비밀의 JSON 필드 확인 |
| EKS | Helm values 및 관리형 Terraform overlay | 정확한 context와 릴리스 확인. 공통·서비스별 이미지 핀을 기록. 제어판/애드온 업그레이드는 별도 계획 |

EKS와 ECS 용량 밴드는 사용자 수를 기준으로 초기 pod/task 범위를 정합니다. DB와 Redis의 실측 및 `deploy.sizing`을 함께 사용합니다. EKS `upgrade eks`는 제어판을 한 번에 한 minor씩 계획하고 애드온과 워크로드 검증을 거칩니다. 앱 롤백은 DB 호환성이 확인된 전 태그를 다시 적용합니다. [업데이트](../update.md) · [EKS 업그레이드](../upgrade-eks-cluster.md)

## 권한, TLS와 선택 기능

- Route 53/ACM 경로는 인증서 검증, 호스트와 인입 구성, Cognito 콜백을 연결합니다. smoke 테스트의 private DNS 판정은 `9b59c781`에서 실제 VPC CIDR 기준으로 바뀌었습니다.
- ADFS의 메타데이터·매핑·provider details를 선언에 포함합니다. `6d382cd4`의 변경 뒤에는 수동 값과 선언 차이를 검토하고 capture·plan으로 소유권을 확인합니다.
- optional 기능의 범위와 준비 조건을 별개로 확인합니다. 웹 검색은 AgentCore Gateway와 제한 설정, BI Insight는 AgentCore Runtime과 DB 읽기 자격, 외부 가격은 Lambda, 로그는 Firehose/S3, 알림은 백엔드별 Secret/채널이 필요할 수 있습니다.
- 인프라 상태와 앱 상태는 다른 변경 단위입니다. 관리 overlay가 담당한 모듈, 운영자가 유지하는 VPC·EKS·Aurora 등의 상태와 변경 경로, teardown에서 환경에 미치는 범위를 확인합니다.

[배포 CLI 소스](../deploy/cli.py) · [Terraform 모듈](../../../deployment/terraform/modules) · [ADFS](../adfs-sso.md) · [운영 인수인계](../op-guide/README.ko.md)
