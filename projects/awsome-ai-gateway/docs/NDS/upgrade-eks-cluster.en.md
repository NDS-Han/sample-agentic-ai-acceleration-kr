# Upgrade EKS and Aurora

[한국어](upgrade-eks-cluster.md) · **English** · [NDS documentation](README.en.md)

Upgrade the EKS control plane one minor version at a time. Confirm the current Kubernetes context, AWS identity, environment Terraform variables, compatible add-ons and workload readiness.

```bash
./deploy upgrade eks --to 1.<minor> --plan
./deploy upgrade eks --to 1.<minor>
./deploy doctor
./deploy apply --plan
```

The upgrade path checks the live version, plans a single hop, verifies the Terraform change scope, and reconciles add-ons. Retain compatible existing pins. Resolve incompatible add-on pins or upgrade insights before continuing. If interrupted, inspect control-plane and add-on versions before retrying.

For the manual fallback, use the target environment's tfvars and generated managed-overlay variables as documented in the [Korean fallback procedure](upgrade-eks-cluster.md). Confirm that rollouts and application requests succeed before starting another hop.

Aurora is a separate maintenance operation. Check the live database engine and `aurora_engine_version` for the environment, take the required snapshot, and assess the restart and compatibility impact. The repository environment defaults are dev 16.13 and prod 16.11. Verify the database, application and gateway requests after upgrade.
