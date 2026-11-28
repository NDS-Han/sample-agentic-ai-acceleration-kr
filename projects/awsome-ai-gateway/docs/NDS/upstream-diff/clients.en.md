# Clients and employee distribution

[한국어](clients.md) · **English** · [Comparison index](README.en.md)

| Component | Changes since the base | What to verify |
|---|---|---|
| `installer/` | Windows Inno Setup, optional offline wheels, Linux deb packaging, site input and managed drop-ins | Deployment URLs, generated settings, and preserved settings after install/removal |
| `cowork-installer/` | Separate Cowork/Claude Desktop installation and managed profile | HTTPS, actual managed profile and login |
| `gateway-cli/` | Existing login, expiry, secure writes and restore improvements, env/file and STS paths, and OTEL wiring | CLI profile, expired key reauthentication and storage permissions |
| `gateway-clients/` | Environment defaults and app-specific behavior | Exact alias sent by the app and resulting policy |
| `policy-helper/` | Optional tool merges VK-authenticated `/v1/policy` fragments over a Claude Code baseline, with install/check/rollback scripts | Actual helper rollout and returned policy |

Hand employees the environment settings from `./deploy client-values`. User credentials and OTEL bearer headers stay at user scope. Managed settings can be readable by multiple users, while CLI secret files use mode 0600 and atomic writes. Verify managed settings, employee sign-in, VK issuance, a request in each supported app, usage capture and removal behavior. Verify current package and helper distribution with the deployment owner.

[Employee setup](../client-setup.en.md) · [Installer](../../../installer) · [Cowork installer](../../../cowork-installer) · [Policy helper](../../../policy-helper)
