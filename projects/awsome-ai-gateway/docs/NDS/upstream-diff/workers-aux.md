# 워커, 비용과 관측

**한국어** · [English](workers-aux.en.md) · [비교 목차](README.md)

## 비용 데이터 흐름

프록시가 사용·비용 이벤트를 만들고 비용 워커가 스트림을 소비해 저장과 집계를 진행합니다. `87ff960b`는 비용 정밀성, `11aad69c`는 내구성, `23c0c6c8`은 대사 조정을 추가합니다. 요청 시작/종료 기록과 대응되는 확정 이벤트, DB fallback과 재처리, 비정상적으로 열린 요청 점검을 포함합니다. 복구 중 추정치가 생길 수 있어 원장·예산 차이를 보고합니다. 웹 검색 과거 데이터 보정은 `93561142`를 확인합니다.

v2 일·월·시간 집계, dirty mark 및 대사 상태로 지연·재계산을 추적합니다. 기본 시간 집계는 매시 05분, 일 집계는 00:10, 대사는 01:00입니다. 실제 일정과 타임존을 확인합니다. 내부 원장을 대조하며 외부 AWS CUR/CloudWatch 청구 대조는 별도 설계로 관리합니다. [R7 설계](../cost-reconciliation-r7.ko.md) [비용 워커 소스](../../../cost-recorder-worker) · [관리 API·스키마](admin-api.md)

## 알림 경로

| 경로 | 무엇을 확인 |
|---|---|
| AWS 알람 | `features.alerting`, CloudWatch/SNS 구독 승낙, Aurora·ElastiCache·ALB 검증과 선택 로그 전달 |
| Prometheus/Alertmanager | EKS 관측 스택 설치, `observability.alerting.enabled`, 관측 namespace의 Slack Secret과 규칙. 기본 Prometheus 24h/emptyDir와 Alertmanager 상태 보존 120h는 별개 |
| notification-worker | 예산·키 이벤트, dedup·메일 공급자. 비용 대사 알림은 별도 `features.business_alerts`와 대상별 Slack 설정 |

`31072728`에서 비용 알림을 EKS·ECS·Compose에 연결했습니다. EKS는 워커 namespace Secret, ECS는 앱 secret의 `slack_webhook_url`, Compose는 `SLACK_WEBHOOK_URL`을 확인합니다. 운영 화면에 알림이 보일 때 원본 이벤트를 어느 경로가 처리했는지 확인합니다. [관측 런북](../observability.md)

## 부속 서비스

- **가격 Lambda**는 옵션형 LiteLLM 카탈로그 경로입니다. 비활성 환경은 AWS Price List 또는 수동 단가를 사용합니다.
- **BI Insight**는 기존 chat agent를 확장해 AgentCore Runtime·DB reader 설정을 배포 수명주기에 넣고 `/chat` 보고서 흐름에 연결합니다.
- **mock과 부하 테스트**는 내부 스테이지에서 SSE, 동시 로그인, executor 포화와 Redis/DB 용량을 재현하는 데 사용합니다. 모의 호출의 결과와 실제 공급자의 지연·비용을 별도로 확인합니다.

[알림 워커](../../../notification-worker) · [부하 검증](../loadtest.md) · [운영 인수인계](../op-guide/README.ko.md)
