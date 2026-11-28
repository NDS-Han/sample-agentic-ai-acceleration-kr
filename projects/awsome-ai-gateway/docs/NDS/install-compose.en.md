# Deploy with Docker Compose

[한국어](install-compose.md) · **English** · [NDS documentation](README.en.md)

Compose is the t0 evaluation path on a single EC2 host. Prepare Docker, the NDS repository, and AWS credentials and connectivity for the Bedrock models used by the environment.

## Configure and start

```bash
python3 -m venv docs/NDS/.venv
docs/NDS/.venv/bin/pip install -r docs/NDS/deploy/requirements.txt
./deploy init
./deploy apply --plan
./deploy apply
./deploy doctor
```

Select `compose` and `t0` during initialization. Set the deployment region and allowed ingress CIDRs. Choose HTTPS before onboarding Cowork. Configure the OIDC and IAM role settings for the intended login and virtual key path. In Compose, the CLI role allowlist is required by the key-issuance path.

On startup, verify the migration completed, PostgreSQL and the gateway are healthy, and the administrator login and a model request work in the selected environment. Use [Domains and TLS](domain.en.md) to connect hostnames and [Client distribution](client-admin.en.md) to distribute validated values.

## Back up and change the deployment

Keep an off-host database backup and a protected copy of the generated .env values, especially `VIRTUAL_KEY_ENCRYPTION_KEY`. Record how to restore the same database volume and verify it as part of [disaster recovery](dr-runbook.en.md).

Use `./deploy apply --plan` for changes. When adopting a manually named Compose project, identify its original volumes and copy data and secrets before starting the generated project. A new Compose project name can create fresh volumes. Use `./deploy teardown` to remove the stack and consider the documented `--purge` option only when you intend to remove volumes. [Capture and teardown](update.en.md)
