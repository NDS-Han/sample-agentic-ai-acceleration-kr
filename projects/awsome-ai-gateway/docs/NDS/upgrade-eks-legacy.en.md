# Legacy EKS migration reference

[한국어](upgrade-eks-legacy.md) · **English** · [NDS documentation](README.en.md)

This reference applies to environments originally installed by `install-eks.sh`. For an active environment, start with [EKS adoption](install-eks.en.md) and [Deployment changes](update.en.md).

The original procedure records how operators pinned an applicable image tag, preserved live Helm values, reviewed the load balancer and database settings, and ran migration and smoke tests. A legacy migration image must use the session-based SQL settings compatible with RDS Proxy. Confirm the current supported tag and behavior for the target before attempting a migration.

Use the [historical step-by-step procedure](upgrade-eks-legacy.md) when you need to identify an old constraint or match a prior incident to its release. Capture the live environment before adopting it into `gateway.yaml`.
