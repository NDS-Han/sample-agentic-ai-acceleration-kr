# Configure Cowork on macOS

[한국어](cowork-client-install-macos.md) · **English** · [Cowork guides](README.en.md)

Ask IT for a trusted HTTPS gateway URL, the client sign-in values, approved model aliases, a managed `.mobileconfig` profile and its credential helper. Install the organization’s approved Claude Desktop build.

1. Install `gateway-cli` and complete the OIDC sign-in using [Employee quick start](../client-setup.en.md).
2. Install the credential helper at the absolute path declared by the managed profile. Run the helper as your own user for the sign-in cache.
3. Validate and open the profile, then approve installation in System Settings.
4. Launch Claude Desktop after installing the profile. If the app was open, quit it and relaunch. Select an approved model and send a short test.

```bash
plutil -lint ~/Downloads/nds-llm-gateway-cowork.mobileconfig
open ~/Downloads/nds-llm-gateway-cowork.mobileconfig
```

A managed profile sets the gateway provider, base URL, Bearer scheme, helper path, helper lifetime and supported model aliases. Use the organization's device-management path for fleet rollout. The [profile and helper template](cowork-client-install-macos.md) lists the exact keys and verification commands.

If the app opens a claude.ai login screen, verify the managed profile. If the helper fails, check its executable path and signed-in account. Verify the TLS certificate, app classification, and model alias before escalating.
