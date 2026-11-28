# Deployment and environment lifecycle

[한국어](deployment.md) · **English** · [Comparison index](README.en.md)

## From configuration to rollout

NDS adds `gateway.yaml` and `./deploy` around the existing Terraform, Helm and deployment assets. `init/configure` prepares input and `validate/render` checks the output. Use `bootstrap` for new environments, `apply --plan` and `apply` for changes, and `doctor` for checks. `doctor --capture` builds a draft from a running environment. Operators review unknowns, retained settings and ownership before adoption. `client-values` produces employee configuration. [Configuration schema](../gateway-yaml.en.md)

| Backend | Artifacts | Handover and operation |
|---|---|---|
| Compose | One host, Compose, env and Caddy | Verify off-host backup and volume removal scope |
| ECS | Terraform inputs and Fargate services | Verify per-service/task deployment, backend state, IAM and required secret fields |
| EKS | Helm values and managed Terraform overlay | Verify context and release. Record common and per-service image pins. Plan control plane/add-on changes separately |

EKS and ECS capacity bands give initial pod/task bounds. Compare real database and Redis headroom with the selected `deploy.sizing`. `upgrade eks` follows one minor hop at a time with add-on and workload checks. Roll back an application by reapplying compatible prior tags. [Update procedure](../update.en.md) · [EKS upgrade](../upgrade-eks-cluster.en.md)

## Cloud ownership, TLS and feature gates

- Route 53/ACM ties certificate validation, hosts and ingress to Cognito callback configuration. `9b59c781` changed the private DNS smoke check to use actual VPC CIDRs.
- ADFS metadata, mapping and provider details are declarative. Review live values against the declaration after `6d382cd4` using capture and plan.
- Review dependencies for each enabled feature. Web search can require AgentCore Gateway, BI Insight AgentCore Runtime and DB reader access, external pricing Lambda, body logs Firehose/S3 and alerting a backend-specific secret and destination.
- Record ownership of the managed overlay and the separately maintained VPC, EKS or database state. Verify the actual resource and data scope before teardown.

[Deploy CLI](../deploy/cli.py) · [Terraform modules](../../../deployment/terraform/modules) · [ADFS](../adfs-sso.en.md) · [Operations handover](../op-guide/README.md)
