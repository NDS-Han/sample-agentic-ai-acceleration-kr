# Configure domains and TLS

[한국어](domain.md) · **English** · [NDS documentation](README.en.md)

| Mode | Use |
|---|---|
| `none` | HTTP for restricted evaluation paths |
| `selfsigned-temp` | Temporary EKS administrator HTTPS with a self-signed certificate |
| `route53-acm` | Regular HTTPS deployment with the intended gateway, administrator and API hosts |

Cowork requires a trusted HTTPS gateway endpoint. `cloudfront-temp` is a reserved mode.

```yaml
domain:
  mode: route53-acm
  name: example.com
  zone_id: Z0123456789ABCDEFGHIJ
```

Use the organization’s DNS zone. With Route 53 control, provide the matching zone ID. For an externally managed DNS zone, coordinate validation and host records with its owner. On Compose, point the hosts at the deployment address before certificate issuance. For ECS or EKS, check ACM validation, HTTPS listeners and the DNS mapping to the target load balancer. EKS can also declare explicit `domain.hosts` values.

Review `./deploy apply --plan` and apply the approved change. Confirm that each hostname resolves to the expected endpoint and that TLS is valid for its name. Verify Cognito sign-in, callback and logout URLs after changing the administrator hostname. Regenerate `./deploy client-values` for clients. [SAML relying-party settings](adfs-sso.en.md)
