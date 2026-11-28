# NDS Gateway 운영·배포 인수인계

**한국어** · [English](README.md) · [NDS 문서](../README.md)

인프라 배포와 게이트웨이 인수인계를 담당하는 운영자를 위한 가이드입니다. 콘솔에서의 기능별 절차는 [관리자 상세 가이드](../guides/admin-guide.ko.md)에서 확인합니다.

## 1. 배포 환경 결정

| 상황 | 경로 | 추천 시작점 |
|---|---|---|
| 평가·개념 검증 | Compose, t0 단일 노드 | [Compose 설치](../install-compose.md) |
| 소규모~일반 운영 | ECS Fargate | [ECS 설치](../install-ecs.md) |
| 신규 Kubernetes 또는 대규모 | EKS | [EKS 설치](../install-eks.md) |
| 가동 중인 환경 인수 | 현재 대상 유지, capture 후 검토 | [기존 배포 편입](../update.md) |

`deployment/gateway.yaml`에 환경을 선언합니다. 환경 이름, AWS 계정·리전, Terraform 상태, EKS context를 확인합니다. ECS·EKS의 `deploy.capacity_tier`는 사용자 수 밴드이며, 필요한 경우 `deploy.sizing`으로 개별 설정을 덮습니다. [설정 설명](../gateway-yaml.md)

## 2. 배포와 변경 절차

1. [배포 호스트 준비](../install-prereq.md)를 완료합니다. 신규 환경은 `./deploy bootstrap --plan`으로 계획을 확인한 뒤 `./deploy bootstrap`을 사용합니다.
2. 기존 환경은 `./deploy doctor --capture`의 결과를 현재 운영 값과 비교합니다. 비밀을 제외하고 계정·리전·도메인·컨텍스트·기능 플래그를 검토합니다.
3. 업데이트는 `./deploy apply --plan`으로 범위를 확인하고 `./deploy apply`로 반영합니다. `./deploy doctor`와 환경의 스모크 테스트로 확인합니다.
4. 앱 롤백 시 이전 이미지 태그를 사용합니다. DB 마이그레이션은 forward-only 방식이므로 이전 앱과 현재 스키마가 호환되는지 확인합니다.
5. EKS 제어판, 애드온, Aurora는 독립된 유지보수 절차로 다룹니다. 삭제는 `./deploy teardown`의 범위와 복구 가능한 백업을 확인한 뒤 진행합니다. [업데이트와 롤백](../update.md) · [EKS 업그레이드](../upgrade-eks-cluster.md)

## 3. 인증과 직원 등록

- Cognito 네이티브 계정은 해당 풀에 만들고 팀 그룹을 배정합니다. 관리자 권한을 가진 그룹은 별도로 통제합니다.
- ADFS 환경은 IdP, ACS URL, RP Identifier 및 `custom:groups` 매핑을 확인합니다. 풀 속성, 토큰 클레임, **Users/Teams**의 팀·역할이 일치하는지 확인합니다. [ADFS 배포](../adfs-sso.md) · [그룹 매핑](../ad-sync.md)
- 웹 로그인 콜백이 사용자를 등록합니다. **Sync Cognito**는 Cognito 레코드를 배치로 조정합니다. CLI 사용자는 `gateway-cli login`에서 VK를 발급받습니다.
- 직원에게 [직원 PC 가이드](../client-setup.md)와 해당 환경의 `./deploy client-values` 결과를 전달합니다.

## 4. 일상 운영

| 대상 | 절차 |
|---|---|
| 팀 및 사용자 | **Users/Teams**에서 소속, 역할, 상속 정책을 확인합니다. 제한 변경 뒤 적용 결과를 다시 확인합니다. |
| 모델 및 라우팅 | **Model Management**에서 별칭과 단가를, **App Policy**에서 기본 모델과 앱별 라우팅을 확인합니다. |
| 예산과 요청 제한 | 팀과 사용자 한도, 앱 기준, 초과 정책, 웹 검색 쿼터를 확인합니다. |
| 키 | 사용자 VK는 CLI로 발급합니다. 서버 앱의 장기 키는 **Agents**에서 발급하며 교체 시 기존 키가 즉시 만료됩니다. |
| 장애 조사 | **Monitoring**에서 요청 ID를 찾고 **Audit Log**에서 설정 변경을 확인합니다. **Analytics**에서 기간과 영향 범위를 비교합니다. |

절차와 판단 기준은 [운영 런북](../operations.md), 화면별 안내는 [관리자 상세 가이드](../guides/admin-guide.ko.md)를 참고합니다.

## 5. 관측·용량·복구

1. 인프라 상태는 CloudWatch·SNS, 앱 메트릭은 Prometheus·Alertmanager, 비용 원장 이상은 notification-worker에서 확인합니다. 운영 환경의 구독 승인을 완료하고 대상 채널의 수신을 확인합니다. [관측·알림](../observability.md)
2. 스케일 결정은 요청 대기열, 첫 응답 시간, Redis 연결 오류, DB 여유를 함께 봅니다. [부하 검증](../loadtest.md)에서 모의 테스트와 원복 절차를 확인합니다.
3. Aurora 복구는 시점 복구 또는 백업으로 새 클러스터를 만든 뒤 DB 주소나 RDS Proxy 대상을 검증합니다. Compose에는 오프호스트 백업이 필요합니다. [재해 복구](../dr-runbook.md)
4. VK 암호화 키를 보호합니다. 유실 시 사용자와 에이전트 키의 재발급이 필요합니다.

## 6. 인수인계 점검

- 작업자 권한, 저장소, 배포 호스트, AWS 계정·역할과 EKS context를 확인합니다.
- 환경 설정, 상태 저장소, Cognito 풀·클라이언트, 도메인 및 출구 CIDR의 소유자를 기록합니다.
- DB 복구 리허설, 관리자 로그인, 직원 로그인과 첫 요청, 알림 수신을 확인합니다.
- 시크릿 값과 실제 환경 파일은 승인된 저장소에서 인계합니다. 점검 기록에는 이름과 소유자만 남깁니다.

[상세 영어 인수인계 문서](README.md)에는 용량 밴드, 계정 라우팅, 스케줄러 기본값과 인수인계 체크리스트가 있습니다.
