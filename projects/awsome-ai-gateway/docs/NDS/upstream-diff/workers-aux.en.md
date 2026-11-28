# Workers, cost and observability

[한국어](workers-aux.md) · **English** · [Comparison index](README.en.md)

## Cost data flow

The proxy emits usage and cost events, and the worker consumes the stream, persists the data and updates rollups. `87ff960b` addresses precision, `11aad69c` adds durability, and `23c0c6c8` tunes reconciliation. The path includes request start and close records, final events, DB fallback and replay, and checks for abandoned requests. Recovery may produce estimated rows, which can affect spend comparisons. `93561142` covers historical web-search backfill.

V2 daily, monthly and hourly rollups and dirty marks track lag and recomputation. Default schedules are minute 05 each hour, daily at 00:10 and reconciliation at 01:00. Verify the actual flags, schedules and timezone. The internal ledger comparison is distinct from any external AWS CUR/CloudWatch billing reconciliation covered by the separate [R7 design](../cost-reconciliation-r7.md). [Cost worker](../../../cost-recorder-worker) · [API and schema](admin-api.en.md)

## Alert routes

| Route | Checks |
|---|---|
| AWS alarms | `features.alerting`, accepted CloudWatch/SNS subscriptions, Aurora, ElastiCache, ALB and optional log forwarding |
| Prometheus/Alertmanager | EKS observability installation, `observability.alerting.enabled`, Slack Secret in its namespace, and rules. Prometheus defaults to 24h/emptyDir; Alertmanager retains its own state for 120h |
| notification-worker | Budget and key events, deduplication and mail provider. Cost accuracy uses separate `features.business_alerts` and target-specific Slack configuration |

`31072728` wires cost-alert deployment across EKS, ECS and Compose. Check the worker namespace Secret on EKS, `slack_webhook_url` in the ECS app secret, and `SLACK_WEBHOOK_URL` on Compose. Identify which route produced a notification before diagnosing it. [Observability runbook](../observability.en.md)

## Supporting services

- **Pricing Lambda** provides an optional LiteLLM pricing path. Environments without it use AWS Price List or manual prices.
- **BI Insight** extends the existing chat agent with deployment-managed AgentCore Runtime and DB reader configuration for reports at `/chat`.
- **Mock and load tests** support streaming, concurrent logins, executor saturation and Redis/DB capacity checks in a controlled environment. Measure real provider latency and cost separately.

[Notification worker](../../../notification-worker) · [Load tests](../loadtest.en.md) · [Operations handover](../op-guide/README.md)
