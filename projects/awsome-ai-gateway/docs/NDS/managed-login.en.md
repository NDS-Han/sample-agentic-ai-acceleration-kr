# Configure Cognito Managed Login branding

[한국어](managed-login.md) · **English** · [NDS documentation](README.en.md)

On ECS and EKS, enable Managed Login with `branding.managed_login: true` in `gateway.yaml`. Review the plan for the Cognito plan tier, hosted domain login version, and app-client branding resource before applying.

The design sources are in `deployment/terraform/modules/cognito/managed-login/`. Update the versioned settings and assets there. A change to the branding resource can recreate the branding for the same client. When a style already exists for that client, review ownership before import or replacement.

Test browser sign-in, password reset, and the appropriate identity-provider button after applying. Compare the desktop and narrow forms in the browser. See [Administrator sign-in and access](guides/admin-guide.en.html#access) for the user workflow.

To return to the classic hosted UI, remove or disable the feature in `gateway.yaml` and use the target environment Terraform directory to review a plan for `module.cognito` with `cognito_managed_login=false`. The branding resource is removed by the resulting Terraform change. Review the independent user-pool tier setting separately.
