# Install and use Cowork on Windows

[한국어](cowork-installer-user-windows.md) · **English** · [Cowork guides](../README.en.md)

Your Windows administrator installs the gateway helper and managed policy. Ask them for the approved Claude Desktop build and the Cowork readiness checker if your PC uses an offline distribution.

1. Run the readiness check. Resolve any reported virtualization or Windows feature issues, then rerun it.
2. Install the approved Claude Desktop application.
3. In a normal PowerShell session under your own account, sign in through the assigned Cognito or federated login. Use the callback port from the rollout instructions.
4. Launch Claude Desktop, select an approved model, and send a short test. Run the verification command.

```powershell
gateway-cli-cowork login --redirect-port 8091
gateway-cli-cowork verify
```

If verification fails, share the failing check with IT. Check whether the managed registry policy is present, the network can reach the gateway over trusted HTTPS, and the current sign-in is the account provisioned for you.
