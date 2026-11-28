# gateway.yaml reference

[한국어](gateway-yaml.md) · **English** · [NDS documentation](README.en.md)

Declare the deployment in `deployment/gateway.yaml`. Use `./deploy validate` for schema checks and `./deploy apply --plan` before applying changes. Review the [detailed field reference](gateway-yaml.md) for target-specific options and precedence.

| Block | What belongs here |
|---|---|
| `aws` | Deployment region and role allowlists |
| `deploy` | Backend, environment Terraform location, size tier, capacity band, state backend, sizing overrides |
| `network` | Exposure mode, allowed CIDRs, administrator ingress |
| `domain` | TLS mode, domain, zone and optional EKS hostnames |
| `features` | Notifications, body logging, BI Insight, web search, pricing and credential controls |
| `observability.alerting` | EKS Alertmanager gate and optional Slack secret name |
| `images` | Image registry and release tags |
| `oidc` | Issuer, authorization and token URLs, client and audience, claims and administrator rules |
| `infra` | Availability zones, VPC, Cognito, model IAM requirements and supported storage overrides |
| `branding` and `clients` | Console appearance and client model profile |

## Capacity

Set an ECS or EKS `deploy.capacity_tier` as a string. Baseline gateway capacity is:

| User band | Minimum or desired | Maximum |
|---|---:|---:|
| `"500"` | 1 | 3 |
| `"1000"` | 2 | 5 |
| `"3000"` | 3 | 6 |
| `"6000"` | 4 | 8 |
| `"6000+"` | 6 | 15 |

Explicit `deploy.sizing` values override the band, which overrides the size-tier defaults. Confirm the real workload with [Load testing](loadtest.en.md) and check Redis and DB headroom.

## Authentication and features

For SAML, declare `infra.cognito_saml` and verify that `oidc.groups_claim` matches the actual token. Configure required groups and administrator identities deliberately. A role computed from a verified email does not establish team membership. [Group mapping](ad-sync.en.md)

`features.alerting` supplies AWS infrastructure alarms. `observability.alerting` controls the EKS Alertmanager installation. `features.business_alerts` supplies cost checks through notification-worker. Each has its own configuration path. [Observability and alerts](observability.en.md)

For HTTPS, `domain.mode: route53-acm` is the normal path. Confirm both gateway and administrator addresses and recheck callback URLs after changes. Keep generated local YAML and secret values in the environment's approved configuration store.
