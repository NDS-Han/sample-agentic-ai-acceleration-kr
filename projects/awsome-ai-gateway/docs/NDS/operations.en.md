# Gateway operations runbook

[한국어](operations.md) · **English** · [NDS documentation](README.en.md)

Use [Administration](guides/admin-guide.en.html) for screen-by-screen tasks and [Handover](op-guide/README.md) for the full infrastructure and access checklist.

## Onboard or offboard a user

1. Verify the identity path. For a native Cognito user, assign the correct team group and additional administrative access when intended. For ADFS, check the configured claim and team mapping.
2. Check the first web sign-in or run **Users/Teams → Sync Cognito** when you need to reconcile an existing pool record. Verify the user's team and effective role.
3. For desktop clients, share the environment values from `./deploy client-values` and the [employee guide](client-setup.en.md). The user obtains a VK through `gateway-cli login`.
4. During offboarding, verify identity-provider access, the Cognito session and gateway keys as separate controls.

## Route models

- In **App Policy**, review **Cross-account routing** for the client. An in-account route uses the gateway workload identity. Cross-account routing requires a destination role, trust and gateway permission to assume it.
- In **Model Management**, model-specific routing takes precedence over a client route. Check the destination role, external ID and region for the expected alias.
- Verify model entitlement in the serving account and model family before registering an alias. For Marketplace subscription errors, check the account that actually invokes the model.
- Prefer console or supported API updates so caches and audit records follow the change. If a documented database fallback is required, verify the cache invalidation procedure for the affected scope.

## Before onboarding employees

Verify OIDC sign-in, ingress and administrator boundaries, trusted HTTPS for Cowork, Bedrock model access, token lifetime, backups, and authorized storage of the VK encryption key. Run `./deploy doctor` against the expected environment and send one representative request through the intended client. For deployment incidents use [Observability](observability.en.md) and [Recovery](dr-runbook.en.md).
