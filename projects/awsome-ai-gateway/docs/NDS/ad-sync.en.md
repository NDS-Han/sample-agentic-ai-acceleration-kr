# Map AD groups and verify user provisioning

[한국어](ad-sync.md) · **English** · [NDS documentation](README.en.md)

A successful ADFS sign-in returns a signed SAML assertion to Cognito. Cognito creates or updates the federated profile. The gateway reads the configured group claim and resolves team membership and role when it provisions the user.

## Relying party and claim mapping

Give the identity administrator the RP identifier `urn:amazon:cognito:sp:<user-pool-id>` and the ACS URL `https://<hosted-ui-host>/saml2/idpresponse`. The administrator console is the user-facing entry point. Use [ADFS setup](adfs-sso.en.md) for the deployment procedure.

Map the actual assertion attribute name or URI to `custom:groups`. If several gateway groups are needed, use one semicolon-separated string of unqualified group names. Configure the exact group prefix and administrator group name. Confirm that the mapped attribute is mutable and that the app client has the required attribute access.

`oidc.admin_groups` matches exact group names. An approved `oidc.admin_emails` entry requires a verified email and grants the role only. The configured group parsing rules still determine the team. Team leader assignment is managed in the console.

## Verify an account

1. Sign in with an authorized federated test account.
2. Check the Cognito user record and the group's presence in the ID token. Keep token text private.
3. Open **Users/Teams** and confirm the expected team and role. Verify a normal user and an administrator separately.
4. Change or remove a test group and sign in again. Verify the resulting access and the behavior when the source omits the claim.

Web sign-in uses `/v1/auth/provision` to provision without issuing a virtual key. `gateway-cli login` uses the exchange path to issue a VK. **Sync Cognito** reconciles users already in the pool. Plan a native emergency administrator path before switching the configured claim.

For offboarding, consider AD sign-in, existing Cognito sessions, and gateway VKs as separate access paths. Verify that gateway access and active keys are disabled as required.
