# NDS Gateway documentation

[한국어](README.md) · **English**

NDS Gateway connects Claude Code, Cowork, and Codex to a managed model gateway. Declare the environment in `deployment/gateway.yaml` and deploy with `./deploy`.

| What do you need to do? | Start here |
|---|---|
| Manage users, models, and policies in the console | [Administrator guide](guides/admin-guide.en.html) |
| Take over or deploy a gateway environment | [Operations and deployment handover](op-guide/README.md) |
| Connect Claude Code, Cowork, or Codex on your computer | [Employee quick start](client-setup.en.md) |
| Check infrastructure in the AWS console | [AWS infrastructure operations guide](aws-infrastructure-operations.en.md) |
| Investigate or recover an environment | [Operations runbook](operations.en.md) · [Observability and alerts](observability.en.md) · [Disaster recovery](dr-runbook.en.md) |

## Deployment engineers

| Task | Guide |
|---|---|
| Prepare the deployment host | [Prerequisites](install-prereq.en.md) |
| Choose a deployment target | [EKS](install-eks.en.md) · [ECS](install-ecs.en.md) · [Compose](install-compose.en.md) |
| Adopt, update, or roll back an environment | [Deployment changes](update.en.md) · [EKS and Aurora upgrades](upgrade-eks-cluster.en.md) |
| Connect DNS and an identity provider | [Domains and TLS](domain.en.md) · [ADFS setup](adfs-sso.en.md) · [Group mapping and provisioning](ad-sync.en.md) · [Managed Login branding](managed-login.en.md) |
| Check deployment keys and defaults | [gateway.yaml reference](gateway-yaml.en.md) |

## Gateway administrators

| Task | Guide |
|---|---|
| Manage the console | [Administrator guide and screenshots](guides/admin-guide.en.html) · [Console documentation](guides/README.en.md) |
| Take over a deployment | [Operations and deployment handover](op-guide/README.md) |
| Provision employees and route models | [Operations runbook](operations.en.md) |
| Monitor, recover, or validate capacity | [Observability and alerts](observability.en.md) · [Disaster recovery](dr-runbook.en.md) · [Load testing](loadtest.en.md) |
| Enable optional features or distribute clients | [BI Insight deployment](chat-agent.en.md) · [Client distribution](client-admin.en.md) |

## Employees and application developers

- [Employee quick start](client-setup.en.md) covers the operating system and app combinations.
- [Install or remove Cowork](cowork/README.en.md) covers macOS profiles and the Windows installer.
- [AICM integration](aicm/README.md) describes the dedicated API contract and authentication.

## Design and migration references

- Maintenance · [Upstream synchronization](upstream-sync.en.md) · [Deployment design](decisions.en.md)
- Technical reference · [Additional Alertmanager receivers](alerting-receivers.en.md) · [R7 external cost reconciliation design](cost-reconciliation-r7.md)
- Change comparison · [NDS Gateway changes since upstream e3a39757](upstream-diff/README.en.md)
- Historical references · [Legacy EKS migration](upgrade-eks-legacy.en.md) · [Review records](review/README.en.md)

## Deployment at a glance

| Target | Typical use | Provisioned stack |
|---|---|---|
| Compose | Evaluation and proof of concept | Single EC2 instance with Docker Compose |
| ECS | Small to standard deployments | Fargate tasks with the supporting managed infrastructure |
| EKS | New Kubernetes deployments and larger workloads | EKS infrastructure and the gateway Helm release |

Use `./deploy bootstrap` for a new managed environment, `./deploy doctor --capture` to adopt an existing environment, and `./deploy apply --plan` to review a change before applying it.
