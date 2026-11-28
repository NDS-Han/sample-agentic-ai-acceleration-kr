# Connect Claude Code, Cowork, or Codex

[한국어](client-setup.md) · **English** · [NDS documentation](README.en.md)

Ask the gateway administrator for your sign-in account and the four values below. Use the values for the environment you intend to connect to. Cowork requires trusted HTTPS.

| Value | Purpose |
|---|---|
| `OIDC_ISSUER_URL` and `OIDC_CLIENT_ID` | Browser sign-in |
| `ADMIN_API_URL` | User registration and key exchange |
| `ANTHROPIC_BASE_URL` | Gateway endpoint |

On macOS, Linux or WSL, export those variables in your shell. On Windows, set the same names through PowerShell's `$env:NAME` syntax. Verify that your network can reach the gateway and the administrator API.

## Install and sign in

If IT supplies a package, use its instructions. For a source install, run this from the checked-out NDS `projects/awsome-ai-gateway` folder. Use the source revision supplied by your administrator.

```bash
uv tool install --from ./gateway-cli gateway-cli
gateway-cli login --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"
```

```powershell
pip install .\gateway-cli
gateway-cli login --issuer-url "$env:OIDC_ISSUER_URL" --client-id "$env:OIDC_CLIENT_ID"
```

Complete browser sign-in under your own account. A Windows client and a WSL client can have separate home directories and credential stores. Use the environment where the client binary runs.

## Claude Code

Install the managed gateway policy from an administrator shell. On macOS, Linux and WSL:

```bash
sudo gateway-cli setup --gateway-url "$ANTHROPIC_BASE_URL" \
  --admin-api-url "$ADMIN_API_URL" \
  --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"
```

On Windows, run in an elevated PowerShell:

```powershell
gateway-cli setup --gateway-url "$env:ANTHROPIC_BASE_URL" --admin-api-url "$env:ADMIN_API_URL" --issuer-url "$env:OIDC_ISSUER_URL" --client-id "$env:OIDC_CLIENT_ID"
```

Open Claude Code and check `/status` for managed settings and the credential helper. WSL can run a Windows binary, whose managed settings belong under the Windows Program Files policy path.

## Cowork

- [macOS](cowork/cowork-client-install-macos.en.md) uses a managed profile and a local credential helper.
- [Windows](cowork/installer/cowork-installer-user-windows.en.md) uses the organization’s installer and machine policy.
- A Windows desktop app started from a WSL workflow still uses the Windows setup.

## Codex CLI and Desktop

Use the user's `.codex/config.toml` on the platform that runs Codex. Merge the settings below into the existing configuration and keep unrelated providers and preferences. Replace the model and URL with the approved alias and gateway address. Use the absolute path to the credential helper supplied for your platform. The helper needs the environment coordinates even when launched by the desktop application.

```toml
model = "<approved-model-alias>"
model_provider = "gateway"

[model_providers.gateway]
name = "NDS Gateway"
base_url = "https://<gateway-host>/v1"
wire_api = "responses"
requires_openai_auth = false

[model_providers.gateway.http_headers]
originator = "codex_desktop"

[model_providers.gateway.auth]
command = "<absolute path to approved credential helper>"
timeout_ms = 10000
refresh_interval_ms = 300000
```

On Windows, use the installer-provided helper or a wrapper supplied by your administrator. A wrapper must set the expected values before invoking the helper. Start Codex, run a short request, and ask your administrator to verify that the gateway records it as `codex` with the expected model.

## Troubleshooting

| Symptom | First check |
|---|---|
| Browser sign-in works but a key cannot be issued | Team/group mapping and network reachability to the administrator API |
| Model request returns 403 | Registered model alias, client routing, effective model access and key scope |
| Codex requests appear as `other` | Provider and originator header |
| Cowork prompts for a personal login | Managed profile or registry policy and trusted TLS |
| Credential helper fails only in the desktop app | Absolute executable path and environment supplied by the wrapper |

Keep tokens and VKs out of troubleshooting messages. Share the request ID, time, app and model alias with the operator.
