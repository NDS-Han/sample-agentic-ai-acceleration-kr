# Configure Cowork manually on Windows

[한국어](cowork-client-install-windows.md) · **English** · [Cowork guides](../README.en.md)

Use this reference for Windows clients already maintained outside the installer. For a new rollout, follow [the Windows installer guide](../installer/cowork-installer-admin-e2e-windows.en.md).

1. Install the approved gateway CLI, configure the target environment, and sign in under the employee account. [Employee quick start](../../client-setup.en.md)
2. Install the Claude Desktop build and verify Cowork readiness.
3. Configure the managed inference policy with the intended provider, trusted URL, Bearer scheme, helper path, helper TTL and model aliases. The normal manual rollout uses `HKLM\SOFTWARE\Policies\Claude`. Use the expected registry value types and the absolute wrapper path.
4. Restart the app, check a short request, and verify classifier and model alias in the gateway.

Use the [exact managed-policy template and diagnostics](cowork-client-install-windows.md) for a device whose configuration must match the original rollout. Record ownership before changing machine-wide settings.
