# Observability & Alerting — NDS 관측·알림 가이드

이 문서는 LLM Gateway 의 관측 계층 전체와, NDS 방식(`gateway.yaml` +
`./deploy`)으로 배포되는 알림·로그 기능을 다룹니다.

```
메트릭:   pod → otel-collector → Prometheus → Grafana / prometheus-adapter → HPA
알림:     Aurora·ElastiCache·RDS events → CloudWatch alarms → SNS topic → email
로그:     AWS managed 서비스 → CloudWatch Logs (이미 동작)
          컨테이너 stdout/stderr → kubectl logs 만 (log_shipping 켜면 CloudWatch)
```

> 관측 스택은 있으나 **알림 발송체는 기본 꺼짐**입니다 — 대시보드는 보는
> 도구고, 깨우는 도구는 `features.alerting` 을 켜야 생깁니다.

---

## 1. 두 개의 관측 계층

| 계층 | 구성 | 소유 | 알림 가능? |
|---|---|---|---|
| **In-cluster** | kube-prometheus-stack (Prometheus+Grafana), otel-collector, prometheus-adapter | `deployment/observability/install.sh` — **수동 설치**, managed overlay 아님 | Alertmanager 는 `enabled: false` — 켜면 가능 |
| **AWS managed** | CloudWatch alarms + SNS + Fargate 로그 라우터 | `module.alerting` — `features.alerting` 선언 시 **managed overlay** | ✅ 이 문서의 기능 |

In-cluster 스택은 부하테스트 관측용으로 만들어진 상태입니다:
Prometheus retention **24h**, `emptyDir` 스토리지(파드 재시작 시 메트릭 소실),
Alertmanager 비활성, PrometheusRule·receiver 없음. HPA 의
`executorInflightTarget` 커스텀 메트릭은 prometheus-adapter 경유로 이미
동작합니다 — 스택을 지우면 HPA 가 깨집니다.

## 2. `features.alerting` — NDS 선언

```yaml
features:
  alerting:
    enabled: true                        # SNS topic + CloudWatch alarms + RDS event sub
    emails: ["ops@example.com"]          # 구독 — apply 후 각 주소로 confirm 메일
    log_shipping: true                   # eks 전용 — Fargate → CloudWatch Logs
```

| 키 | 대상 | 동작 |
|---|---|---|
| `enabled` | eks / ecs | `module.alerting` 생성 — SNS topic + 알람 세트 |
| `emails` | enabled 필수 | SNS email 구독 — **수신자가 메일에서 Confirm 해야 발송 시작** |
| `log_shipping` | eks 전용 | `aws-observability` namespace + `aws-logging` ConfigMap → Fargate 내장 fluent-bit 가 전체 stdout/stderr 를 CloudWatch Logs 로 |

검증 규칙: compose 선언은 에러, `emails` 단독은 에러, `log_shipping` 은 eks 가
아니면 에러, `enabled` + `emails` 비움은 경고(알람은 보이지만 수신자 없음).

### 생성되는 리소스

`module.alerting` (`deployment/terraform/modules/alerting/`):

- `aws_sns_topic` — `<project>-<env>-alerts`
- `aws_db_event_subscription` — Aurora cluster 의 `failover`/`failure`/
  `maintenance`/`notification` 이벤트
- Aurora 인스턴스별 알람 × 1: `CPUUtilization > 85%`(2×5분). FreeableMemory
  알람은 의도적으로 제외 — serverless v2 저 ACU 에서는 유휴 시에도 상시
  저수위라 오탐입니다.
- ElastiCache 멤버별 알람 × 3: `EngineCPUUtilization > 75%`,
  `DatabaseMemoryUsagePercentage > 85%`, `CurrConnections > 50,000`
  (노드 상한 ~65k 의 80% 수준 휴리스틱). member id 는 각 env 모듈의
  `member_cluster_ids` output 으로 plan-time 결정 — bootstrap 경로도 안전.

### 안 만드는 것 (의도적)

- **ALB 알람 (eks 자동)** — ALB dimension 은 배포 후 실측값이라 plan 시점에
  알 수 없습니다. `deploy apply` 가 helm 배포 뒤 ingress 를 조회해 LB/TG
  dimension 을 라이브 발견하고 gen var-file 로 `module.alerting` 에 주입해
  자동 생성합니다 (§8-A). 첫 배포(아직 ALB 없음)는 다음 apply 에서 수렴.
- **파드/앱 레벨 알람** — CrashLoop·restarts·executor 포화는 PrometheusRule
  + Alertmanager 계층의 몫입니다.
- **빈 로그 그룹** — `log_shipping` 이 라우터 자체를 만들 때만 로그가 실제로
  흐릅니다.

### 배포

```bash
./deploy validate          # 스키마+검증
./deploy apply --plan      # module.alerting + helm values 미리보기
./deploy apply             # -target=module.alerting 으로 좁혀 적용
./deploy doctor            # managed-overlay plan drift — module.alerting 누락·변경 감지
```

`alert_emails` 주의: overlay 가 발산한 `alert_emails` 는 env tfvars 에
사람이 넣은 같은 키를 대체합니다(나중 -var-file 우선). 즉 terraform 으로
관리된 구독이 env tfvars 에만 있고 gateway.yaml 의 emails 와 다르면 다음
apply 에 교체됩니다 — 콘솔로 추가한 구독은 state 밖이라 안전합니다.

`enabled=false` 로 되돌려도 managed overlay 는 비활성화를 apply 하지
않습니다 — 다른 기능 토글과 같은 계약(destroy 는 env terraform 수동 절차).
알림을 정말 끄려면 env 디렉토리에서
`terraform apply -var='enable_alerting=false'` 로 모듈을 제거합니다.

## 3. 수동 단계 — SNS 구독 확인

SNS email 구독은 설계상 자동화가 불가능합니다 — AWS 가 각 주소로
"AWS Notification - Subscription Confirmation" 메일을 보내고, 수신자가
**Confirm subscription** 을 눌러야 ACTIVE 가 됩니다.

```bash
# 구독 상태 확인 (PendingConfirmation 면 아직 미확인)
aws sns list-subscriptions-by-topic \
  --topic-arn "$(cd deployment/terraform/environments/llm-gateway-<env> \
    && terraform output -raw alerting_sns_topic_arn)" --region <region>
```

콘솔: SNS → Topics → `<project>-<env>-alerts` → Subscriptions. Slack/Teams/
Lambda 구독도 같은 topic 에 콘솔로 추가하면 됩니다 — topic·alarm 소유는
terraform, 구독자 관리는 운영 채널 관행대로 수동입니다.

## 4. 알람·로그 확인 방법

```bash
# 알람 상태
aws cloudwatch describe-alarms --alarm-name-prefix <project>-<env>- \
  --region <region> --query 'MetricAlarms[].{n:AlarmName,s:StateValue}'

# 강제 테스트 (실제 장애 없이 알림 발송 확인)
aws cloudwatch set-alarm-state \
  --alarm-name <project>-<env>-cache-<member>-cpu \
  --state-value ALARM --state-reason "notification test" --region <region>
# 확인 후 OK 로 복원 — metric alarm 은 다음 평가에서 실제 값으로 돌아간다
```

### CloudWatch 에 이미 있는 로그 (관리형 서비스)

| 로그 그룹 | 내용 |
|---|---|
| `/aws/rds/cluster/<cluster>/postgresql` | DB 에러·슬로우쿼리·failover |
| `/aws/rds/proxy/<proxy>` | 프록시 연결 실패 |
| `/aws/elasticache/<id>/engine`, `.../slow` | Valkey 이벤트·슬로우 |
| `/aws/lambda/<fn>` | pricing/chat-agent 람다 |
| `/aws/eks/<cluster>/cluster` | 컨트롤 플레인 |

### 앱 로그 — `log_shipping` 전/후

| 상태 | 확인 경로 |
|---|---|
| `log_shipping: false` (기본) | `kubectl -n llm-gateway logs deploy/llm-gateway-gateway-proxy --tail=200` — 파드 재시작 시 유실 |
| `log_shipping: true` | CloudWatch → Logs → `/aws/eks/<cluster>/fargate-logs` (ConfigMap 생성 후 뜨는 파드부터; 기존 파드는 재시작 시 적용) |

- `logGroupName` 은 render 가 env 디렉토리명(`<project>-<env>` 규칙)으로
  추론합니다. 커스텀 클러스터명이면 env values 의
  `observability.fargateLogging.logGroupName` 에 직접 선언 — env values 가
  gen 추론보다 우선합니다.
- Fargate 가 아닌 EKS(EC2 nodegroup)에서는 이 라우터가 동작하지 않습니다 —
  해당 환경은 DaemonSet fluent-bit 등 다른 경로가 필요합니다.
- `aws-observability` 네임스페이스 또는 `aws-logging` ConfigMap 을 이미 수동
  생성해 둔 클러스터는 helm 이 "already exists" 로 배포를 거절합니다 — 먼저
  helm 소유로 adopt 하세요:

  ```bash
  kubectl label  ns aws-observability app.kubernetes.io/managed-by=Helm
  kubectl annotate ns aws-observability \
    meta.helm.sh/release-name=<release> meta.helm.sh/release-namespace=<ns>
  kubectl -n aws-observability annotate cm aws-logging \
    meta.helm.sh/release-name=<release> meta.helm.sh/release-namespace=<ns>
  kubectl -n aws-observability label cm aws-logging app.kubernetes.io/managed-by=Helm
  ```

로그 스트림명은 `fargate-<namespace>.<pod>.<container>` 형식입니다.
앱 로그가 CloudWatch 에 온 뒤에는 metric filter(`"ERROR"` 등)로 에러율 알람을
얹을 수 있습니다 — 기본 모듈 범위 밖이며 필요 시 추가하세요.

## 5. Grafana / Prometheus / HPA

```bash
kubectl -n observability port-forward svc/kube-prometheus-stack-grafana 3000:80
# http://localhost:3000 — 대시보드는 부하테스트용이 기본
```

- **executor 큐 포화**: `gateway_bedrock_executor_queue_depth > 0` 지속은
  용량 상한 신호 — §10(op-guide) 의 scale-out 판단 기준과 같은 메트릭입니다.
- **HPA**: `kubectl -n llm-gateway get hpa` — CPU + `executor_inflight` 두 축.
- **Alertmanager 를 켜려면**: 절차는 §8-B — 이 스택은 managed overlay 소유가
  아니라 `deployment/observability/` 의 수동 설치 자산입니다.

## 6. 비용 (ap-northeast-2 기준 대략)

| 항목 | 단가 | dev 추정 | prod 추정 |
|---|---|---|---|
| 알람 | $0.10/개·월 | ~4개 → ~$0.5 | 멤버 수에 비례 → ~$1–2 |
| SNS email | — | 사실상 무료 | 사실상 무료 |
| Fargate 로그 수집 | $0.50/GB | 수십 MB → <$1 | 요청당 ~2KB 가정 시 ~$10–20 |
| 로그 보관 | $0.03/GB·월 | 무시 가능 | 30일 기준 수 $ |

비용 레버: 로그 그룹 retention(기본 무제한 — `aws logs put-retention-policy
--log-group-name ... --retention-in-days 14` 로 제한 권장), DEBUG 레벨 사용
시간, 알람 개수. body logging(S3, 비마스킹 페이로드)은 별개 축으로 훨씬
커질 수 있으니 디버그 시에만 켭니다.

## 7. 운영 체크리스트 (SSIR 인수 기준)

- [ ] `features.alerting.enabled: true` + `emails` 선언 → `./deploy apply`
- [ ] 각 수신자가 SNS confirm 메일을 눌렀는지 `list-subscriptions-by-topic` 으로 확인
- [ ] `set-alarm-state` 로 테스트 알람 1건 → 실제 메일 도착 확인
- [ ] `log_shipping` 켰다면 `/aws/eks/<cluster>/fargate-logs` 에 스트림 유입 확인
- [ ] 로그 그룹 retention 정책 결정(기본 무제한)
- [ ] 알림 수신 채널·당번 체계 합의 — 알람이 울려도 보는 사람이 없으면 의미 없음
- [ ] (선택) Alertmanager + PrometheusRule + persistent Prometheus 스토리지 —
      파드/앱 레벨 알림이 필요하면 (§8-B)
- [ ] ALB 5xx/UnHealthyHost 알람 — `deploy apply` 가 ingress ALB 를 발견해
      자동 생성 (첫 배포 직후엔 다음 apply 에서 수렴, §8-A)
- [ ] (선택) 앱 에러율 알람 — `log_shipping` 켠 뒤 metric filter 경로 (§8-C)

## 8. 보류 작업 절차 (follow-up)

기본 모듈 범위 밖인 세 가지 — 필요 시 아래 순서대로 붙입니다.

### 8-A. ALB 알람 — apply 시 자동 발견 (eks)

ALB·TargetGroup 은 AWS Load Balancer Controller 가 배포 시 생성하고
dimension(ARN suffix)을 그때 부여합니다 — `deploy apply` 는 이를 라이브
발견해 자동으로 알람을 채웁니다:

1. helm 배포 후 `kubectl get ingress -n <ns>` 로 ALB hostname 수집
   (gateway·admin-ui·admin-api 3개 모두)
2. `aws elbv2 describe-load-balancers` 에서 hostname 일치 + `application`
   타입만 매칭 → LB dimension `app/<name>/<id>`
3. LB ARN 각각 `describe-target-groups` 로 TG dimension
   `targetgroup/<name>/<id>` 수집 (LB 당 1회 호출)
4. `gen/<env>/alerts-dims.tfvars` 생성 → `module.alerting` 의
   `alb_lb_dimensions` / `alb_target_groups` 변수로 주입
5. 알람 2종 생성: ALB별 `HTTPCode_Target_5XX_Count ≥ 1`(5m×2),
   TG별 `UnHealthyHostCount > 0`(1m×2) — actions 은 기존 alerting topic

**수렴 계약**: 첫 배포 등 ALB 를 못 찾으면 var-file 을 만들지 않고
`⚠ alerting: ALB dimension 을 발견하지 못했습니다 — ... deploy apply 를
한 번 더 실행해야 알람이 만들어집니다` 를 출력한 뒤 진행합니다 — 이 메시지가
뜨면 **ALB 알람이 없는 상태이므로, ingress ALB 가 뜬 뒤 `deploy apply` 를
한 번 더 실행해야** 알람이 만들어집니다. 주입 var-file 은 apply 마다
재생성되므로 ALB 교체(id 변경)도
자동 추적되고, env tfvars 에 사람이 둔 같은 키는 gen 파일이 덮습니다.

**수동 경로 (ecs/외부 ALB)**: eks 가 아니거나 발견이 불가하면 env tfvars 에
직접 고정합니다 — `aws elbv2 describe-load-balancers`/`describe-target-groups`
로 dimension 을 실측해 `alb_lb_dimensions = ["app/<name>/<id>"]`,
`alb_target_groups = [{lb=..., tg="targetgroup/<name>/<id>"}]`.

### 8-B. Alertmanager + PrometheusRule (in-cluster 알림)

`deployment/observability/` 는 managed overlay 소유가 아닌 수동 설치 자산 —
이 디렉토리에서 직접 변경합니다.

1. `kube-prometheus-stack/values.yaml`:
   - `alertmanager.enabled: true`
   - `alertmanager.config.receivers` — email(SES SMTP) 또는 Slack webhook.
     SNS 직결은 지원하지 않으므로 SNS 경유가 필요하면 SNS→Lambda→webhook.
   - `prometheus.prometheusSpec.retention: 7d` + `storageSpec`(gp3 PVC 10Gi+)
     — 현 `emptyDir`+24h 는 재시작 시 소실.
2. `serverFiles.alerts`(또는 PrometheusRule) 시작 세트:
   - `KubePodCrashLooping`, container restart 급증, deployment replica 불일치
   - `gateway_bedrock_executor_queue_depth > 0` 지속 (큐 포화 → 스케일 한계)
   - budget/rate-limit 실패율, gateway-proxy 5xx 비율 (export 된 메트릭명 확인 후)
3. `deployment/observability/install.sh` 재실행 또는 `helm upgrade`.
4. 검증: 크래시 테스트 파드 → alertmanager UI + 실제 채널 도착 확인.
   silence 규칙으로 유지보수 창구 묵음 처리.

### 8-C. 앱 에러율 알람 (`log_shipping` 전제)

`log_shipping: true` 가 먼저 적용돼 있어야 합니다 — 로그가 CloudWatch 에
와야 metric filter 가 잡을 수 있습니다.

```bash
# ERROR 라인 카운트 → 메트릭 → 알람 (필터 패턴은 로그 포맷에 맞춰 조정)
aws logs put-metric-filter --region <region> \
  --log-group-name /aws/eks/<cluster>/fargate-logs \
  --filter-name gateway-errors --filter-pattern "ERROR" \
  --metric-transformations metricName=GatewayErrorCount,metricNamespace=LLMGateway,metricValue=1
aws cloudwatch put-metric-alarm --region <region> \
  --alarm-name <project>-<env>-app-error-rate \
  --namespace LLMGateway --metric-name GatewayErrorCount \
  --statistic Sum --period 300 --evaluation-periods 1 --threshold 10 \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "$TOPIC" --ok-actions "$TOPIC"
```

## 9. 끄기 / 롤백

- **알림만 끄기**: `features.alerting.enabled=false` + env 에서
  `terraform apply -var='enable_alerting=false'` — topic·alarm·구독이 모두
  삭제됩니다(구독 재생성 시 confirm 재필요).
- **로그 라우팅 끄기**: `log_shipping: false` 후 `./deploy apply` —
  helm 이 `aws-observability` namespace+ConfigMap 을 제거. 이미 수집된 로그는
  그룹 삭제 전까지 남습니다.
- **긴급 조용히**: SNS subscription 을 콘솔에서 삭제 — topic·alarm 상태는
  유지되고 메일만 멈춥니다(알람 이력은 콘솔에서 계속 조회 가능).
