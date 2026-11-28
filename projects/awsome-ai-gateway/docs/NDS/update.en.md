# Adopt, update, roll back, or remove a deployment

[한국어](update.md) · **English** · [NDS documentation](README.en.md)

## Adopt a live environment

```bash
./deploy doctor --capture --gen-dir deployment/gen/<env>
./deploy validate --config deployment/gateway.captured-<env>.yaml
```

Review the capture notes before adopting the generated YAML. Verify the AWS account, region, target, EKS context or ECS state, hostnames, and feature settings against the live environment. Fill in unobserved values from approved configuration and secret stores. With an existing declaration, an interactive capture can absorb a live value, keep the declared value, or skip it. Keeping a declared value can cause a later apply to restore it.

Review database and cache overrides in ECS. For manual Compose, confirm project and volume names and plan database and secret migration before starting the new project.

## Apply a configuration or image change

```bash
./deploy apply --plan
./deploy apply
./deploy doctor
```

Review the plan for the intended environment. For an image release, pin the target image version in `images.tag` or the supported per-service pins. Confirm the preceding database migration and verify the post-change services and application smoke test. Use `./deploy client-values` after public URL changes.

## Roll back

Restore the prior image pin or configuration where compatible and preview the change. Database migrations are forward-only. Verify that the prior application can run on the current schema. Use [Disaster recovery](dr-runbook.en.md) for database restore.

## Change domain or network exposure

Use [Domains and TLS](domain.en.md) for TLS and callbacks. For a public-to-private switch, verify VPN or internal reachability, DNS resolution, ingress policy and allowed CIDRs before applying.

## Remove a deployment

Confirm the exact environment, backups and tool-owned scope first. `./deploy teardown` removes Compose stack resources or the managed ECS environment. EKS teardown removes the Helm release by default, with an explicit `--infra` option for the Terraform environment. Compose volume deletion is explicit through `--purge`. Read the displayed scope before confirming.
