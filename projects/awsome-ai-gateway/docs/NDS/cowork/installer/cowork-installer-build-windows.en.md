# Build the Windows Cowork installer

[한국어](cowork-installer-build-windows.md) · **English** · [Cowork guides](../README.en.md)

Use a Windows build host with Git, Python 3.11 or later, and the repository’s supported Inno Setup installation. Check out `nds/dev` and work from `projects\awsome-ai-gateway\cowork-installer\installer`.

1. Obtain the environment values with `./deploy client-values` on the deployment host. Confirm the trusted gateway endpoint, approved model aliases and the intended organization settings.
2. Fill `packaging\site-config.json` for this environment using the checked-in example and field definitions.
3. Run the build script with the approved signing options. Verify the result and checksum before distribution.

```powershell
powershell -File packaging\build.ps1
```

The installer is written under `dist\installer\`. Keep environment-specific config and signing material in the approved store. See the [exact field and offline build reference](cowork-installer-build-windows.md) and [rollout guide](cowork-installer-admin-e2e-windows.en.md).
