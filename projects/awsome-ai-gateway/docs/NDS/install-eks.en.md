# Deploy or adopt on EKS

[한국어](install-eks.md) · **English** · [NDS documentation](README.en.md)

Choose this route for a new Kubernetes deployment or to bring an existing EKS installation under NDS management. Prepare the [deployment host](install-prereq.en.md), AWS identity, Helm, and Kubernetes context for the target.

## New infrastructure

```bash
./deploy init
./deploy bootstrap --plan
./deploy bootstrap
```

Select `eks` in `init`. Review the environment, region, image pins, and state before bootstrap. If bootstrap scaffolds a Terraform environment from the dev template, review its cluster version, add-on versions, tags, and administrator email before rerunning. Declare at least two availability zones and fit the subnet plan to the target VPC. The noninteractive bootstrap path reads the declared inputs from `gateway.yaml`.

Bootstrap ensures Terraform state, provisions the VPC, EKS, Aurora, ElastiCache, Cognito and IRSA, seeds application and Redis secrets, builds or selects the service images, applies the gateway Helm release, and provisions the first Cognito administrator. The database secret remains Terraform-owned. Verify the resulting hosts, callback URLs, model catalog, and model routing profile. Run the repository smoke test for the environment.

## Existing deployment

1. Confirm the EKS context, Helm namespace and release, Terraform environment directory, and ownership of the live stack.
2. Capture the existing settings with `./deploy doctor --capture`. Review the generated file and unresolved fields. Keep the live database, Cognito, domain and release coordinates in sync with the environment.
3. Validate and adopt the reviewed file using the [capture and adoption procedure](update.en.md).
4. Compare `./deploy apply --plan` to the current state. Proceed when the expected Helm and managed overlay changes are visible. The existing Terraform environment continues to own its infrastructure.

## Update and validate

Use `./deploy apply --plan`, `./deploy apply`, and `./deploy doctor` in that order. Use `deploy.capacity_tier` to set the baseline HPA replica band and `deploy.sizing` for explicit overrides. Review Redis and Aurora headroom when changing capacity. [Configuration reference](gateway-yaml.en.md)

Use [Domains and TLS](domain.en.md) to attach a hostname and verify callbacks. Use [EKS and Aurora upgrades](upgrade-eks-cluster.en.md) for control-plane, add-on and database engine versions. [Deployment changes](update.en.md) describes the difference between deleting the Helm release and including Terraform infrastructure.
