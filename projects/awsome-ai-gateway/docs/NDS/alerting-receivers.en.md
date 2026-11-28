# Additional Alertmanager receiver examples

[한국어](alerting-receivers.md) · **English** · [NDS documentation](README.en.md)

The bundled EKS receiver overlay uses Slack. The original reference also includes examples for Teams, Discord, generic webhooks, PagerDuty, Opsgenie, email and SNS. Treat an additional receiver as a custom overlay and validate it against the deployed Alertmanager version.

Mount the receiver credential from an environment-specific Secret. Reference the mounted file from the receiver configuration. Merge the intended routing and receiver lists in the custom overlay because Helm replaces lists. Keep the desired Watchdog and info routing and check all routes using the installed version of `amtool check-config`.

Confirm the new receiver receives an authorized test alert. SNS or SES delivery can require additional infrastructure and account permissions. Review the detailed target examples in the [receiver reference](alerting-receivers.md) and the three notification paths in [Observability](observability.en.md).
