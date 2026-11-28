# Roll out the Windows Cowork installer

[한국어](cowork-installer-admin-e2e-windows.md) · **English** · [Cowork guides](../README.en.md)

Build the installer with the target environment’s coordinates. Separate the build host, administrator deployment steps, and employee sign-in.

| Stage | Owner | Completion check |
|---|---|---|
| Build and sign | Build administrator | Installer and checksum from [the build guide](cowork-installer-build-windows.en.md) |
| Install the package and managed policy | Windows administrator | Machine-wide registry policy and the expected helper path |
| Install Claude Desktop and authenticate | Employee | Readiness check, browser sign-in, then `gateway-cli-cowork verify` |

## Set up the employee PC

1. Install the signed package in an administrative session. Use the machine-wide scope for the standard rollout.
2. Apply the gateway policy after installation. The setup command writes the Claude Desktop policy under `HKLM\SOFTWARE\Policies\Claude`.
3. Hand off the installer instructions and readiness tool or approved download location. [Employee guide](cowork-installer-user-windows.en.md)

```powershell
gateway-cli-cowork setup
Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Claude'
```

Verify the gateway URL, managed inference settings and helper path. Verify as the employee after login. Coordinate tab restrictions separately through organization policy. [Revert or remove](cowork-installer-uninstall-windows.en.md)
