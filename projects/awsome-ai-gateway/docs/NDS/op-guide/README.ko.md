# NDS Gateway 운영·배포 인수인계

**한국어** · [English](README.md) · [NDS 문서](../README.md)

SSIR 게이트웨이를 인수하는 운영자가 환경과 권한을 확인하고, 변경·장애·복구를 수행하는 데 쓰는 문서입니다. 콘솔의 버튼과 화면은 [관리자 상세 가이드](../guides/admin-guide.ko.md)에서, 기준 upstream 이후의 변경 이유와 범위는 [upstream 비교](../upstream-diff/README.md)에서 확인할 수 있습니다.

AWS 콘솔에서의 서비스별 점검과 환경 인벤토리는 [AWS 인프라 운영 가이드](../aws-infrastructure-operations.md)를 사용합니다.

## 0. 인수 첫날에 확인할 것

1. 대상 환경별로 AWS 계정, 리전, 배포 대상과 호스트, Kubernetes context 또는 ECS 클러스터, 도메인, Terraform 상태 위치와 소유자를 기록합니다. 운영과 dev는 각각 기록합니다.
2. 배포 권한으로 읽기 전용 점검을 하고, 현재 앱 이미지·DB 마이그레이션·기능 플래그와 알림 수신자를 기록합니다. 승인된 보안 저장소에서 환경별 비밀의 위치와 복구 권한을 확인합니다.
3. 운영자가 실제로 관리자 UI에 로그인해 대시보드와 감사 로그를 열어 봅니다. 일반 직원 계정으로 웹 로그인, 팀·역할 확인, CLI 교환, 게이트웨이 첫 요청까지 별도로 검증합니다.
4. 직전 정상 릴리스의 서비스별 이미지와 배포 설정을 보관합니다. 복구 담당자, 네트워크·SSO·데이터 소유자 및 에스컬레이션 경로를 확인합니다.
5. 첫 주에 비운영 환경을 대상으로 알림 전달과 백업 복구를 검증합니다. 마지막 절의 합격 항목에 증거와 담당자를 기록합니다.

### 확인한 dev 배포 기준점

2026-11-28 KST, dev EKS Deployment 명세를 읽어 확인한 이미지입니다. 현재 운영 시에는 다시 조회해 사용합니다. 아래 값은 Deployment의 원하는 상태이며 Pod 이미지 digest, rollout 완료 또는 운영 환경의 상태는 별도로 확인합니다.

| 워크로드 | 이미지 태그 |
|---|---|
| admin-ui | `4fe6d927` |
| admin-api, gateway-proxy, cost-recorder-worker, scheduler | `8040b1d5` |
| notification-worker | `62493ba1` |

같은 조회에서 admin-ui와 admin-api의 `DEV_LOGIN_ENABLED=false`, scheduler의 `ROI_AGGREGATION_ENABLED=false`가 직접 선언되어 있었습니다. `AICM_API_ENABLED`와 `BUSINESS_ALERTS_ENABLED`는 직접 지정된 환경 변수에서 확인되지 않았으며 소스 기본값은 `false`입니다. 채택 여부를 판단할 때는 ConfigMap 및 Secret 참조와 실제 동작까지 확인합니다. dev Prometheus에는 24시간 보존이 선언되어 있고 영구 스토리지가 설정되지 않았습니다.

## 1. 서비스 구조와 운영 책임

| 구성 | 운영자가 확인할 것 |
|---|---|
| ALB·DNS·TLS | 직원용 Gateway와 관리자용 호스트, 접근 허용 CIDR, 대상 그룹 상태, 인증서 갱신 |
| admin-ui / admin-api | Cognito 로그인, 사용자·팀·모델·앱·예산 관리, 감사 로그, 운영 작업 |
| gateway-proxy | VK 검증, 클라이언트·모델 허용, 예산 예약, 외부 모델 라우팅, 스트리밍 |
| Aurora PostgreSQL | 사용자, 키, 정책, 사용·비용·감사 데이터의 영구 저장, 마이그레이션 및 PITR |
| ElastiCache | 인증·정책 캐시, 예산·레이트리밋 카운터, 작업 지연과 정산 상태 |
| cost-recorder-worker, scheduler | 사용량 확정·재처리·대사, 집계와 키 수명주기 |
| notification-worker, 관측 스택 | 비용·예산 이벤트 알림, 메트릭·CloudWatch·Slack·SNS 경로 |
| Bedrock·외부 모델·검색 | 계정별 구독과 역할, 허용 모델 ARN, 호출 출구와 공급자 오류 |

장애를 접수하면 먼저 로그인·관리 경로와 모델 호출 경로를 구분합니다. 호출은 정상이고 비용만 늦을 때에는 비용 워커와 집계를 먼저 확인합니다. [전체 구성과 설치](../gateway-yaml.md) · [관측·알림](../observability.md)

## 2. 배포 환경과 호스트

| 상황 | 배포 경로 | 시작 문서 |
|---|---|---|
| 단일 호스트 평가 | Compose, t0 | [Compose 설치](../install-compose.md) |
| ECS Fargate 운영 | ECS, 용량 밴드·태스크 상태 확인 | [ECS 설치](../install-ecs.md) |
| SSIR 인수 또는 EKS 배포 | EKS, Terraform과 Helm 대상 분리 | [EKS 설치](../install-eks.md) |
| 기존 가동 환경 편입 | 대상 유지, doctor capture → 검토 → plan | [업데이트](../update.md) |

명령은 게이트웨이 프로젝트 루트에서 실행합니다. `deployment/gateway.yaml`을 기본으로 사용하며, 대상이 여러 개면 승인된 경로를 `--config`로 명시합니다. EKS에서는 절차에 맞는 context를 명시합니다. 도구·계정·역할은 [배포 호스트 준비](../install-prereq.md)에서 확인합니다.

```bash
aws sts get-caller-identity
kubectl config get-contexts
kubectl --context "<eks-context>" -n llm-gateway get deploy
kubectl --context "<eks-context>" -n llm-gateway get pods
./deploy doctor --config "<config-path>" --context "<eks-context>"
```

위 Kubernetes 예시는 EKS용입니다. 실제 네임스페이스가 다르면 환경 인벤토리의 값을 사용합니다. 신규 설치 전 `./deploy bootstrap --config "<config-path>" --plan`으로 계획을 확인합니다. 기존 환경은 `./deploy doctor --capture --target eks --context "<eks-context>" --out "<private-capture-path>"`로 현재 상태를 읽어 선언을 대조합니다. capture 파일을 제한된 위치에 보관하고 도메인·CIDR·ARN·상태 저장소·플래그를 확인합니다. 미확인 값은 실제 서비스 상태를 찾아 확인한 뒤 반영합니다. [설정 스키마](../gateway-yaml.md)

## 3. 변경·릴리스·롤백

1. 변경 기록에 대상, 변경 이유, 설정·태그 차이, 위험 범위, 검증 계정과 롤백 조건을 남깁니다. Aurora, EKS 제어판, DNS·SSO처럼 별도 소유자가 있는 변경은 작업 시간과 확인 담당자를 합의합니다.
2. EKS에서는 직전 각 서비스 태그와 현재 DB 리비전을 기록합니다. 공통 이미지 핀은 `images.tag`, 서비스별 핀은 `images.tags`입니다. ECS와 Compose는 대상별 업데이트 가이드에 따라 릴리스 입력을 기록합니다.
3. `./deploy apply --config "<config-path>" --context "<eks-context>" --plan`으로 계획을 확인합니다. 예기치 않은 리소스 교체, IAM/CIDR 범위 확대, 다른 환경의 상태 참조, 알 수 없는 데이터 변경이 보이면 확인될 때까지 반영을 보류합니다.
4. 승인된 계획대로 동일 입력으로 `--plan`을 제거해 실행합니다. 롤아웃은 서비스별 가용 replica, Pod/태스크, 마이그레이션 작업, 5xx·대기열·TTFT 및 워커 지연 순으로 확인합니다.
5. `doctor`와 EKS 스모크 테스트, 일반 직원 호출, 관리자 로그인으로 확인합니다. `--with-bedrock`은 실제 모델 호출이 승인된 경우에 사용합니다. 비용은 워커 반영까지 확인합니다.
6. 계속 실패하면 실패 서비스, 마이그레이션 결과와 알림을 기록하고, 현재 스키마와 호환성이 확인된 직전 이미지를 배포 설정으로 복구합니다. 결정한 롤백의 계획·적용 후 검증도 동일하게 수행합니다. 마이그레이션은 forward-only이므로 데이터 오류는 별도 복구 절차로 다룹니다.

```bash
kubectl --context "<eks-context>" -n llm-gateway rollout status deployment/gateway-proxy
./deploy doctor --config "<config-path>" --context "<eks-context>"
./deployment/scripts/smoke-test.sh --env "<dev-or-prod>" --context "<eks-context>"
```

Terraform 환경과 상태 소유권은 인수인계 항목입니다. 관리 overlay가 담당하는 Cognito·IRSA·로그·알림 등을 수동 변경한 경우 선언과 상태를 다시 맞춥니다. VPC·EKS·Aurora·ElastiCache 작업은 환경별 Terraform 계획을 확인합니다. `./deploy upgrade eks --to "<next-minor>" --plan` 경로는 한 번에 한 minor이며, 제어판·애드온·노드와 워크로드를 단계별로 확인합니다. 철거는 `teardown`과 `teardown --infra`의 범위가 달라 계획·백업·복구 의존성을 검토합니다. [업데이트와 롤백](../update.md) · [EKS 업그레이드](../upgrade-eks-cluster.md)

## 4. 인증·입사·퇴사 처리

### 직원과 관리자 등록

1. 직원의 권한 소스를 확인합니다. 네이티브 풀은 Cognito 그룹, ADFS 연동 환경은 매핑된 SAML 속성 및 토큰 그룹이 기준입니다. 설정의 `oidc.groups_claim`, `required_group`, `group_prefix`, `reject_unmatched_groups`, 관리자 그룹을 확인합니다.
2. 웹 로그인은 JWT를 검증하고 사용자·팀·역할을 등록합니다. 재로그인은 이 경로를 다시 실행합니다. 대량 조정은 **Users/Teams → Sync Cognito**로 실행하고 변경 건수·오류와 표본 사용자의 실제 배정을 확인합니다. 수동 팀 배정은 원본과 감사 로그를 확인한 뒤 수정합니다.
3. 직원에게 [직원 PC 가이드](../client-setup.md)와 환경에 맞는 `./deploy client-values` 결과를 제공합니다. `gateway-cli login`은 별도로 VK를 발급합니다. 사용자 한 명을 대상으로 할당 팀과 앱을 확인하고 작은 요청 한 건을 호출합니다.
4. 조직 이동이나 퇴사 시 권한 원본을 바꾸고 게이트웨이에서 팀·활성 상태·가상키를 확인합니다. 필요한 키를 폐기하고, 한 번 더 기존 자격으로 거부되는지 확인한 뒤 감사 로그에 변경을 남깁니다.

### ADFS → Cognito

ADFS 측에는 RP Identifier `urn:amazon:cognito:sp:<pool-id>`와 HTTPS ACS `https://<cognito-domain>/saml2/idpresponse`를 각각 전달합니다. SSIR SSO 담당자에게 그룹 claim 이름과 실제 값 형식을 확인합니다. 예를 들어 `ds_dm`을 그룹 값으로 받기로 했다면 assertion의 원본 속성에서 Cognito `custom:groups`로 매핑되고, 로그인한 사용자의 토큰에 기대 값이 들어있는지 확인합니다. 풀 속성이 가변이며 해당 앱 클라이언트에 필요한 읽기·쓰기 권한이 있어야 합니다. 비교 종료점의 Terraform 모듈에는 `custom:groups`가 읽기 목록에 있고 쓰기 목록에는 없습니다. 실제 앱 클라이언트와 선언을 대조한 뒤 연동을 검증합니다.

테스트 계정으로 **ADFS 로그인 → Cognito 사용자 속성 → 토큰 클레임 → 게이트웨이 팀·역할**을 순차 확인합니다. 누락 지점에 따라 ADFS claim rule, Cognito 매핑·앱 클라이언트 권한 또는 게이트웨이 그룹 규칙을 수정합니다. 관리자와 일반 직원, 거부되어야 할 사용자 권한을 각각 확인합니다. Cognito 앱 클라이언트에 지원되는 IdP를 바꾸기 전 승인된 비상 계정의 접근도 확인합니다. [ADFS 런북](../adfs-sso.md) · [그룹 매핑](../ad-sync.md)

### 자격 증명 구분

| 종류 | 발급·대응 |
|---|---|
| 직원 VK | 사용자 CLI 로그인으로 발급. 노출·퇴사 시 폐기 확인 |
| 서비스 계정 VK | **Agents**에서 발급. 대상 앱·저장소·교체 담당자 식별. 키 교체 후 소비자 테스트를 즉시 수행 |
| 일반 관리자 자동화 토큰 | 운영 목적에 맞는 서비스 토큰으로 발급·만료 관리 |
| AICM 전용 토큰 | 범위와 CIDR을 지정한 `svc-` 토큰. 별도 미러 경로 전용. 기능 플래그와 계약을 확인한 뒤 사용 |

토큰·VK 값은 승인된 저장소를 통해서만 전달합니다. 키 교체의 영향과 기존 소비자를 작업 기록에 남깁니다. [관리자 상세 가이드](../guides/admin-guide.ko.md) · [AICM 및 인증 변경](../upstream-diff/identity.md)

## 5. 일상 운영

### 모델, 라우팅, 예산

- 신규 모델은 별칭·provider ID·가격·앱 접근 조건을 확인합니다. 계정 라우팅의 우선순위는 모델별 ARN, 클라이언트별 ARN, 실행 환경 IAM입니다. 실제 호출 계정의 역할과 구독을 확인하고 등록 후 한 건을 호출합니다.
- **Sync Pricing**에서 소스를 선택해 미리 보고 선택한 항목만 반영합니다. LiteLLM 소스는 가격 Lambda 배포가 필요합니다. 적용 후 입력·출력·캐시·장문 가격과 Analytics 사용 내역을 대조합니다.
- 모델을 사용 중지하기 전에 기본 모델·팀 정책·다운그레이드 목적지에 대한 참조를 확인합니다. 기록을 보존하면서 신규 호출을 막으려면 비활성화를 사용합니다. 삭제는 가격과 접근 설정에 영향을 주므로 영향 목록을 확인합니다.
- 팀·사용자 한도와 Hard Block·Warning Only·Throttle 설정을 확인합니다. Claude Code와 Cowork의 앱 그룹 상한은 해당 앱 사용자 한도와 함께 시행됩니다. `0`은 명시적인 차단이며 그룹 상한 삭제는 그룹 제한을 해제합니다.
- 웹 검색 쿼터의 조직·팀 정책, 사용자 예외와 모델 예산을 각각 확인합니다. 모델 비용, 웹 검색 비용, backfill 값이 같은 수치로 표시된다고 가정하지 않습니다. 기준 영업일·타임존과 조회 기간을 맞춰 비교합니다.

### 모니터링, 감사, 비용

| 주기 | 확인과 인계 기록 |
|---|---|
| 매일 | 로그인·호출 장애, 오류율과 TTFT, Pod/태스크, Redis·DB, 비용 적재 지연과 알림을 확인합니다. 이상 건의 최초 시간·환경·요청 ID를 남깁니다. |
| 매주 | **Audit Log**의 변경, 예산 알림 후속 대응, 사용량 누락 여부, 알림 채널 수신자와 근무 교대를 확인합니다. |
| 매월 | 가격과 공급자 ARN, 부하·DB·Redis 여유, 구독, 인증서 만료, 활성 서비스 키와 만료일을 검토합니다. |
| 분기 | Aurora 시점 복구 또는 Compose 백업 복구를 리허설하고, SSO·비상 계정·복구 역할을 시험합니다. |
| 변경 직후 | rollout, 스모크, 실제 사용 1건, Analytics/원장 반영과 감사 이벤트를 확인합니다. |

CloudWatch·SNS는 인프라, Prometheus·Alertmanager는 앱 메트릭, notification-worker의 비용 정확도 Slack은 원장 대사를 대상으로 합니다. 각 기능과 구독이 활성화돼 있고 의도한 담당자가 시험 알림을 받았는지 확인합니다. EKS Alertmanager는 관측 스택의 `observability.alerting.enabled` 및 해당 namespace의 Slack Secret을, 비용 알림은 `features.business_alerts`와 대상별 워커 설정을 확인합니다. 수집 이력과 사고 시간을 함께 확인할 수 있도록 보존 기간을 인계합니다. [관측 런북](../observability.md)

비용 분석이 늦으면 먼저 시간대와 데이터 흐름을 확인합니다. 비용 워커 기본 시간대는 배포의 `REPORTING_TIMEZONE`을 따릅니다. 기본 hourly 집계는 매시 05분, daily는 00:10, 대사는 01:00입니다. reconciliation, ROI, 키 purge 등의 실행 여부는 배포 설정으로 확인합니다. 본문 로그는 비마스킹 payload를 저장할 수 있으므로 승인된 대상·기간에만 사용하고 조회·종료 사실을 기록합니다.

## 6. 장애 대응

1. 티켓에 환경·영향 대상·최초 발생 시각과 타임존, 요청 ID, 최근 릴리스와 변경을 남깁니다. 우선 일시적인 개별 요청 문제인지 전체 서비스 문제인지 판별합니다.
2. 대시보드·로그·감사 이벤트의 시각을 맞춰 분석합니다. 사용자에게 보이는 오류, 해당 계층의 메트릭, 확인된 원인을 구분해 기록합니다.
3. 영향이 큰 변경을 중단하고 담당자를 호출합니다. 복구 후 실제 요청, 비용 반영, 알림이 정상으로 돌아왔는지 확인합니다.

| 증상 | 점검 순서 |
|---|---|
| 로그인 루프 또는 403 | 사용자 유형, Cognito callback, IdP 연결 상태, 토큰의 그룹과 팀·역할, admin API의 provision 결과 확인 |
| VK 또는 agent 401 | 정확한 환경, 만료·폐기 여부와 키 범위 확인. 서비스 계정은 **Repair auth** 전후 테스트 |
| 예산 또는 쿼터 거부 | 현재 요청의 앱·모델·사용자, 대시보드의 유효 정책, 팀 한도, 앱 그룹 한도·다운그레이드, 검색 쿼터와 Redis 상태 확인 |
| Bedrock AccessDenied | 선택된 모델·클라이언트 ARN, 실제 호출 계정, 역할·구독·리전·허용 정책 확인 |
| 지연 또는 503 | proxy 대기열·TTFT, 공급자 오류, replica/태스크, Redis 연결·클러스터, DB pool을 순서대로 확인 |
| 집계 지연·비용 차이 | 보고 시간대와 기간을 맞춘 후 스트림 지연, cost-recorder, rollup, reconciliation, 추정·backfill 표시 확인 |
| 알림 미수신 | 원본 이벤트가 발생했는지 먼저 확인하고, 해당 경로의 플래그·채널·대상·구독 상태를 추적 |

[운영 런북](../operations.md) · [부하 검증](../loadtest.md)

## 7. 데이터 복구와 인계

### 복구 실행

- Aurora 복구 전 복구 목표 시각, 예상 데이터 유실 범위, 검증 담당자와 트래픽 전환 순서를 합의합니다. PITR 또는 스냅샷을 새 클러스터에 복구하고 인스턴스·접근 경로를 구성합니다. RDS Proxy가 포함되면 대상과 헬스 체크를 확인합니다.
- 승인된 새 대상에 연결한 뒤 스키마와 표본을 읽어 검증합니다. 필요한 워크로드를 전환하고, 로그인·호출·비용·감사·집계를 확인합니다. 선언과 Terraform 상태를 복구 대상에 맞춰 이후 apply가 되돌리는 일을 방지합니다.
- Compose는 오프호스트 백업의 최신 시각과 복구 테스트 기록을 확인합니다. EKS·ECS는 실제 DB 보존 기간과 복구 소요 시간을 인계 시 기록합니다. 이 값으로 RPO와 RTO를 확정합니다.
- `VIRTUAL_KEY_ENCRYPTION_KEY` 유실 시 복구 저장소부터 확인합니다. 복구본이 없으면 새 키를 적용하고 기존 직원·에이전트 VK를 재발급합니다. 소비자별 전달·전환·검증 담당자와 영향 범위를 기록합니다.

### 환경별 인계 기록

| 기록 항목 | 남길 근거 |
|---|---|
| 접근 및 경로 | 계정·리전·배포 IAM 역할·context·namespace 또는 ECS 클러스터·도메인, DNS·SSO·네트워크 담당자 |
| 릴리스 | 현재·직전 정상 서비스 태그, Pod 이미지 ID 또는 ECS task revision, DB 리비전, 승인 계획과 롤백 조건 |
| 복구 | 백업 시각과 복구 가능 구간, 키 보관 위치 확인, 리허설 복구 소요 시간과 검증 결과 |
| 온콜과 알림 | 장애 분류별 연락처, 공급자 에스컬레이션, 알림 테스트와 수신 기록, 관측 보존 기간 |
| 미해결 항목 | 영향 범위, 담당자, 다음 검토일, 별도 보관한 증거 링크 |

### 인수 완료 기준

- [ ] 인수자가 AWS 역할, 저장소, 배포 호스트, 환경별 context와 제어권을 검증했습니다.
- [ ] 실제 `gateway.yaml`, 로컬 values, 환경별 Terraform 설정과 상태 위치, 도메인·CIDR·Cognito 설정을 승인된 위치에 보관했습니다.
- [ ] `doctor`, 스모크 테스트, 관리자 로그인, 직원 온보딩 및 호출의 결과를 기록했습니다.
- [ ] 서비스별 이미지·플래그·DB 리비전, ECR의 직전 정상 이미지와 되돌림 검증 방법을 인계했습니다.
- [ ] 비밀 경로, VK 암호화 키의 두 보관 위치, 서비스 계정 소비자·소유자와 교체 담당자를 확인했습니다.
- [ ] DNS·네트워크·ADFS·AWS 인프라·DB·비용 담당자와 온콜, 알림 수신자·보존 기간을 기록했습니다.
- [ ] 백업 복구 리허설, 알림 전달 최종 확인과 미해결 항목의 담당자·기한을 기록했습니다.

[재해 복구](../dr-runbook.md) · [환경별 세부 배포 명령과 설정](README.md) · [NDS 문서 목차](../README.md)
