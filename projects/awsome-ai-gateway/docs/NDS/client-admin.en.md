# Distribute and support gateway clients

[한국어](client-admin.md) · **English** · [NDS documentation](README.en.md)

## Prepare what employees receive

1. Run `./deploy client-values` on the target deployment host. Check the OIDC issuer and client ID, administrator API URL, and gateway URL.
2. Verify the user's native group or ADFS group mapping and confirm the employee egress range can reach the gateway and administrator API.
3. Share the approved installer or source revision, environment values, and [employee quick start](client-setup.en.md). Cowork needs trusted HTTPS.

## Build the applicable package

| Package | Source |
|---|---|
| Windows CLI suite for Claude Code and Codex | `installer/packaging/` |
| Linux or WSL CLI suite | `installer/packaging-linux/` |
| Windows Cowork installer | `cowork-installer/installer/packaging/` |

Use the target operating system for the build. Copy the example site configuration, fill it with the deployment’s coordinates, sign artifacts according to the organization's process, and make the checksums available. Keep local config and credentials in the approved store. [Cowork administrator rollout](cowork/installer/cowork-installer-admin-e2e-windows.en.md)

When troubleshooting, verify account provisioning and approved model aliases first. Check the actual client classification and key scope, especially if Codex usage is counted as `other`. For Cowork, confirm the managed policy and trusted TLS. [Operations runbook](operations.en.md)
