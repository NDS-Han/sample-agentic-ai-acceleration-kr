# Revert or remove the Windows Cowork installer

[한국어](cowork-installer-uninstall-windows.md) · **English** · [Cowork guides](../README.en.md)

Choose the action that matches the intended device change. Manage machine policy in an administrative PowerShell session and user sign-in state in the affected employee account.

| Task | Command |
|---|---|
| Remove the gateway policy while keeping the installer | `gateway-cli-cowork disable` |
| Reapply the managed gateway policy | `gateway-cli-cowork setup` |
| Clear the current user's sign-in | `gateway-cli-cowork logout` |
| Clear managed settings and uninstall | `gateway-cli-cowork uninstall --clear-first` |

Quit and relaunch Claude Desktop when verifying the result. Check the residual policy, installed components, and any user-specific sign-in cache. Removing Cowork configuration can affect another client if that client was explicitly configured to use the same helper. See the [original removal checks](cowork-installer-uninstall-windows.md).
