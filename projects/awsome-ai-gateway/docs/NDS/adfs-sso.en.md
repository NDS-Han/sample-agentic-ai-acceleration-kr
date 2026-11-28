# Configure Cognito SAML federation with ADFS

[한국어](adfs-sso.md) · **English** · [NDS documentation](README.en.md)

Collect the ADFS federation metadata URL or XML, exact group claim URI, group prefix, and administrator group. Coordinate both values of the Relying Party Trust:

| Field | Value |
|---|---|
| RP Identifier | `urn:amazon:cognito:sp:<user-pool-id>` |
| ACS URL | `https://<hosted-ui-host>/saml2/idpresponse` |

The setup tool prints the environment-specific values and writes the provider and group configuration to `gateway.yaml`.

```bash
python3 deployment/scripts/setup-adfs-sso.py setup \
  --provider-name ssir-adfs \
  --metadata-url https://adfs.example.com/federationmetadata/2007-06/federationmetadata.xml \
  --group-prefix ssir_ --admin-group ssir_claude_admin
```

Use `--metadata-file` when the metadata is supplied as a local XML file. Match the source claim explicitly if the IdP uses a different URI. Review and adopt any existing provider before applying, then run `./deploy render`, `./deploy apply --plan` and `./deploy doctor` around the approved change.

Use unqualified ADFS group names. Select only gateway groups and send them as one semicolon-separated string. Group mapping requires mutable `custom:groups` and app-client **WriteAttributes** to populate it, plus **ReadAttributes** to expose it in the token. The repository Terraform module currently lists `custom:groups` in read attributes. Review deployed and declared settings and protect the role-bearing attribute from end-user changes. [AWS attribute mapping reference](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-specifying-attribute-mapping.html)

Verify the hosted sign-in button, a federated user's first login and group claim, their gateway team and role, a separate CLI login, and the native emergency account. [Group mapping and provisioning](ad-sync.en.md)
