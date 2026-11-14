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
- Aurora 인스턴스별 알람 × 2: `CPUUtilization > 85%`(2×5분),
  `FreeableMemory < 256MiB`(2×5분)
- ElastiCache 멤버별 알람 × 3: `EngineCPUUtilization > 75%`,
  `DatabaseMemoryUsagePercentage > 85%`, `CurrConnections > 50,000`
  (member id 는 data source 로 해석 — cluster/non-cluster mode 모두 대응)

### 안 만드는 것 (의도적)

- **ALB 알람** — ALB dimension(LoadBalancer/TargetGroup suffix)이 AWS
  Load Balancer Controller 가 생성 시점에 부여해 plan 시점에 알 수 없습니다.
  ALB 헬스는 Prometheus 경로(후술) 또는 ALB access log 로 보완하세요.
- **파드/앱 레벨 알람** — CrashLoop·restarts·executor 포화는 PrometheusRule
  + Alertmanager 계층의 몫입니다.
- **빈 로그 그룹** — `log_shipping` 이 라우터 자체를 만들 때만 로그가 실제로
  흐릅니다.

### 배포

```bash
./deploy validate          # 스키마+검증
./deploy apply --plan      # module.alerting + helm values 미리보기
./deploy apply             # -target=module.alerting 으로 좁혀 적용
./deploy doctor            # alerting(SNS topic/alarms) output 확인
```

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
- **Alertmanager 를 켜려면**: `deployment/observability/kube-prometheus-stack/
  values.yaml` 의 `alertmanager.enabled` + `serverFiles.alerts`(PrometheusRule)
  + receiver(SNS webhook·Slack·email) 설정, 그리고 Prometheus PVC
  (현 `emptyDir`+24h 는 운영급이 아님). 이 스택은 managed overlay 소유가
  아니라 수동 설치 자산 — 변경은 `deployment/observability/` 에서 합니다.

## 6. 비용 (ap-northeast-2 기준 대략)

| 항목 | 단가 | dev 추정 | prod 추정 |
|---|---|---|---|
| 알람 | $0.10/개·월 | ~10개 → ~$1 | 멤버 수에 비례 → ~$2–3 |
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
      파드/앱 레벨 알림이 필요하면
- [ ] (선택) ALB 5xx/UnHealthyHost 알람 — ALB dimension 을 배포 후 실측으로
      얻어 env tfvars 확장 모듈로 추가

## 8. 끄기 / 롤백

- **알림만 끄기**: `features.alerting.enabled=false` + env 에서
  `terraform apply -var='enable_alerting=false'` — topic·alarm·구독이 모두
  삭제됩니다(구독 재생성 시 confirm 재필요).
- **로그 라우팅 끄기**: `log_shipping: false` 후 `./deploy apply` —
  helm 이 `aws-observability` namespace+ConfigMap 을 제거. 이미 수집된 로그는
  그룹 삭제 전까지 남습니다.
- **긴급 조용히**: SNS subscription 을 콘솔에서 삭제 — topic·alarm 상태는
  유지되고 메일만 멈춥니다(알람 이력은 콘솔에서 계속 조회 가능).
