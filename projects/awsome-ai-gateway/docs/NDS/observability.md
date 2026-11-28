# Observability & Alerting — NDS 관측·알림 가이드

이 문서는 LLM Gateway 의 관측 계층 전체와, NDS 방식(`gateway.yaml` +
`./deploy`)으로 배포되는 알림·로그 기능을 다룹니다.

```
메트릭:   pod → otel-collector → Prometheus → Grafana / prometheus-adapter → HPA
알림:     Aurora·ElastiCache·RDS events → CloudWatch alarms → SNS topic → email
          앱 메트릭 → PrometheusRule → Alertmanager → Slack (env 별 Secret 이 있을 때, §10)
          비용 대조·실패 기록·단가 누락 → notification-worker → Slack (§11)
로그:     AWS managed 서비스 → CloudWatch Logs (이미 동작)
          컨테이너 stdout/stderr → kubectl logs 만 (log_shipping 켜면 CloudWatch)
```

> 알림 목적에 맞는 설정을 선택합니다. AWS 인프라 알림은 `features.alerting`,
> EKS Prometheus 알림은 `observability.alerting`, 비용 정확성 알림은
> `features.business_alerts`에서 켭니다. 세 경로의 사용 설정은 각각 기본값이 꺼져 있습니다.

---

## 1. 두 개의 관측 계층

| 계층 | 구성 | 소유 | 알림 가능? |
|---|---|---|---|
| **In-cluster** | kube-prometheus-stack (Prometheus+Grafana+Alertmanager), otel-collector, prometheus-adapter | `deployment/observability/install.sh` — **수동 설치**, managed overlay 아님 | ✅ Slack — env 별 Secret 이 있을 때만 전달(§10) |
| **AWS managed** | CloudWatch alarms + SNS + Fargate 로그 라우터 | `module.alerting` — `features.alerting` 선언 시 **managed overlay** | ✅ 이 문서의 기능 |

In-cluster 스택은 부하테스트 관측용으로 만들어진 상태입니다:
Prometheus 기본 보관 기간은 **24h**, 스토리지는 `emptyDir`입니다.
게이트웨이·비용 PrometheusRule은 기본 설치합니다. Alertmanager는
`observability.alerting.enabled: true`에서 파드 1개를 설치하며, Slack 전달은 §10을 따릅니다. HPA 의
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
- **Alertmanager / Slack 알림**: §10 — 이 스택은 managed overlay 소유가
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
- [ ] env 별 Slack Secret 생성 → kube-prometheus-stack `install.sh` 재실행 → 테스트 알림 도착 확인 (§10)
- [ ] (선택) persistent Prometheus 스토리지 — 현재 emptyDir+24h (§8-B)
- [ ] ALB 5xx/UnHealthyHost 알람 — `deploy apply` 가 ingress ALB 를 발견해
      자동 생성 (첫 배포 직후엔 다음 apply 에서 수렴, §8-A)
- [ ] (선택) 앱 에러율 알람 — `log_shipping` 켠 뒤 metric filter 경로 (§8-C)

## 8. 보류 작업 절차 (follow-up)

기본 모듈 범위 밖인 세 가지 — 필요 시 아래 순서대로 붙입니다.

### 8-A. ALB 알람 — apply 시 자동 발견 (eks)

ALB·TargetGroup 은 AWS Load Balancer Controller 가 배포 시 생성하고
dimension(ARN suffix)을 그때 부여합니다 — `deploy apply` 는 이를 라이브
발견해 자동으로 알람을 채웁니다:

1. apply 시점에 `kubectl get ingress -n <ns> -o json` 으로 우리 release
   의 ingress(`app.kubernetes.io/instance` 라벨 일치)만 ALB hostname 수집 —
   ensure_infra 가 helm 보다 먼저 실행되므로 "이전 배포의 ingress" 기준.
   최초 배포는 항상 미발견 → 다음 apply 에서 수렴
2. `aws elbv2 describe-load-balancers` 에서 hostname 일치 + `application`
   타입만 매칭 → LB dimension `app/<name>/<id>`
3. LB ARN 각각 `describe-target-groups` 로 TG dimension
   `targetgroup/<name>/<id>` 수집 (LB 당 1회 호출 — 실패 시 전체 발견
   실패로 처리해 부분 dims 주입 방지)
4. `gen/<env>/eks/alerts-dims.tfvars` 생성 → `module.alerting` 의
   `alb_lb_dimensions` / `alb_target_groups` 변수로 주입
5. 알람 2종 생성: ALB별 `HTTPCode_Target_5XX_Count ≥ 1`(5m×2),
   TG별 `UnHealthyHostCount > 0`(1m×2) — actions 은 기존 alerting topic

**수렴 계약**:

- **첫 배포** (발견 파일 없음 + 발견 실패) — `⚠ alerting: ALB dimension 을
  발견하지 못했습니다 — ... deploy apply 를 한 번 더 실행해야 알람이
  만들어집니다` 를 출력하고 진행. ingress ALB 가 뜬 뒤 apply 한 번 더.
- **발견 실패 + 이전 발견 파일 존재** — 이전 `alerts-dims.tfvars` 를
  **stale 경고와 함께 재사용**합니다. 안 하면 일시적 kubectl/AWS 오류로
  dims 가 [] 로 평가돼 기존 알람이 destroy-plan 에 잡힙니다. stale 상태는
  다음 apply 의 발견 성공으로 자동 정상화됩니다 (doctor 는 같은 파일을
  재사용해 stale 을 감지하지 못합니다 — apply 경고가 유일한 신호).
- **발견 실패 + 파일 부재** (신규 머신/CI, gen 삭제) — var-file 없이
  진행해 기존 알람이 있으면 destroy-plan 이 뜨는데, **이번 실행에 라이브
  발견이 성공한 경우에만** ALB 알람 delete 를 가드가 허용하므로 차단됩니다.
  drift 수렴 경로는 경고 후 terraform 적용만 건너뛰고 helm 은 계속 진행,
  필수 인프라 누락 경로는 중단됩니다 — kubectl/AWS 접근을 복구하고
  재실행하세요. 차단 시 같은 plan 의 **정상 변경도 함께 지연**됩니다
  (저장된 단일 tfplan 은 부분 적용 불가). `deploy bootstrap` 재실행도
  같은 상황이면 사전 차단되니 먼저 `deploy apply` 로 발견을 갱신해야
  합니다 — 단 수동 경로(env tfvars 고정)는 dims 가 있으므로 차단되지
  않습니다.
- **ALB 교체 (id 변경)** — 신 dims 발견 → 신 알람 create + 구 알람 delete
  가 plan 에 뜨고, 발견 성공 run 이므로 가드가 허용합니다 (dimension
  교체가 정상 수명주기).
- `deploy doctor` 는 drift 검사에 같은 발견 파일을 읽기 전용으로 재사용 —
  없으면 존재하는 ALB 알람이 destroy drift 로 오진됩니다.

주입 var-file 은 apply 마다 재생성되므로 ALB 교체(id 변경)도
자동 추적되고, env tfvars 에 사람이 둔 같은 키는 gen 파일이 덮습니다.

**수동 경로 (ecs/외부 ALB)**: eks 가 아니거나 발견이 불가하면 env tfvars 에
직접 고정합니다 — `aws elbv2 describe-load-balancers`/`describe-target-groups`
로 dimension 을 실측해 `alb_lb_dimensions = ["app/<name>/<id>"]`,
`alb_target_groups = [{lb=..., tg="targetgroup/<name>/<id>"}]`.

### 8-B. Alertmanager + PrometheusRule (in-cluster 알림)

구현됨 — §10. 남은 follow-up:

- `prometheus.prometheusSpec.retention: 7d` + `storageSpec`(gp3 PVC 10Gi+) — 현 `emptyDir`+24h 는
  재시작 시 소실되고, 24h 보다 긴 창(`[25h]`)을 쓰는 대조 규칙은 재시작 직후 하루 동안 짧은 창으로 동작합니다.
  Fargate 에는 EBS CSI 가 없으므로 EC2 노드그룹 또는 AMP(Amazon Managed Prometheus)가 필요합니다 — 인프라 변경.
- prod Alertmanager HA(2+ replica, gossip mesh).
- Slack 외 수신자 — 예시만 [alerting-receivers.md](alerting-receivers.md).

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

## 10. In-cluster 알림 — Alertmanager + Slack

> **켜고 끄는 곳: `gateway.yaml` `observability.alerting`** (기본 꺼짐). `features.alerting`(terraform SNS/CloudWatch)과는
> 다른 스위치다. `./deploy render` 가 `deployment/gen/<env>/eks/observability.env`(`ALERTING_ENABLED`,
> `ALERT_SLACK_SECRET_NAME`)를 쓰고 `kube-prometheus-stack/install.sh`(install-eks.sh `ensure_observability`)가 읽는다.
>
> ```yaml
> observability:
>   alerting:
>     enabled: true                 # Alertmanager 1 파드 + values-alerting.yaml
>     slack_secret_name: ""         # 비우면 alertmanager-slack-<env>
> ```
>
> | 상태 | 설치되는 것 |
> |---|---|
> | 키 없음 / `false` | 알림 도입 전과 같은 릴리즈 + 게이트웨이 PrometheusRule(`kube-prometheus-stack-llm-gateway-alerts`, Prometheus 에서만 보임). Alertmanager·Secret·Service·ServiceMonitor 없음, EKS 잡음 규칙/모니터 그대로 |
> | `true`, Secret 없음 | Alertmanager 1 파드, 모든 알림 `"null"`(전달 꺼짐), EKS 잡음 규칙·컨트롤 플레인 모니터 끔, `env` 외부 라벨 |
> | `true`, Secret 있음 | 위 + Slack 전달 |
>
> render 비교(nds/dev 대비, `helm template` kps 60.1.0): 꺼짐은 PrometheusRule 1개(List 로 감싼) 추가 외 동일, 켜짐은
> Alertmanager 관련 6개 추가·컨트롤 플레인 ConfigMap/Service/ServiceMonitor/규칙 제거·Prometheus spec 변경.
> 반영: gateway.yaml 수정 → `./deploy render` → observability install 재실행(Secret 을 나중에 만들었을 때도 재실행).

```
앱(OTel) → otel-collector → Prometheus ─ PrometheusRule kube-prometheus-stack-llm-gateway-alerts
                                         └→ Alertmanager (kps-alertmanager, 1 파드) → Slack (env 별 webhook)
```

| 파일 (`deployment/observability/kube-prometheus-stack/`) | 내용 |
|---|---|
| `values.yaml` | 기본 Prometheus 보관 기간 24h와 `emptyDir` 스토리지 |
| `values-alerting.yaml` | Alertmanager 1 replica, 자체 알림 상태 보관 기간 120h, `emptyDir` 스토리지, 기본 `"null"` 라우트 및 EKS 잡음 규칙 제어 |
| `alerting-rules.yaml` | 게이트웨이/비용 알림 규칙 (항상 적용) |
| `alertmanager-slack.yaml` | Slack 수신자 + route (Secret 이 있을 때만 `install.sh` 가 적용) |
| `install.sh` | env 판별 → Secret 확인 → 오버레이·마운트 결정, Prometheus `externalLabels.env` 설정 |
| `check-alerting.sh`, `tests/` | 오프라인 검증 — `promtool check/test rules`, `amtool check-config`, 라우팅 확인 |

### Slack 켜기 (env 마다 1회, 사용자 작업)

Slack 앱의 **Incoming Webhook** 을 env 별 채널로 하나씩 만듭니다(dev 와 prod 는 다른 채널).
URL 은 git·values·`--set` 어디에도 넣지 않습니다 — k8s Secret 에만 둡니다.

```bash
# 대상 클러스터 context 를 먼저 확인 (dev 와 prod 를 섞지 않게)
kubectl config current-context
ENV=dev    # prod 클러스터에서는 prod
read -rs SLACK_WEBHOOK_URL   # https://hooks.slack.com/services/... 붙여넣기 (화면·기록에 안 남음)
kubectl -n observability create secret generic "alertmanager-slack-${ENV}" \
  --from-literal=api_url="$SLACK_WEBHOOK_URL"
unset SLACK_WEBHOOK_URL

# 적용 — Secret 이 있으면 install.sh 가 Slack 오버레이를 켠다
ALERT_ENV="$ENV" bash deployment/observability/kube-prometheus-stack/install.sh
```

- env 판별: `ALERT_ENV` → 없으면 현재 context 의 클러스터 이름 끝(`llm-gateway-dev` → `dev`).
  `install-eks.sh` 의 `ensure_observability` 는 `ALERT_ENV=<dev|prod>` 를 넘깁니다.
- Secret 이 없거나 `api_url` 이 비면: `⚠ Alertmanager: Secret ... 없음 → 알림 전달 꺼짐` 경고 후 그대로
  설치합니다. Alertmanager 는 떠서 UI 에서 알림을 보여 주지만 모든 알림이 `"null"` 로 갑니다.
- URL 교체: 같은 이름으로 Secret 을 다시 만듭니다(`kubectl -n observability delete secret alertmanager-slack-<env>`
  후 위 create). 마운트된 파일은 kubelet 이 ~1분 안에 갱신하고 Alertmanager 는 보낼 때마다 파일을 읽으므로
  재설치가 필요 없습니다(마운트가 `optional` 이라 잠시 비어도 파드는 그대로).
- 끄기: Secret 삭제 후 `install.sh` 재실행(마운트·Slack route 제거). 재실행 전에는 Slack 전송 실패가
  `alertmanager_notifications_failed_total` 과 차트 기본 알림 `AlertmanagerFailedToSendAlerts` 로 보입니다.

### 규칙 (`alerting-rules.yaml`)

| 알림 | 조건 | for / 등급 |
|---|---|---|
| CostReconciliationMismatch | `sum by (check) (increase(reconciliation_mismatch_total[25h])) > 0` | — / warning |
| CostReconciliationStale | 마지막 성공 대조가 26h 넘게 전, 또는 `reconciliation_last_success_timestamp` 30m 부재 | 15m / critical |
| CostStreamLagHigh | `max(cost_recorder_stream_lag) > 1000` / `> 20000` | 10m / warning, critical |
| CostWorkerMetricsAbsent | `absent_over_time(cost_recorder_stream_lag[10m])` | 5m / critical — 워커의 다른 알림을 억제 |
| CostDeadLetterGrowth | `cost_recorder_dead_letter_rows_total` 30m 증가 또는 새 시리즈 | — / warning |
| CostDeadLetterDropped | `cost_recorder_dead_letter_dropped_total` 30m 증가 또는 새 시리즈 | — / critical |
| GatewayHigh5xxRatio | `gateway_request_total{status=~"5.."}` 비율 `> 5%` / `> 20%` (5분 ~15건 이상일 때) | 10m warning / 5m critical |
| GatewayHigh503Ratio | `status="503"` 비율 `> 5%` | 5m / warning |
| GatewayBudgetDegraded | `gateway_budget_degraded_total` — `grace_*`·`last_known_*` 증가 / `stale_policy`·`rebuild_skipped` 지속 | 2m critical / 15m warning |

메모:

- 앱 메트릭은 scrape 가 아니라 remote-write 로 오므로 staleness marker 가 없습니다. 부재는
  `absent_over_time` 으로 봅니다. OTel 카운터는 첫 증가 때 시리즈가 생기므로, dead-letter 규칙은
  "30m 전에 없던 시리즈" 항으로 재시작 뒤 첫 이벤트도 잡습니다(Prometheus 자체 재시작 후 30m 은 그 항을 끔).
- 대조 잡이 꺼진 환경(`RECONCILIATION_ENABLED=false`)에서는 CostReconciliationStale 이 계속 울립니다 —
  그 env 에서는 silence 를 걸거나 규칙을 지웁니다.
- 끈 차트 기본 규칙: `kubeControllerManager`·`kubeScheduler`·`kubeProxy`·`kubeEtcd` 컴포넌트(EKS 가 노출하지
  않음, Fargate 에 kube-proxy 없음)와 `KubeVersionMismatch`(Fargate kubelet 버전 차이). `Watchdog`·
  `InfoInhibitor` 와 `severity=info` 알림은 `"null"` 로 보냅니다(UI 에서만 보임).
- Slack 메시지 제목: `[FIRING:n] [<env>] <alertname>` — env 는 Prometheus `externalLabels.env`.

### 확인

```bash
# 오프라인 (렌더 + promtool + amtool) — 배포 전
bash deployment/observability/kube-prometheus-stack/check-alerting.sh

# 클러스터
kubectl -n observability get pods -l app.kubernetes.io/name=alertmanager
kubectl -n observability port-forward svc/kps-alertmanager 9093:9093   # http://localhost:9093
kubectl -n observability port-forward svc/kps-prometheus 9090:9090     # Alerts 탭 / Status → Rules

# 테스트 알림 1건 → Slack 채널 도착 확인 (resolve_timeout 5m 뒤 자동 해소)
kubectl -n observability exec alertmanager-kps-alertmanager-0 -c alertmanager -- \
  amtool alert add GatewayAlertingTest severity=warning service=test env=<env> \
  --annotation='summary="alerting test - ignore"' --alertmanager.url=http://localhost:9093
```

유지보수 창 묵음: Alertmanager UI → Silences, 또는
`amtool silence add 'alertname=~"Gateway.*"' --duration=2h --comment=maintenance --alertmanager.url=http://localhost:9093`.
silence 는 emptyDir 이라 Alertmanager 파드 재시작 시 사라집니다.

다른 수신자(Teams, webhook, PagerDuty, Opsgenie, Discord, email/SES, SNS) 예시: [alerting-receivers.md](alerting-receivers.md).

## 11. 비용 이상 알림 — notification-worker

`features.business_alerts`는 세 배포 대상에서 비용 원장 대조 결과, dead-letter 증가,
단가 누락을 주기적으로 확인합니다. 알림을 보낼 점검만 선택합니다.

```yaml
features:
  business_alerts:
    enabled: true
    reconciliation_mismatch: true
    dead_letter_growth: true
    pricing_missing: true
    slack: true
```

| 배포 대상 | Slack 웹훅 전달 방식 |
|---|---|
| EKS | notification-worker의 네임스페이스에서 Secret `alertmanager-slack-<env>`의 `api_url`을 읽습니다. Alertmanager를 별도 네임스페이스에 설치한 경우 이쪽 Secret도 확인합니다. |
| ECS | 태스크 시작 전에 앱의 Secrets Manager 값에 `slack_webhook_url` 키가 있어야 합니다. 누락되면 태스크가 시작하지 못합니다. |
| Compose | 배포 호스트의 `.env`에 있는 `SLACK_WEBHOOK_URL`을 사용합니다. |

`./deploy apply --plan`으로 notification-worker 설정을 확인한 뒤 적용합니다. 워커가
시작됐는지, 켠 점검이 실행되는지, 이상이 발생한 경우 대상 Slack 채널에 전달되는지
확인합니다. 비용 대조 불일치와 미처리 건수는 §10에서 사용하는 Prometheus 알림과
별도로 전달될 수 있으므로 두 경로를 같은 채널에 켤 때 확인합니다.

현재 비용 대조는 내부 원장과 집계의 정합성을 확인합니다. AWS 청구와의 대조는
[R7 설계](cost-reconciliation-r7.md)에 기록되어 있습니다.
