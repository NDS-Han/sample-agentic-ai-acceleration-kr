# Monitor the gateway and configure alerts

[한국어](observability.md) · **English** · [NDS documentation](README.en.md)

| Signal | Configuration | Delivery |
|---|---|---|
| AWS managed infrastructure health | `features.alerting` | CloudWatch alarms and SNS. Recipients confirm subscriptions. |
| EKS gateway and worker metrics | `observability.alerting` and the observability installer | PrometheusRule → Alertmanager → Slack when a receiver is configured |
| Cost accuracy | `features.business_alerts` | notification-worker checks and Slack |
| Container logs | `features.alerting.log_shipping` on EKS | Fargate log routing to CloudWatch |

## AWS alerts and logs

Enable the intended infrastructure alerting keys in `gateway.yaml`. Preview the plan, apply it, and check the alarms, target resource dimensions and SNS subscriptions. Check CloudWatch retention and access separately from in-cluster metrics.

## EKS metrics and Alertmanager

Use the repository observability installer with the intended kube context and rendered values. Prometheus retains 24 hours by default with ephemeral storage. The gateway rules are installed with the Prometheus stack. Enable `observability.alerting.enabled` to install Alertmanager and configure an environment-specific Slack secret. Check the rules and route, then verify an authorized test notification.

## Cost accuracy notifications

```yaml
features:
  business_alerts:
    enabled: true
    reconciliation_mismatch: true
    dead_letter_growth: true
    pricing_missing: true
    slack: true
```

EKS uses the webhook secret in the notification-worker namespace. ECS requires the app Secrets Manager key `slack_webhook_url` before starting a task with Slack. Compose uses its local `SLACK_WEBHOOK_URL` setting. Confirm that the worker starts, checks run, and an expected alert reaches the chosen channel. Operational ledger comparisons are separate from the [R7 external billing proposal](cost-reconciliation-r7.md).
