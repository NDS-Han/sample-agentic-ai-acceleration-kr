# Alertmanager 수신자 예시 — Slack 외 (문서 전용, 미구현)

**한국어** · [English](alerting-receivers.en.md)

> **이 저장소에서 제공하는 Alertmanager 수신자 오버레이는 Slack입니다.** `deployment/observability/kube-prometheus-stack/alertmanager-slack.yaml`
> + `install.sh` 의 Secret 감지([observability.md](observability.md) §10).
> 이 문서의 수신자는 **예시**입니다. 저장소에 오버레이 파일도, `install.sh` 연결도 없습니다.
> 쓰려면 아래 패턴대로 오버레이를 만들고 `install.sh` 의 Slack 블록을 복사해 연결하세요.

notification-worker의 비용 이상 Slack 알림과 CloudWatch·SNS 알림은 [관측 및 알림 가이드](observability.md)에서 확인합니다.

대상: kube-prometheus-stack 60.1.0 에 포함된 **Alertmanager v0.27.0**. 필드 이름은
[v0.27 configuration](https://github.com/prometheus/alertmanager/blob/v0.27.0/docs/configuration.md) 기준입니다.

## 공통 패턴 (Slack 구현과 같음)

1. 자격 증명(URL·키·비밀번호)은 **env 별 k8s Secret** 에만 둡니다. git·values·`--set` 에 넣지 않습니다.
   이름 규칙: `alertmanager-<receiver>-<env>` (ns `observability`). dev 와 prod 는 다른 채널/키를 씁니다.
2. Secret 을 Alertmanager 파드에 파일로 마운트하고(`alertmanagerSpec.volumes` + `volumeMounts`,
   `secret.optional: true`), 설정에서는 `*_file` 필드로 읽습니다. 그래서 차트가 만드는
   `alertmanager-kps-alertmanager` Secret(설정 원문)에도 자격 증명이 들어가지 않습니다.
3. 오버레이는 `route` 와 `receivers` 를 **통째로** 다시 씁니다 — helm 은 리스트를 병합하지 않습니다.
   `Watchdog`·`InfoInhibitor`·`severity=~info|none` → `"null"` 경로는 그대로 둡니다.
4. `install.sh` 는 Secret 이 있을 때만 오버레이와 `--set ...volumes[N]...` 을 넘깁니다. 수신자를 둘 이상
   쓰면 volumes 인덱스(`[0]`, `[1]`, …)와 마운트 경로를 수신자마다 다르게 둡니다.
5. 배포 전 `./check-alerting.sh` 처럼 `amtool check-config` 로 렌더 결과를 검사합니다.
   webhook·PagerDuty·Opsgenie·email·SNS 예시는 v0.27.0 `amtool check-config` 를 통과했습니다
   (Teams·Discord 는 v0.28.1 에서만 통과 — 각 절 참고).

```bash
# Secret 생성 공통 형태 — 값은 셸 기록에 남지 않게 read -s 로 받는다
read -rs SECRET_VALUE
kubectl -n observability create secret generic alertmanager-<receiver>-<env> \
  --from-literal=<key>="$SECRET_VALUE"
unset SECRET_VALUE
```

아래 예시의 `receivers` 조각은 Slack 오버레이의 `receivers` 리스트에 추가하거나 `slack` 대신 넣습니다.
여러 수신자로 나누려면 `route.routes` 에 `matchers` 로 분기합니다(예: `severity = "critical"` → 호출기).

## AWS 인프라 필요 여부

| 수신자 | 필요한 것 | AWS 인프라 |
|---|---|---|
| Microsoft Teams | Teams 웹훅 URL (+ Alertmanager v0.28+, 아래 참고) | 없음 |
| Discord | 채널 웹훅 URL (+ Alertmanager v0.28+, 아래 참고) | 없음 |
| Generic webhook | HTTPS 엔드포인트(+ 선택 Bearer 토큰) | 없음 |
| PagerDuty | Events API v2 integration(routing) key | 없음 |
| Opsgenie | API integration key | 없음 |
| Email (SES SMTP) | SES 검증 도메인/주소 + SMTP 자격 증명(IAM 사용자) | **있음** (IAM, SES) |
| Email (사내 SMTP 릴레이) | 릴레이 주소 + 계정 | 없음 |
| Amazon SNS | SNS topic + Alertmanager SA 용 IRSA 역할(`sns:Publish`) | **있음** (IAM 역할, topic) |

모두 observability Fargate 파드에서 나가는 HTTPS(또는 SMTP 587)가 필요합니다. HTTPS 경로는 이미
있습니다(Grafana → CloudWatch, gateway → Bedrock). SMTP 587 아웃바운드는 보안 그룹/NAT 정책을 확인하세요.
AWS 인프라가 필요한 두 수신자는 terraform 변경이라 prod 사이징·승인 항목입니다(dev 에는 만들지 않음).

---

## Microsoft Teams (`msteams_configs`)

```yaml
alertmanager:
  alertmanagerSpec:
    volumeMounts:
      - {name: teams-webhook, mountPath: /etc/alertmanager/teams, readOnly: true}
  config:
    receivers:
      - name: teams
        msteams_configs:
          - webhook_url_file: /etc/alertmanager/teams/webhook_url
            send_resolved: true
```

- Secret: `alertmanager-teams-<env>`, key `webhook_url`.
- **현재 차트(Alertmanager v0.27.0)에서는 이대로 쓸 수 없습니다.** v0.27.0 의 설정 검증은
  `webhook_url_file` 을 보지 않고 `no msteams webhook URL provided` 로 거부합니다(`amtool check-config`
  실측; v0.28 에서 고쳐짐). URL 을 설정에 직접 넣으면 values/git 에 비밀이 들어가므로 이 저장소 방식과
  맞지 않습니다. 쓰려면 Alertmanager v0.28+ 를 포함한 kube-prometheus-stack 으로 올린 뒤 위 예시를 씁니다.
- v0.27 의 `msteams_configs` 는 Office 365 **커넥터** 형식(MessageCard)을 보냅니다. Microsoft 는
  커넥터를 Power Automate **Workflows** 로 옮기는 중이고, Workflows 웹훅은 Adaptive Card 를 요구합니다.
  Workflows URL 은 `msteamsv2_configs`(v0.28+, 같은 `webhook_url_file` 필드)로 보냅니다.

## Discord (`discord_configs`)

```yaml
  config:
    receivers:
      - name: discord
        discord_configs:
          - webhook_url_file: /etc/alertmanager/discord/webhook_url
            send_resolved: true
```

- Secret: `alertmanager-discord-<env>`, key `webhook_url`. 마운트는 Teams 와 같은 형태(`/etc/alertmanager/discord`).
- Teams 와 같은 제약: v0.27.0 은 `webhook_url_file` 을 `no discord webhook URL provided` 로 거부합니다.
  Alertmanager v0.28+ 가 필요합니다.

## Generic webhook (`webhook_configs`)

```yaml
  config:
    receivers:
      - name: webhook
        webhook_configs:
          - url_file: /etc/alertmanager/webhook/url
            send_resolved: true
            max_alerts: 50
            http_config:
              authorization:
                type: Bearer
                credentials_file: /etc/alertmanager/webhook/token   # 선택
```

- Secret: `alertmanager-webhook-<env>`, keys `url`, (선택) `token`.
- 본문은 Alertmanager webhook JSON(version 4). 받는 쪽은 `groupKey` 로 중복을 걸러야 합니다.
- notification-worker 에는 알림 수신 라우트가 없습니다. 그쪽으로 보내려면 코드 변경이 먼저입니다.

## PagerDuty (`pagerduty_configs`)

```yaml
  config:
    route:
      routes:
        # 기존 null 경로들 뒤, slack 경로 앞에
        - receiver: pagerduty
          matchers: ['severity = "critical"']
          continue: true          # Slack 에도 같이 보낸다
    receivers:
      - name: pagerduty
        pagerduty_configs:
          - routing_key_file: /etc/alertmanager/pagerduty/routing_key
            send_resolved: true
            severity: '{{ if eq .CommonLabels.severity "critical" }}critical{{ else }}warning{{ end }}'
```

- Secret: `alertmanager-pagerduty-<env>`, key `routing_key` (Events API v2 integration key).
- dev 는 보통 호출기를 울리지 않습니다 — prod Secret 만 만들면 dev 는 자동으로 꺼진 상태로 남습니다.

## Opsgenie (`opsgenie_configs`)

```yaml
  config:
    receivers:
      - name: opsgenie
        opsgenie_configs:
          - api_key_file: /etc/alertmanager/opsgenie/api_key
            # EU 계정: api_url: https://api.eu.opsgenie.com/
            priority: '{{ if eq .CommonLabels.severity "critical" }}P1{{ else }}P3{{ end }}'
            tags: 'llm-gateway,{{ .CommonLabels.env }}'
            send_resolved: true
```

- Secret: `alertmanager-opsgenie-<env>`, key `api_key`.

## Email (`email_configs`) — SES SMTP 또는 사내 릴레이

```yaml
  config:
    receivers:
      - name: email
        email_configs:
          - to: ops@example.com
            from: alerts@example.com              # SES 에서 검증된 주소/도메인
            smarthost: email-smtp.<region>.amazonaws.com:587
            auth_username: <SMTP 사용자 이름 — 비밀 아님, 값 파일에 둬도 됨>
            auth_password_file: /etc/alertmanager/email/smtp_password
            require_tls: true
            send_resolved: true
```

- Secret: `alertmanager-email-<env>`, key `smtp_password`.
- SES: SMTP 자격 증명은 `ses:SendRawEmail` 권한의 IAM 사용자에서 파생합니다(IAM 사용자 + SES 신원 =
  terraform 변경). 샌드박스 계정은 검증된 수신 주소에만 보냅니다.
- 사내 릴레이: `smarthost` 와 계정만 바꾸면 되고 AWS 인프라는 필요 없습니다.

## Amazon SNS (`sns_configs`)

```yaml
alertmanager:
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: <Alertmanager IRSA role ARN — install.sh 가 --set 으로 주입>
  config:
    receivers:
      - name: sns
        sns_configs:
          - topic_arn: arn:aws:sns:<region>:<account>:<project>-<env>-alerts
            sigv4:
              region: <region>
            subject: '[{{ .CommonLabels.env }}] {{ .CommonLabels.alertname }}'
            send_resolved: true
```

- Secret 은 필요 없습니다(IRSA 웹 ID 토큰으로 서명). 대신 IAM 역할(`sns:Publish` on topic, trust =
  `system:serviceaccount:observability:kps-alertmanager`)과 topic 이 필요합니다.
  topic 은 `features.alerting`(`module.alerting`)이 만드는 `<project>-<env>-alerts` 를 재사용할 수
  있습니다 — 그러면 CloudWatch 알람과 같은 구독자(email confirm 필요)에게 갑니다.
- 역할 ARN 은 비밀이 아니지만 env 마다 다르므로 `GRAFANA_ROLE_ARN` 처럼 install.sh 에서 주입합니다.
- 이 역할·topic 생성은 terraform 변경입니다(dev 에는 만들지 않음, prod 승인 항목).
