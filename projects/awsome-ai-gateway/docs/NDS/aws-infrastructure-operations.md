# AWS 인프라 운영 가이드

**한국어** · [English](aws-infrastructure-operations.en.md) · [운영·배포 인수인계](op-guide/README.ko.md) · [NDS 문서](README.md)

NDS Gateway의 AWS 인프라를 인수하는 운영자가 콘솔에서 상태를 확인하고, 장애를 분류하고, 변경과 복구를 검증하는 가이드입니다. 점검은 환경별로 수행하고, 해당 환경에 배포한 리소스에 적용합니다. 게이트웨이의 사용자·모델·정책은 [관리자 상세 가이드](guides/admin-guide.ko.md)에서 관리합니다.

## 1. 환경 확인과 인수 첫날

1. 승인된 운영 역할로 접속합니다. 콘솔의 계정과 리전을 환경 인벤토리 및 배포 설정과 대조합니다. Bedrock 교차 계정 호출이나 별도 DNS·청구 계정이 있으면 각각 확인합니다.
2. **CloudWatch → Alarms**, **SNS → Subscriptions**에서 필수 알람이 현재 리소스를 감시하는지 확인하고, 테스트 이벤트가 당직자에게 도착하는지 검증합니다.
3. **RDS → Automated backups**에서 최신 복구 가능 시점과 보존 기간을 확인합니다. 복구 리허설 결과, 활성 키 복구 담당자, 직전 정상 릴리스의 ECR 이미지와 Terraform 상태 접근을 확인합니다.
4. 관리자와 일반 직원의 로그인, 대표 모델 호출, 앱 로그와 사용량 반영을 검증합니다. 교차 계정 모델은 실제 호출 역할과 모델을 함께 기록합니다.
5. 확인마다 시각과 타임존, 담당자, 결과, 증거 위치를 남깁니다. 실패한 항목은 영향을 받는 환경과 후속 담당자를 지정합니다.

환경별로 아래 양식을 복제해 승인된 인계 저장소에서 작성합니다. 시크릿은 보관 위치와 접근 담당자만 기록합니다.

| 인계 항목 | 기입할 값 |
|---|---|
| 환경과 권한 | `<environment>` · `<aws-account-id>` · `<aws-region>` · `<operator-role>` |
| 컴퓨팅과 네트워크 | `<eks-or-ecs-cluster>` · `<namespace-or-ecs-service>` · `<vpc-and-subnets>` · `<gateway-and-admin-hosts>` |
| 백업과 복구 | `<aurora-cluster-and-proxy-if-used>` · `<backup-retention>` · `<restore-test-record>` · `<secret-store-reference>` |
| 배포와 소유권 | `<deployment-config-reference>` · `<terraform-backend-reference>` · `<last-known-good-image-digests>` · `<infrastructure-owner>` |
| 알림과 에스컬레이션 | `<alarm-and-log-links>` · `<on-call-channel>` · `<aws-support-contact>` · `<last-verification-record>` |
| 모델과 비용 | `<bedrock-invocation-account-and-region>` · `<quota-owner>` · `<billing-owner>` |

## 2. 서비스별 점검

### CloudWatch, SNS, AWS Health

- **CloudWatch → Alarms**에서 최근 상태와 이력을 보고, 각 알람의 차원과 SNS 액션을 실제 리소스와 대조합니다. `INSUFFICIENT_DATA`이면 트래픽, 지표 수집, 알람 차원을 확인합니다. **SNS → Subscriptions**에서 구독 승인과 당직 교대 후 수신 대상을 확인합니다.
- **CloudWatch → Log groups**에서 마지막 이벤트, 보존 기간, 운영자 접근을 확인합니다. EKS Fargate 앱 로그는 로그 전달이 구성된 환경에서 수집을 확인합니다. 현재 배포 도구의 AWS 인프라 알림은 `features.alerting`에 따릅니다.
- **AWS Health → Your account events**에서 계정별 장애와 예정된 작업을 확인하고, 영향 리소스와 검토할 날짜를 작업 기록에 남깁니다. [NDS 관측 런북](observability.md) · [AWS Health](https://docs.aws.amazon.com/health/latest/ug/getting-started-health-dashboard.html)

### ALB, Route 53, ACM, VPC

- **EC2 → Load Balancers / Target Groups**에서 리스너·호스트 규칙과 Target 상태를 봅니다. `HTTPCode_ELB_5XX_Count`와 `HTTPCode_Target_5XX_Count`를 나눠 보고, 응답 시간과 헬스 체크 실패 이유를 같은 시간대에서 비교합니다.
- **Route 53 → Hosted zones** 또는 사내 DNS에서 실제 레코드 대상을 확인합니다. **ACM → Certificates**에서 인증서의 호스트 이름, 리스너 연결, 만료·갱신 상태, DNS 검증 레코드를 확인합니다.
- **VPC → Subnets / Route tables / Security groups**에서 가용 IP, ALB와 워크로드 간 허용 경로, 관리자 접근 범위, 구성한 NAT 또는 VPC Endpoint의 경로를 확인합니다. DNS나 인증서 전환 뒤에는 직원용과 관리자용 호스트를 각각 검증합니다. [도메인과 TLS](domain.md) · [ALB 메트릭](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-cloudwatch-metrics.html) · [ACM DNS 갱신](https://docs.aws.amazon.com/acm/latest/userguide/dns-renewal-validation.html)

### EKS 또는 ECS, ECR

- **EKS → Clusters**에서 Kubernetes 버전의 지원 종료 일정, 업데이트 상태, 애드온과 Fargate 프로파일을 확인합니다. Pod가 Pending이면 워크로드 이벤트에서 스케줄링과 이미지 가져오기 실패부터 확인합니다.
- **ECS → Clusters → Services**를 사용하는 환경에서는 desired/running 태스크, 최근 배포, 서비스 이벤트, 중지 태스크 이유를 확인합니다. 실패 시 task execution role, 이미지, 시크릿 참조와 Target Group을 대조합니다.
- **ECR → Repositories**에서 현재 실행 이미지와 직전 정상 이미지의 digest를 확인합니다. 보존 정책이 필요한 롤백 이미지를 삭제하지 않는지 확인합니다. [EKS 업그레이드](upgrade-eks-cluster.md) · [업데이트와 롤백](update.md)

### Aurora, RDS Proxy, ElastiCache

- **RDS → Databases / Automated backups**에서 Writer·Reader 상태, 이벤트, 연결 수와 리소스 여유, 백업 보존, 최근 복구 가능 시점을 봅니다. Proxy를 쓴다면 **RDS → Proxies**에서 대상 상태와 연결 대기 시간을 확인합니다.
- 복구 리허설은 Aurora를 별도 클러스터에 복구해 연결을 검증합니다. Proxy를 쓰는 환경은 테스트 경로에서 그 대상을 확인합니다. 스키마, 로그인, 대표 모델 호출, 사용량·감사 조회를 확인하고 복구 소요 시간과 복구 가능한 시점을 기록합니다.
- **ElastiCache → Valkey 또는 Redis OSS**에서 복제 그룹 상태, `EngineCPUUtilization`, 메모리 사용, `CurrConnections`, `Evictions`와 복제 지연을 확인합니다. 장애 이후 게이트웨이 예산 집행과 비용 이벤트 처리 지연도 확인합니다. [재해 복구 런북](dr-runbook.md) · [Aurora 백업](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Managing.Backups.html) · [ElastiCache 메트릭](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/CacheMetrics.WhichShouldIMonitor.html)

### Cognito, IAM, Secrets Manager, CloudTrail

- **Cognito → User pools**에서 앱 클라이언트, 콜백·로그아웃 URL과 활성 IdP를 확인합니다. ADFS를 쓰면 SAML 메타데이터·서명 인증서 교체 일정과 그룹 매핑을 확인합니다. `custom:groups`로 매핑하는 경우 해당 속성의 가변성과 앱 클라이언트 읽기·쓰기 권한을 확인하고, 테스트 사용자의 속성·토큰 클레임·실제 권한까지 검증합니다.
- **IAM → Roles**에서 배포·서비스·교차 계정 역할의 신뢰 정책과 권한을 대조합니다. **Secrets Manager**에서 소유자, 마지막 변경, 회전 방식과 소비 워크로드를 확인합니다. 회전 뒤 DB 연결과 Pod/태스크의 동작을 검증합니다.
- **CloudTrail → Event history**에서 변경 시각, AWS 리전, 리소스, 실행 주체를 확인합니다. 조직 단위나 장기 조회가 필요하면 계정의 감사 로그 보관 경로를 인계합니다. [ADFS 런북](adfs-sso.md) · [Cognito 속성 매핑](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-specifying-attribute-mapping.html) · [CloudTrail 이벤트 기록](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/view-cloudtrail-events.html)

### Bedrock과 선택 기능

- **Bedrock / Service Quotas**를 실제 모델 호출 계정과 출발 리전에서 확인합니다. 활성 모델과 inference profile, IAM 권한과 필요한 구독·접근 조건, 모델별 요청·토큰 할당량을 대조합니다. 교차 리전 inference profile을 쓰면 대상 리전에 적용되는 IAM·SCP도 확인합니다.
- 웹 검색, BI Insight, 외부 단가, 본문 로그 또는 이메일이 활성화된 환경은 의존 서비스를 인수합니다. 해당하는 AgentCore Gateway/Runtime, Lambda, Firehose/S3, SES 등의 상태와 로그, 실행 역할, 전달 결과를 확인합니다. [환경 설정](gateway-yaml.md) · [Bedrock Runtime 할당량](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-runtime.html) · [교차 리전 profile](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html)

### AWS Billing

- **Billing and Cost Management → Cost Explorer / Budgets / Cost Anomaly Detection**에서 계정·서비스·리전 또는 적용된 비용 태그 기준으로 추이를 확인합니다. Bedrock, Fargate, Aurora, ElastiCache, NAT와 로그 수집·보관 비용을 분리해 봅니다.
- 예산·이상 탐지의 감시 범위와 수신자를 확인합니다. 교차 계정 호출 비용은 실제 청구 계정을 확인하고, 게이트웨이 보고 금액과 비교할 경우 기간·서비스·비용 항목을 맞춥니다. [AWS Cost Anomaly Detection](https://docs.aws.amazon.com/cost-management/latest/userguide/getting-started-ad.html)

## 3. 장애 시 첫 분류

계정·리전, 시작 시각과 타임존, 영향 리소스, AWS 이벤트 또는 요청 ID, 최근 변경을 기록합니다. AWS Health 이벤트가 있으면 실제 영향 리소스와 대조합니다.

| 증상 | AWS 점검 순서 | 인계 대상 |
|---|---|---|
| 접속 실패 또는 502/503/504 증가 | DNS·TLS → ALB 오류와 Target 오류 → Target 헬스 → Pod/태스크 이벤트 | 네트워크·플랫폼 |
| 로그인 루프 또는 그룹 누락 | Cognito 앱 클라이언트의 콜백과 IdP → SAML 매핑 → 테스트 계정의 토큰 그룹 | SSO·플랫폼 |
| 모델 호출 거부 또는 429 | Bedrock 오류 코드 → 실제 호출 역할·계정·리전 → 모델/profile 권한과 쿼터 | 모델 제공 계정 담당자 |
| DB 연결 실패 또는 급격한 지연 | RDS 이벤트 → Writer·Proxy 대상 상태와 연결 대기 → 배포 또는 네트워크 변경 이력 | DB·플랫폼 |
| 캐시 오류 또는 비용 처리 지연 | ElastiCache 이벤트 → 연결·메모리·복제 → 워커 건강 상태와 비용 대사 | 플랫폼·게이트웨이 운영 |
| 인프라 알림 미수신 | 알람 상태와 이력 → 액션·SNS 구독 → 당직 채널의 테스트 이벤트 | 모니터링·당직 |
| AWS 청구 급증 | 비용 계정·기간 → 서비스·리전·태그 → 배포·사용량 변경 및 이상 탐지 내역 | 비용·서비스 소유자 |

## 4. 정기 점검과 변경 완료 조건

| 시점 | 확인할 일 |
|---|---|
| 매일 | CloudWatch 알람, AWS Health, Target/RDS/ElastiCache 상태, 비용 이상 이벤트 확인 |
| 매주 | 인증서·SSO 메타데이터 갱신과 유지보수 일정, 백업 복구 가능 시점, 미해결 경고 확인 |
| 매월 | 비용·할당량·서브넷 여유, ECR 보존과 롤백 이미지, IAM·알림 수신자와 운영 접근 검토 |
| 분기 또는 인수 시 | Aurora 복구 리허설, 당직 테스트 알림, 비상 운영 접근, 교차 계정 모델 테스트 |
| 변경 전후 | 변경 소유권과 현재 상태 기록 → 선언과 계획 검토 → 적용 → 리소스 상태와 서비스 기능 확인 |

콘솔 점검 결과를 `gateway.yaml`, Terraform 및 플랫폼 팀 관리 선언과 대조합니다. 긴급 콘솔 변경 시 실행 이유, 변경 내역, 승인 및 복구 조건을 기록하고, 다음 배포 전에 선언과 상태를 맞춥니다. 복구 또는 변경은 필요한 대상이 정상 상태이며 로그인·대표 호출·비용 기록·알림의 관련 검증을 통과했을 때 완료로 기록합니다. [운영·배포 인수인계](op-guide/README.ko.md) · [배포 변경 절차](update.md)
