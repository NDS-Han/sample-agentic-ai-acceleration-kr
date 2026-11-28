# Identity, authorization and credentials

[한국어](identity.md) · **English** · [Comparison index](README.en.md)

## Cognito and gateway users

| Workflow | NDS behavior |
|---|---|
| First web login | Validate the token, call `POST /v1/auth/provision`, resolve team and role, and look up `/admin/my/profile`. This flow issues no VK. Provision failure prevents session creation. |
| CLI login | `POST /v1/auth/exchange` shares provisioning and issues an employee VK. The supported STS flow checks configured account, Region and role restrictions. |
| Cognito synchronization | Group sync and full user enumeration cover federated users with mapped attributes but no native Cognito group membership. Per-user and per-group operations are available. |
| Administrator and manual assignment | Administrator roles check exact groups or a configured verified email. Synchronization observes manual assignment sources and protections around administrator roles. |

`1ab19b29` separated web provisioning from token exchange, and `033d0632` made the group claim configurable. Review required group, prefix and unmatched group rules before user onboarding. [OIDC router](../../../admin-api/src/app/routers/auth_oidc.py) · [Group mapping](../ad-sync.en.md)

## ADFS SAML and group mapping

Changes `85f608a9` and `6d382cd4` bring the provider, metadata, attribute mapping and provider details under `infra.cognito_saml`. Give ADFS the RP Identifier `urn:amazon:cognito:sp:<pool-id>` and the HTTPS ACS at `/saml2/idpresponse` as separate values. If the ADFS attribute maps to mutable Cognito `custom:groups`, allow the relevant app client to write that attribute. At the comparison endpoint the Terraform module lists `custom:groups` for reading and omits it from the write list. Verify both the running app client and its declaration. [Cognito app-client definition](../../../deployment/terraform/modules/cognito/main.tf)

Test one user's assertion, then Cognito profile, configured JWT group claim, and gateway role and team. An omitted attribute on a subsequent assertion can leave an older stored value, so test group removal as well. Verify the emergency native access path if the environment selects a single group claim for both flows. [ADFS runbook](../adfs-sso.en.md)

## Credential boundaries

Employee VKs use the CLI exchange. Long-lived service-account VKs are issued in **Agents**, and employee login blocks service accounts. Ordinary and AICM scoped service tokens share a `svc-` prefix and have separate authorization boundaries. Dev login is gated for evaluation. Logout clears the local session and invokes Cognito sign-out. Verify the IdP session separately when federating through ADFS. [Administration API](admin-api.en.md) · [Operations handover](../op-guide/README.md)
