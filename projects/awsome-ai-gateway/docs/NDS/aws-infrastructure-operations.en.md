# AWS infrastructure operations guide

[한국어](aws-infrastructure-operations.md) · **English** · [Operations and deployment handover](op-guide/README.md) · [NDS documentation](README.en.md)

Use this guide to take over the AWS infrastructure for NDS Gateway, check service health, triage incidents, and verify changes and recovery. Run the checks for each environment and its deployed resources. Use the [administrator guide](guides/admin-guide.en.html) for gateway users, models, and policies.

## 1. Verify the environment on day one

1. Sign in with the approved operator role. Compare the console account and Region with the environment inventory and deployment configuration. Check separate Bedrock invocation, DNS and billing accounts where applicable.
2. In **CloudWatch → Alarms** and **SNS → Subscriptions**, verify that required alarms cover the current resources and that an on-call operator receives a test notification.
3. In **RDS → Automated backups**, verify the latest restorable time and backup retention. Locate the restore drill record, key recovery owner, last known good images in ECR and Terraform state access.
4. Verify administrator and employee sign-in, a representative model request, application logs and usage recording. Record the actual invocation role and model for cross-account requests.
5. Record the time and timezone, owner, result and evidence for each check. Assign an owner and identify the affected environment for each outstanding item.

Copy this template for each environment in the approved handover store. Record secret references and access owners.

| Inventory item | Record |
|---|---|
| Environment and access | `<environment>` · `<aws-account-id>` · `<aws-region>` · `<operator-role>` |
| Compute and network | `<eks-or-ecs-cluster>` · `<namespace-or-ecs-service>` · `<vpc-and-subnets>` · `<gateway-and-admin-hosts>` |
| Backup and recovery | `<aurora-cluster-and-proxy-if-used>` · `<backup-retention>` · `<restore-test-record>` · `<secret-store-reference>` |
| Deployment and ownership | `<deployment-config-reference>` · `<terraform-backend-reference>` · `<last-known-good-image-digests>` · `<infrastructure-owner>` |
| Alerts and escalation | `<alarm-and-log-links>` · `<on-call-channel>` · `<aws-support-contact>` · `<last-verification-record>` |
| Models and cost | `<bedrock-invocation-account-and-region>` · `<quota-owner>` · `<billing-owner>` |

## 2. Service checks

### CloudWatch, SNS and AWS Health

- In **CloudWatch → Alarms**, check the current state and history. Match metric dimensions and SNS actions to the intended resources. For `INSUFFICIENT_DATA`, check traffic, metric delivery and alarm dimensions. In **SNS → Subscriptions**, check confirmation and the on-call recipients after a rotation.
- In **CloudWatch → Log groups**, check the latest event, retention and operator access. For EKS Fargate application logs, verify delivery if forwarding is configured. NDS uses `features.alerting` for the AWS infrastructure alert configuration.
- In **AWS Health → Your account events**, check account-specific incidents and scheduled changes. Record affected resources and review dates. [NDS observability runbook](observability.en.md) · [AWS Health](https://docs.aws.amazon.com/health/latest/ug/getting-started-health-dashboard.html)

### ALB, Route 53, ACM and VPC

- In **EC2 → Load Balancers / Target Groups**, check listeners, host rules and target health. Inspect `HTTPCode_ELB_5XX_Count` and `HTTPCode_Target_5XX_Count` separately. Compare response time and health-check reasons in the same incident window.
- In **Route 53 → Hosted zones**, or with the corporate DNS provider, verify where each record points. In **ACM → Certificates**, check hostnames, listener association, expiry and renewal, and DNS validation records.
- In **VPC → Subnets / Route tables / Security groups**, check available IPs, allowed traffic between ALB and workloads, administrator access, and configured NAT or VPC endpoint paths. Verify employee and administrator hosts after DNS or certificate cutover. [Domains and TLS](domain.en.md) · [ALB metrics](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-cloudwatch-metrics.html) · [ACM DNS renewal](https://docs.aws.amazon.com/acm/latest/userguide/dns-renewal-validation.html)

### EKS or ECS, and ECR

- In **EKS → Clusters**, check Kubernetes support dates, update status, add-ons and Fargate profiles. For Pending Pods, inspect workload events for scheduling and image-pull failures.
- Where ECS is deployed, check desired and running task counts, the last deployment, service events and stopped-task reasons in **ECS → Clusters → Services**. For failures, review the task execution role, image, secret references and target group.
- In **ECR → Repositories**, verify the current and previous known-good image digests. Confirm that retention policies keep the images needed for rollback. [EKS upgrades](upgrade-eks-cluster.en.md) · [Update and rollback](update.en.md)

### Aurora, RDS Proxy and ElastiCache

- In **RDS → Databases / Automated backups**, check writer and reader status, events, connection count and available capacity, backup retention and the latest restorable time. Where used, check target health and connection borrow latency in **RDS → Proxies**.
- For a restore drill, restore Aurora into a separate cluster and verify connectivity. For Proxy-based deployments, verify the target in the test path. Check the schema, sign-in, a representative model request, and usage and audit queries. Record recovery duration and recovery point.
- In **ElastiCache → Valkey or Redis OSS**, check replication group status, `EngineCPUUtilization`, memory, `CurrConnections`, `Evictions` and replication lag. After a disruption, verify budget enforcement and check for cost-event processing lag. [Disaster recovery runbook](dr-runbook.en.md) · [Aurora backups](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Managing.Backups.html) · [ElastiCache metrics](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/CacheMetrics.WhichShouldIMonitor.html)

### Cognito, IAM, Secrets Manager and CloudTrail

- In **Cognito → User pools**, verify the app client, callback and logout URLs, and active IdPs. For ADFS, check SAML metadata, the signing certificate rollover schedule and group mapping. For a mapping to `custom:groups`, check mutability and app-client read/write attributes. Verify a test user's attribute, token claim and resulting permissions.
- In **IAM → Roles**, check trust and permissions for deployment, service and cross-account roles. In **Secrets Manager**, verify each secret's owner, latest change, rotation method and consuming workloads. Verify DB connectivity and Pod or task behavior after rotation.
- In **CloudTrail → Event history**, check the time, Region, resource and actor of a change. Record the audit log destination for organization-wide or long-term investigations. [ADFS runbook](adfs-sso.en.md) · [Cognito attribute mapping](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-specifying-attribute-mapping.html) · [CloudTrail event history](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/view-cloudtrail-events.html)

### Bedrock and optional features

- In **Bedrock / Service Quotas**, use the actual invocation account and source Region. Check active models and inference profiles, IAM permissions, required subscriptions or access conditions, and request and token quotas. For cross-Region inference, check IAM and SCPs for the profile's destination Regions.
- For enabled search, BI Insight, external pricing, body logging or email, hand over each AWS dependency. Check the relevant AgentCore Gateway or Runtime, Lambda, Firehose/S3 or SES status, logs, execution roles and delivery evidence. [Environment configuration](gateway-yaml.en.md) · [Bedrock Runtime quotas](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-runtime.html) · [Cross-Region profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html)

### AWS billing

- In **Billing and Cost Management → Cost Explorer / Budgets / Cost Anomaly Detection**, review spend by account, service, Region or applicable cost tag. Inspect Bedrock, Fargate, Aurora, ElastiCache, NAT, and log ingestion and retention separately.
- Check the scope and recipients of budgets and anomaly monitors. Verify the billed account for cross-account calls. When comparing the gateway report with AWS charges, align the period, services and charge categories. [AWS Cost Anomaly Detection](https://docs.aws.amazon.com/cost-management/latest/userguide/getting-started-ad.html)

## 3. Incident triage

Record the account and Region, start time and timezone, affected resources, AWS event or request ID, and recent changes. If there is an AWS Health event, match it to the affected resources.

| Symptom | AWS investigation order | Escalate to |
|---|---|---|
| Endpoint failure or rising 502/503/504 | DNS and TLS → ALB and target errors → target health → Pod/task events | Network or platform |
| Login loop or missing group | Cognito callback and IdP → SAML mapping → test user's token group | SSO or platform |
| Model denied or 429 | Bedrock error code → actual role, account and Region → permissions and quotas for the model/profile | Model invocation account owner |
| DB connection failure or sudden latency | RDS events → writer and Proxy target health and borrow latency → deployment or network changes | Database or platform |
| Cache error or delayed cost processing | ElastiCache events → connections, memory and replication → worker health and cost reconciliation | Platform and gateway operations |
| Infrastructure notification absent | Alarm state and history → action and SNS subscription → test delivery to on-call | Monitoring or on-call |
| AWS cost spike | Billed account and time window → service, Region and tag → release or usage change and anomaly history | Billing and service owners |

## 4. Review cadence and change completion

| When | Check |
|---|---|
| Daily | CloudWatch alarms, AWS Health, target, RDS and ElastiCache health, and cost anomalies |
| Weekly | Certificate and SSO metadata renewal, maintenance schedule, backup restorable time and open warnings |
| Monthly | Cost, quotas, subnet headroom, ECR retention, rollback images, IAM and recipients |
| Quarterly or at handover | Aurora restore drill, on-call test notification, emergency operator access and cross-account model verification |
| Before and after a change | Record ownership and current state, review configuration and plan, apply, then verify resource health and affected workflows |

Compare console observations with `gateway.yaml`, Terraform and any platform-team configuration. For an emergency console change, record the reason, scope, approval and rollback criteria, then reconcile configuration and state before the next deployment. Close the change or incident when the affected resources are healthy and the relevant sign-in, representative request, cost recording and alert verification pass. [Operations handover](op-guide/README.md) · [Deployment changes](update.en.md)
