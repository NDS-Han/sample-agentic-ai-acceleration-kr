# Deployment design decisions

[한국어](decisions.md) · **English** · [NDS documentation](README.en.md)

These notes describe the rationale for the NDS deployment model.

- A single `gateway.yaml` records environment intent. The renderer produces backend-specific configuration without editing checked-in base templates.
- One schema supports Compose, ECS and EKS. Each renderer maps the common model onto the target's own infrastructure and workload resources.
- Environment identity, size tier, user capacity band and explicit sizing overrides describe different concerns. Explicit settings take precedence over presets.
- Generated environment values and secret state are managed per deployment. Application and infrastructure owners should reconcile live ownership before adopting a stack.
- `doctor --capture` makes live settings visible before adoption. `apply --plan` allows the operator to compare desired and live settings before applying.
- App changes, database migrations, and managed infrastructure can have different recovery procedures. Use the operating runbooks for precise behavior.

See [Configuration](gateway-yaml.en.md), [Deployment changes](update.en.md), and the [original design record](decisions.md).
