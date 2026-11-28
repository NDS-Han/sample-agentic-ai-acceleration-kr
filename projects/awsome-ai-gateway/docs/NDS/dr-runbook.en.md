# Disaster recovery runbook

[한국어](dr-runbook.md) · **English** · [NDS documentation](README.en.md)

| Loss | Recovery source |
|---|---|
| Compose database or instance | Off-host PostgreSQL dump and the environment's protected configuration backup |
| ECS or EKS database | Aurora point-in-time restore or a retained snapshot |
| App release | The environment's declaration and compatible image tags |
| VK encryption key | Approved backup or a controlled plan to reissue user and agent VKs |

Set recovery objectives based on the backups that you can demonstrate in a rehearsal. The original runbook contains estimates for each backend.

## Restore and verify

1. Confirm environment, account, database, restoration time, and recovery destination.
2. For Compose, rebuild the original deployment coordinates and restore the database and protected environment values from off-host backups. Confirm the application uses the intended volume.
3. For Aurora, restore into a new cluster, attach an instance, and verify the writer endpoint. Where the application connects through RDS Proxy, update the target and validate the proxy connection. Follow the documented database secret and Terraform reconciliation procedure for the environment.
4. Restart or redeploy affected services so they read the restored endpoint. Verify readiness, expected data, authentication, and a representative gateway request.
5. If the original VK encryption key cannot be restored, reissue user and service credentials through their respective flows.

Use the [original command and rehearsal reference](dr-runbook.md) for the backend-specific examples. [Roll back an application change](update.en.md)
