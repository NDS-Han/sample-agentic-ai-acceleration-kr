# Deploy on ECS Fargate

[한국어](install-ecs.md) · **English** · [NDS documentation](README.en.md)

ECS provisions a new Fargate environment with a VPC, Aurora, ElastiCache, ALB, Cognito, Secrets Manager and ECR. Use `t1` for a smaller starting topology or `t2` for the standard multi-AZ topology. Check the account, region, IAM role and state backend before creating resources.

## Create a deployment

```bash
./deploy init
./deploy bootstrap --plan
./deploy bootstrap
./deploy doctor
```

Select `ecs` and the target tier in `init`. Bootstrap can create the Terraform state bucket and lock table and records the infrastructure inputs in `gateway.yaml`. If you already have the designated state backend, declare `deploy.tfstate_bucket` and `deploy.tfstate_table` as appropriate. To inspect the generated Terraform inputs before applying, run `./deploy render` and review the environment output.

AWS state, images, database migration and ECS startup are part of this deployment. Check service health, the migration task, ALB listeners, and the gateway URL. Confirm the first Cognito administrator and test an authenticated model request.

## Operate and update

- Use `domain.mode: route53-acm` when moving to approved HTTPS hostnames. Review Cognito callback URLs and regenerate the employee environment values after the change.
- Cognito issues tokens for ECS. Declare a SAML identity provider with `infra.cognito_saml`. [ADFS setup](adfs-sso.en.md)
- `features.bi_insight` enables the ECS AgentCore runtime workflow. `features.web_search` and `features.pricing_lambda` are optional deployed integrations. [BI Insight](chat-agent.en.md)
- Preview a new image pin using `./deploy apply --plan`, apply it, and check health with `./deploy doctor`. For rollback, pin a prior image only after checking compatibility with the migrated schema.
- A private deployment uses `network.mode: private`. Verify clients can resolve and reach the private ALB and its TLS endpoints.

`./deploy teardown` covers the ECS Terraform environment and environment-owned dependencies. Confirm deletion scope and retain any required database snapshot before teardown. See [Deployment changes](update.en.md).
