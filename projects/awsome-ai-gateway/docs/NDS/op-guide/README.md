# LLM Gateway — Operations & Deployment Guide (NDS)

[한국어](README.ko.md) · **English**

> **Audience:** SSIR operations team taking over the LLM Gateway.
> **Scope:** everything from first install to day-2 operations, updates,
> capacity, and disaster recovery — in one document.
> **Deployment model:** NDS (`gateway.yaml` + `./deploy` tool). The legacy
> `us-deploy` path (`docs/us-llm-gateway/`, install-*.sh scripts) is
> superseded — do not follow it for new work.

---

## Read this on your first day

Use this handover with the [console walkthrough](../guides/admin-guide.en.html) and the [upstream comparison](../upstream-diff/README.en.md). Record an evidence link, operator and timestamp for each of the checks in `14.

1. Inventory account, Region, deploy host, Terraform backend, EKS context or ECS cluster, namespace, ingress, and service ownership for each environment.
2. From the approved deployment host, verify the target and review the running images, migration revision and feature flags. Preserve the previous compatible release configuration for rollback.
3. Sign in as an administrator. With a separate employee test account, verify web provisioning, team and role, CLI VK issuance, a real gateway call and its later appearance in usage.
4. Identify the DNS, SSO, network, database, on-call and cost owners. Confirm secret recovery access through the approved vault and test alert delivery and backup restoration in a designated non-production environment.

### Observed development baseline

The EKS deployment templates on 28 November 2026 KST specified these image tags:

| Workload | Image tag |
|---|---|
| admin-ui | `4fe6d927` |
| admin-api, gateway-proxy, cost-recorder-worker, scheduler | `8040b1d5` |
| notification-worker | `62493ba1` |

These are the desired Deployment images. Check Pod image IDs and rollout status separately. The check covered development. It showed `DEV_LOGIN_ENABLED=false` explicitly in admin-ui and admin-api, and `ROI_AGGREGATION_ENABLED=false` in scheduler. `AICM_API_ENABLED` and `BUSINESS_ALERTS_ENABLED` had no direct environment entry in the inspected templates and default to false in source. Check references to ConfigMaps and Secrets and verify the actual feature before relying on either gate. Development Prometheus was configured for 24 hours with no persistent storage.

Run these checks from the gateway project root when taking a fresh EKS baseline. Substitute the approved context, namespace and config.

```bash
aws sts get-caller-identity
kubectl --context "<eks-context>" -n llm-gateway get deployments
kubectl --context "<eks-context>" -n llm-gateway get pods
./deploy doctor --config "<config-path>" --context "<eks-context>"
```

## 0. What changed: us-deploy → NDS

The gateway is no longer deployed by per-site shell scripts. NDS makes the
deployment **declarative and portable**:

| Legacy (us-deploy) | NDS |
|---|---|
| Site-pinned scripts (`install-eks.sh`, manual helm/terraform sequencing) | One config file `deployment/gateway.yaml` is the source of truth |
| Region/account baked into scripts | Any AWS account/region — answers live in `gateway.yaml`, not code |
| Hand-run helm + terraform + secret seeding | `./deploy bootstrap` (new) / `./deploy apply` (updates) orchestrate everything |
| State drift invisible | `./deploy doctor` reports drift; `--plan` previews every change before it lands |
| Adoption impossible | `./deploy doctor --capture` reverse-generates `gateway.yaml` from a live deployment |

**Key idea:** `gateway.yaml` declares intent; `./deploy` converges the
world to it. Operators edit the yaml, run `./deploy apply --plan`, review
the diff, then `./deploy apply`.

---

## 1. System overview

```
Employee PC (Claude Code / Cowork / Codex)
      │  HTTPS + Bearer VK
      ▼
Gateway endpoint  ──▶  gateway-proxy (auth, rate limit, budget, routing)
      │                    │
      │                    ├─▶ Bedrock (in-account or cross-account role)
      │                    ├─▶ Redis/ElastiCache (auth cache, budgets, rate limits)
      │                    └─▶ Aurora Postgres (keys, users, usage, audit)
      ▼
admin-ui + admin-api  (ops console — Cognito SSO, optional SAML/ADFS)
```

Components the deploy tool manages:

| Component | Role |
|---|---|
| `gateway-proxy` | Request entrypoint — VK auth, model routing, streaming, budget reservation/settle |
| `admin-api` | Ops API — users/teams/keys/models/budgets/audit; Cognito sync; service-account keys |
| `admin-ui` | Next.js console for admins. See the [Administrator Guide](../guides/admin-guide.en.html). |
| `scheduler`, `notification-worker`, `cost-recorder-worker` | Background jobs — alerts, usage/cost persistence |
| Aurora PostgreSQL | System of record — keys, users, budgets, usage, audit |
| ElastiCache (Redis/Valkey) | Auth cache, budget counters, rate-limit windows |
| Cognito User Pool | SSO for admin-ui and `gateway-cli` (native or SAML/ADFS federation) |
| ECR | Service images, built and pushed by `./deploy` |

Supported clients: **Claude Code**, **Cowork** (Claude Desktop),
**Codex**. Default models are `global.*` inference profiles — region-agnostic.

---

## 2. Deployment targets — pick one

| Situation | Backend | Capacity | Result |
|---|---|---|---|
| Evaluation / POC | `compose` | **t0** fixed | Docker Compose stack on one EC2 |
| Small–standard production | `ecs` (t1/t2) | `capacity_tier` → task counts | Full infra: VPC, Aurora, ElastiCache, ALB, ECS, Cognito, Secrets, ECR |
| Large / new EKS build | `eks` (t2/t3) | `capacity_tier` → pod min/max | `bootstrap` creates infra + app; `apply` manages the helm release |

`deploy.capacity_tier` is declared as a **user-count band** —
`"500" | "1000" | "3000" | "6000" | "6000+"` — which the render expands
into gateway-proxy pod min/max and the Redis pool size:

| band | pods (min/max) | redis pool_size |
|---|---|---|
| 500 | 1 / 3 | 150 |
| 1000 | 2 / 5 | 150 |
| 3000 | 3 / 6 | 150 |
| 6000 | 4 / 8 | 150 |
| 6000+ | 6 / 15 | 150 |

Thread-pool sizing (`bedrock_thread_pool_size`) and individual keys are
overridden via `deploy.sizing` — bands are encapsulated defaults, not
declaration precedence.

> SSIR production runs on **EKS**. The compose and ecs paths are
> documented for completeness; the sections that matter most to you are
> §4 (prereq), §5 (EKS), §7–§11 (day-2 ops).

---

## 3. Command surface

```bash
./deploy init          # first time only — create gateway.yaml interactively
./deploy configure     # item-by-item review wizard → "apply now?" at the end
./deploy bootstrap     # [first time] new install — infra + secrets + images + Cognito
./deploy apply         # updates — renders, shows plan, applies on confirm
./deploy apply --plan  # preview only — changes nothing
./deploy doctor        # health + drift report;  --capture: live → yaml reverse-gen
./deploy client-values # 4 env lines to hand to employees
./deploy teardown      # delete deployment (scope varies by backend)
```

Lifecycle: **`bootstrap` → `apply` (repeat) → `teardown`**.
Adopting an existing deployment: `doctor --capture` → edit yaml → `apply`.

Non-interactive/CI: `--yes` skips wizards and uses declared yaml values.

---

## 4. Deploy host prerequisites (one-time)

All AWS commands run on a **dedicated deploy EC2** — your laptop needs
only VS Code + Remote-SSH.

1. **Deployer IAM role** `llm-gateway-deployer` — AdministratorAccess +
   SSM managed core + instance profile (create role AND profile
   explicitly via CLI; console auto-creates profiles but CLI does not).
2. **EC2**: Ubuntu LTS x86_64, **t3.xlarge+**, gp3 128GB+, public IP,
   SSH restricted to operator `/32`, IAM instance profile attached.
3. **Bedrock model access**: serverless models auto-enable since
   2025-10; Anthropic models need a one-time **use case form** per
   account (Bedrock console → Model catalog → any Anthropic model).
   Verify with `aws bedrock get-foundation-model-availability`.
4. **Tools**: `deployment/scripts/bootstrap-ec2.sh` installs
   aws-cli/terraform/kubectl/helm/docker/jq/psql idempotently.
   **Restart your remote session afterwards** — docker group membership
   only applies to new sessions.
5. **Deploy tool venv**:
   ```bash
   python3 -m venv docs/NDS/.venv
   docs/NDS/.venv/bin/pip install -r docs/NDS/deploy/requirements.txt
   ```

---

## 5. EKS install — the path SSIR uses

### 5.1 New install (`bootstrap`)

```bash
./deploy bootstrap          # runs init wizard if no yaml, then all steps
./deploy bootstrap --plan   # preview stages
```

Stages (each idempotent — fix and re-run on failure):

| # | Stage | What happens |
|---|---|---|
| 0 | env scaffold | `deploy.tf_env_dir` created from template — **review tfvars AND set `adminApi.adminBootstrap.emails` to real operator emails in `values-eks-fargate-<env>.local.yaml`, then re-run.** Leftover `admin@example.com` becomes the actual admin user in stage 6 |
| ★ | infra wizard | AZs, tfstate bucket/table, Cognito domain suffix, `Claude_*` groups, Bedrock allowed ARNs |
| 1 | tfstate | state bucket + lock table ensured |
| 2 | env apply | terraform: EKS, VPC, Aurora, ElastiCache, Cognito, Secrets, IRSA |
| 3 | secret seed | `/<project>/<env>/app` (4 random keys), `/redis`, `/db` (terraform-owned — never overwritten) |
| 4 | images | 6 services built + pushed to ECR (`images.tag`/`tags` to pin) |
| 5 | helm | same upgrade path as `apply` |
| 6 | Cognito | `adminApi.adminBootstrap.emails[0]` user + `ClaudeAdmin` + first `Claude_*` team group (temp password shown once) |

**After bootstrap:**
- Run `deployment/scripts/smoke-test.sh --env <env>`.
- Model aliases/prices are seeded by init SQL + migrations + the pricing
  Lambda — verify in admin-ui `/models`.
- **Routing profiles**: verify `model.routing_profiles` has
  `account_role_arn`/`external_id`/`default_model` all NULL — migration
  0055 clears the old sample ARNs, but an environment that ran older
  images may still carry them and then **every** request fails
  AssumeRole. Cowork's `default_model` must also be NULL or users can't
  pick a model (§9.3).

### 5.2 Adopt an existing deployment (capture path)

```bash
./deploy doctor --capture --target eks \
  --namespace llm-gateway --release llm-gateway \
  --env-dir deployment/terraform/environments/llm-gateway-<env>
# → deployment/gateway.captured-<env>.yaml
vi deployment/gateway.captured-<env>.yaml   # fill notes
./deploy validate --config deployment/gateway.captured-<env>.yaml
mv deployment/gateway.captured-<env>.yaml deployment/gateway.yaml
```

Capture records per-service image pins, live hostnames (`domain.hosts`),
capacity reverse-engineered into `capacity_tier`/`deploy.sizing`, Route53
zone, tfstate, and infra wizard answers. Secrets are never read —
ExternalSecret names only.

### 5.3 Updates — the everyday path

```bash
./deploy configure      # wizard, ends with "apply now?"
# or edit-then-apply:
vi deployment/gateway.yaml
./deploy apply --plan   # helm server dry-run + live diff + managed-infra plan
./deploy apply          # plan → confirm → helm upgrade --install
```

`--plan` shows changed/added/deleted resources with unified diffs
(helm ≥ 3.13; falls back to client dry-run below). Migrations run as a
pre-upgrade hook Job — a failed migration fails the upgrade before pods
roll.

> The `--plan` output is the contract: **review it every time.** It
> shows HPA changes, ingress diffs, managed terraform overlays, and
> web-search/pricing-lambda provisioning state.

---

## 6. `gateway.yaml` — the fields that matter

```yaml
version: 1
env: prod
aws:
  region: <region>
  account_id: <account-id>

deploy:
  target: eks                   # compose | ecs | eks  (key name is "target")
  size_tier: t3                 # eks requires t2|t3 — a documentation label;
                                # real compute size comes from capacity_tier
  capacity_tier: "3000"         # user-count band → pod min/max (+ Redis pool)
  tf_env_dir: deployment/terraform/environments/llm-gateway-prod
  tfstate_bucket: <bucket>
  tfstate_table: <table>

domain:
  mode: route53-acm             # none | selfsigned-temp | route53-acm
  name: <domain>
  zone_id: <R53 zone id>        # present → cert + DNS fully automatic
  hosts:                        # only if keeping non-derived hostnames
    gateway: gateway-dev.<domain>
    admin_ui: admin-dev.<domain>
    admin_api: admin-api-dev.<domain>

network:
  allowed_cidrs: [<employee egress CIDRs>]
  admin_internal: true          # admin Ingress internal-only (S2S VPN)

oidc:                           # issuer_url configures OIDC; dev login has a separate explicit gate
  issuer_url: https://cognito-idp.<region>.amazonaws.com/<pool-id>
  client_id: <app-client-id>
  authorize_url: https://<hosted-ui-domain>.auth.<region>.amazoncognito.com/oauth2/authorize
  token_url: https://<hosted-ui-domain>.auth.<region>.amazoncognito.com/oauth2/token
  provider_name: oidc:cognito
  groups_claim: custom:groups   # SAML path: attribute-carried groups;
                                # blank → cognito:groups (native pool groups)
  group_prefix: SSIR_
  admin_groups: [SSIR_Claude_Admin]   # AD/SAML groups — separate namespace
  reject_unmatched_groups: false
  vk_ttl_hours: <ttl>

features:
  service_keys: true            # agent/service VK issuance — default OFF
  notifications:
    provider: ses               # mock | smtp | ses | internal_api (no "none" — use mock)
    ses_from: <verified sender> # required when provider=ses

infra:                          # bootstrap wizard answers (new installs)
  azs: [<az-1>, <az-2>]
  cognito_domain_suffix: <suffix>
  cognito_groups: [ClaudeAdmin, Claude_<dept>_<team>]  # POOL groups — must be
                                                      # Claude_* / ClaudeAdmin,
                                                      # NOT the SSIR_* AD groups
  bedrock_allowed_model_arns: [...]

images:
  tag: <git-sha>                # bootstrap auto-injects HEAD sha
  # tags: {service: <pin>}      # per-service overrides
```

Validation is strict — unknown keys, wrong types, non-boolean booleans
are rejected by `./deploy validate` (and by `apply`/`configure`). Two
groups share names but live in **different namespaces**: Cognito pool
groups (`infra.cognito_groups`, always `Claude_*`) for native users vs
AD/SAML attribute groups (`SSIR_*`, carried in `custom:groups`).

---

## 7. Domain & TLS

| mode | What | When |
|---|---|---|
| `none` | HTTP only | Local/test — **Cowork impossible**, no Cognito SSO |
| `selfsigned-temp` | Self-signed cert on admin-ui only (EKS) | Need SSO before a domain exists — browser warning, gateway stays HTTP. **Manual step:** add `https://<admin-ui ALB>/api/auth/callback` + logout URL to the env `terraform.tfvars` `cognito_callback_urls`/`cognito_logout_urls` and `terraform apply` — callbacks are terraform-owned and NOT automated in this mode |
| `route53-acm` | Real domain + ACM + Route53 records | **Production** |

With `zone_id` set, one `./deploy apply` does everything idempotently:
wildcard ACM cert → DNS validation → 443 listeners → `gateway./admin./
admin-api.<domain>` A aliases → Cognito callback URLs merged into gen
tfvars (`module.cognito` targeted apply) → `nextauthUrl` injected.

- **No `zone_id`** (corporate DNS path): request the ACM cert + register
  host records manually; apply picks up the issued cert and warns about
  missing pieces.
- **Domain acquisition** (registration, NS delegation) is out of scope —
  it is an organizational step.
- Adding a domain **breaks existing login sessions** — re-share the new
  endpoints via `./deploy client-values`.

## 8. SSO — Cognito native and SAML/ADFS

### 8.1 Native Cognito users

```bash
POOL_ID=$(terraform -chdir=<env dir> output -raw cognito_user_pool_id)
aws cognito-idp admin-create-user --user-pool-id $POOL_ID \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username user@example.com --group-name Claude_<dept>_<team>   # required for VK
```

| Group | Meaning | Without it |
|---|---|---|
| `Claude_<dept>_<team>` | Team membership — prerequisite for VK issuance | Login works, **VK issuance 403** |
| `ClaudeAdmin` | admin-ui access | admin pages 403 (intended) |

A successful web sign-in provisions the user from the configured group
claim. `gateway-cli login` handles virtual key (VK) issuance. To populate the organization
tree before a user signs in, use **Users/Teams → Sync Cognito**. See
[Sign-in and access](../guides/admin-guide.en.html#access).

### 8.2 ADFS ↔ Cognito SAML (AD integration)

Login-time federation — no user sync needed; attributes arrive per
assertion:

```
login → browser to ADFS → AD auth → signed assertion (email/name/groups)
      → POST to Cognito → JIT user + custom:groups → token → role/team
```

- **The toggle is `oidc.groups_claim`** — set `custom:groups` for the
  attribute path (ADFS/SAML); leave it blank and it is inferred from
  `provider_name` → `cognito:groups` (native pool groups). One declared
  key switches exchange, sync, and session auth together — across all
  three backends (eks helm value / ecs tfvar / compose env).
- **Role resolution:** ADMIN = `oidc.admin_groups` exact match;
  TEAM_LEADER = console flag only (not a group); DEVELOPER = default.
- **Team parsing:** `group_prefix: SSIR_` → `SSIR_<Dept>_<Team>`;
  unmatched names follow `reject_unmatched_groups` (default **true** —
  switching `group_prefix` to `SSIR_` makes native `Claude_*` pool-group
  members "unmatched" and rejected; keep `reject_unmatched_groups: false`
  or grant `custom:groups` to native accounts during migration).
- **Group claim format.** Send multiple gateway group names as one
  `;`-joined string. Cognito serializes multivalued attributes with brackets
  and commas. The current gateway parser expects plain group names.
  [AWS attribute mapping](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-specifying-attribute-mapping.html)
- **Relying party settings.** Give the ADFS administrator the Cognito
  **RP identifier** `urn:amazon:cognito:sp:<pool-id>` and the HTTPS **ACS URL**
  ending in `/saml2/idpresponse`. The administrator console URL is the
  entry point for users. See [AD integration](../ad-sync.en.md) and
  [declarative SAML deployment](../adfs-sso.en.md).
- **Attribute permissions.** Cognito needs a mutable `custom:groups`
  attribute and app-client write access to populate it from the SAML
  mapping. Read access controls its availability in the ID token. The
  repository module currently grants read access only. Review the live app
  client and the declarative configuration before rollout.
- **Provisioning.** After a successful web sign-in, `POST /v1/auth/provision`
  registers or updates the gateway user from the token's group claim. Web
  sign-in leaves existing VKs alone. **Sync Cognito** also reconciles users
  in bulk using their pool records and mapped attributes.
- **Preserve existing admins** before switching `groups_claim`. Test a native
  emergency account with a compatible `custom:groups` value. `admin_emails`
  matches verified email addresses for the ADMIN role and does not supply a team.
- Disabling an account in AD prevents new federation sign-ins. Review active
  Cognito sessions and gateway VKs separately when removing access.

---

## 9. Day-2 operations

The [Administrator Guide](../guides/admin-guide.en.html) provides procedures
and 74 dev captures for sign-in and 13 administrator menus. Use this section
for the operational behavior behind those screens.

### 9.1 User lifecycle

| Task | Where | Notes |
|---|---|---|
| Add user | Cognito account or first federated sign-in, then web or CLI sign-in | **Sync Cognito** can reconcile existing pool users in bulk |
| Team leader | Users/Teams → team node → **Change leader** | Assignment is managed in the console |
| Deactivate | Member node → deactivate | Revokes active VKs in the same transaction |
| Force re-auth | Team/member → **Force re-authentication** | Invalidates all VKs in scope |
| Offboard an AD user | Disable AD access, review the Cognito session, deactivate gateway access | Verify that active VKs are revoked and handle pool record cleanup separately |

**When does a new user appear?** Three routes can create or update the
gateway record:

- **Sync Cognito** (admin-driven) — pool groups **+ full user enumeration**;
  each user's `custom:groups` attribute is read for team/role resolution,
  so SAML users already created in the pool are covered too. Native users can
  appear in the organization tree before their first sign-in.
- **`/v1/auth/provision`** (web sign-in callback) — validates the token, then
  registers the user and resolves the team and role.
- **`/v1/auth/exchange`** (user-driven) — the first `gateway-cli login`
  provisions the account from verified token claims and issues a VK.

If a session predates registration and receives `user_not_provisioned`
(403), sign out and sign in again. If the callback displays **User
registration failed**, check the claim, required groups, and team mapping.
Granular sync endpoints are available for targeted updates
(`sync-cognito/user/{username}`, `sync-cognito/group/{groupname}`).

### 9.2 Keys — two kinds, never mixed

| Kind | Where issued | Lifetime | Display |
|---|---|---|---|
| **User VK** | `gateway-cli login` (browser SSO) | `oidc.vk_ttl_hours` | Once in CLI |
| **Agent/service VK** | Agents page → **Issue long-lived key** — requires `features.service_keys: true` in `gateway.yaml` (default OFF — endpoints 403 and the button is hidden while off; turning it off later blocks only new issuance) | TTL ≤ 365d | Once in dialog — straight to Secrets Manager |

- Issuing on a non-agent account **converts it to an agent** (interactive
  login + short-lived keys blocked) — the dialog warns.
- Use **Agents → Issue long-lived key** to replace a key on an existing agent.
  Issuing the replacement expires the previous key immediately. Prepare the
  application's secret update and verify a request after the switch.
- **Repair auth** re-registers a lost auth mapping — the fix for "401 on
  a still-valid key" (cache eviction, manual DB edit). Agents only —
  user VKs get a fresh `gateway-cli login`.
- Storage: AES-256-GCM encrypted in DB + `key_hash`; plaintext exists
  only in memory and the one-time display.
- **`VIRTUAL_KEY_ENCRYPTION_KEY` loss = all VKs dead.** Back it up in two
  places (§12.4).

### 9.3 Routing profiles — cross-account Bedrock

Two axes, both manageable in the UI:

- **Per-client** (`/apps` → Cross-account routing): `account_role_arn` +
  `external_id` + `region`. NULL = in-account. Verify the rows are NULL
  after install — migration 0055 clears the seed samples, but stale
  environments may still carry ARNs that break every call. UI saves
  invalidate the Redis cache instantly; direct SQL takes up to 5 min
  (300s TTL).
- **Per-model** (`/models` → edit → Routing): same fields on the model
  alias. Precedence: `model_aliases` ARN > client profile > in-account.
  **Fail-closed** — a failed AssumeRole on a model-scoped route is a 503,
  never a silent fallback. UI/API saves invalidate the model cache
  immediately; **direct SQL requires deleting the Redis `model:<alias>`
  key** — the 5-min TTL applies only to client profiles.

Target accounts need an assumable role — trust = gateway execution role
+ ExternalId — with `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`,
`bedrock:CountTokens` (+ `bedrock-mantle:*` for Mantle models), **and**
the gateway-side role needs `sts:AssumeRole` permission on the target ARN
(eks: `modules/irsa` role_arn variables). New Anthropic models are
**Marketplace subscriptions**, not model-access forms — `AccessDenied …
aws-marketplace:Subscribe` means subscribe in the target account.

### 9.4 Budgets & rate limits

- **Team cap** applies to summed member spend; member caps stack under it.
- **Overage policy** per team: Hard Block (4xx at cap) / Warning Only /
  Throttle (reduce RPM). Alert thresholds notify before the cap.
- **Auto-downgrade** redirects a member's requests to a cheaper alias at
  a usage threshold — shown as `downgrade N` on the team row.
- Enforcement is fail-safe: a Redis settle failure leaves the reservation
  over-counted — a temporary early block is possible. Check reconciliation and reserve/settle lag before adjusting limits.
- Rate limits resolve per scope (member > team > org > default) —
  Effective Policy card shows the resolved result.

Match the app shown in the request and the console before troubleshooting a denial. Claude Code and Cowork each use the lower of the user app limit and its corresponding app-group limit. A zero cap explicitly blocks; deleting the group cap removes that group constraint. Codex follows its own policy. Compare web-search organization/team quotas and user overrides separately from model spend, with the same reporting timezone and date window.

#### AICM integration

Check `AICM_API_ENABLED` before testing the `/aicm/v1/admin/*` mirror. Integration clients use scoped service tokens issued by an authenticated administrator via `POST /admin/service-tokens`. Record the client owner, scopes, CIDRs, expiration and rotation sequence. Limit verification to an approved integration environment. Newly issued or rotated credentials should pass one authorized read and fail a request outside their scope. With `AICM_MANAGED_LOCK_ENABLED`, verify field ownership and inspect the audit trail before resolving a 409 by releasing a lock. [AICM contract and migration changes](../upstream-diff/admin-api.en.md)

### 9.5 Models & pricing

Catalog = **aliases** clients send; upstream IDs stay behind the alias.
Registering a new model (admin-ui `/models` — full field reference and
screenshots in [Model Management](../guides/admin-guide.en.html#models)):

1. **Serving-account prerequisite** — the account that will invoke the
   model must be able to. New Anthropic models are **Marketplace
   subscriptions** (Bedrock console → Model catalog → Subscribe),
   propagated in ~2 min — do this *before* registering the alias or
   calls fail `AccessDenied … aws-marketplace:Subscribe`.
2. **+ Add model** — required: alias, provider, upstream model ID
   (e.g. a `global.anthropic.*` inference profile). **The alias is the
   client contract** — clients send it verbatim as the `model` request
   field and the gateway resolves it by exact match, so it must be a
   string the client can actually send: Claude Code/Cowork send
   Anthropic names (`claude-sonnet-4-6`, dated IDs like
   `claude-haiku-4-5-20251001`); codex uses `codex-*` names. Arbitrary
   nicknames register fine but every call fails model-not-found.
   Optional: display name, endpoint URL (provider-dependent),
   description, specs (context window / max output), per-model
   **Routing** override, and the long-context pricing band
   (threshold + 5 price fields).
3. **Pricing** — enter per-1M input/output/cache rates directly, or run
   **Sync Pricing** after registration (below).
4. **Permissions** — expand the row → team-permission panel; the alias
   is not callable until a policy allows it (app policy in `/apps`,
   team policy in the row).
5. **Verify** — one test call through the gateway → confirm it appears
   in Analytics and audit.

**Sync Pricing** (`/models` → *Sync Official Pricing*) — two sources:

| Source | Feed | Requirement |
|---|---|---|
| **AWS Price List** | Official AWS Bedrock pricing | None — always available |
| **3rd-party catalog** (LiteLLM) | LiteLLM canonical cost map (GitHub `model_prices_and_context_window.json`, tag-pinned), fetched via the `llm-gateway-<env>-litellm-pricing` Lambda proxy | `features.pricing_lambda: true` — `apply` auto-provisions role+function+invoke policy. Undeployed → option is **disabled** and the API 503s |

Flow is deliberately read-then-write: pick source → **Preview** (diff
table: current → proposed input/output/cache5m/cache1h/cacheRead +
long-context band rows) → select rows → **Apply selected**. Unmatched
aliases keep their current price — a sync never blanks a price; changing
the source after previewing clears the result and a server-side drift
guard rejects stale applies.

**Deactivate** stops new requests and preserves the catalog and history.
**Delete** removes linked pricing and access settings but keeps usage history.
Deletion is blocked while another model uses the alias as a budget downgrade
destination. Review the impact dialog before entering the alias.

### 9.6 Monitoring, audit, analytics

- **Monitoring**: live 1h window + 24h event log. **Body logging** stores
  full request/response bodies **unmasked** in S3 — a debugging tool,
  not an always-on feature; every view is audited.
- **Dashboards + opt-in alerts** — the Prometheus/Grafana stack collects
  metrics and feeds the HPA; Alertmanager sends gateway/cost alerts to
  Slack with `observability.alerting.enabled` and its per-environment webhook Secret configured (observability.md §10), and
  `features.alerting` adds SNS + CloudWatch alarms for Aurora/
  ElastiCache plus optional Fargate→CloudWatch log shipping through the
  managed overlay. Confirm the SNS subscriptions to receive CloudWatch
  notifications. See [Observability and alerting](../observability.en.md).
- **Cost accuracy alerts** are sent by notification-worker when
  `features.business_alerts` is enabled. Review the target-specific Slack
  configuration before enabling [cost accuracy alerts](../observability.en.md#cost-accuracy-notifications).
- **Audit Log**: every mutation, atomically committed — first stop for
  "who changed X". Body-log views are themselves logged.
- **Analytics**: aggregated reporting (KPIs, cost trend, per-model,
  token mix, export CSV/JSON). Admin sees all teams; team leaders see
  their own.
- **BI Insight** (`/chat`, admin only): 5-agent analysis over usage
  data — plan → approve → run → validated numbers.

### 9.7 What runs automatically — background jobs

Don't be surprised by these — they run on their own schedule (all crons
in `REPORTING_TIMEZONE`, default **Asia/Seoul**):

| Job | Schedule (default) | What it does |
|---|---|---|
| `roi_aggregation` | Disabled by default. `*/15 * * * *` when enabled | Optional legacy job (`ROI_AGGREGATION_ENABLED`). Analytics calculates ROI from usage rollups. |
| `key_expiry` | `0 * * * *` | Expires VKs past TTL — a user's key "randomly" failing is often this |
| `service_key_repair` | `*/15 * * * *` | Auto-heals agent-key auth mappings (what **Repair auth** does manually) |
| `key_purge` | `30 3 * * *` | Deletes expired VKs after 90d retention — **disabled by default** (`KEY_PURGE_ENABLED`); enabling is a retention-policy decision |
| Hourly usage rollup (cost-recorder) | `5 * * * *` | Refreshes hourly usage and cost aggregates |
| `daily_usage_agg` (cost-recorder) | `10 0 * * *` | Daily usage/cost aggregation feeding Analytics |
| `cost_reconciliation` (cost-recorder) | `0 1 * * *` when enabled | Checks the internal usage, cost, and budget ledgers |
| notification-worker | event-driven | Budget alert emails via the configured provider — verify `ses_from`/recipient ownership |

### 9.8 Recurring ops cadence

| Cadence | Task |
|---|---|
| Daily | `doctor` HIGH findings, queue-depth/503 glance (§10) |
| Weekly | Audit log review; budget alert follow-ups |
| Monthly | Model pricing sync check (`/models`); capacity band review vs signals |
| Quarterly | DR rehearsal (§12.5); stale `ADFS_*` pool records cleanup; access review (`ClaudeAdmin` membership) |
| On event | New-model Marketplace subscription; upstream sync (`upstream-sync.md`) |

For an incident, capture environment, first occurrence and timezone, affected clients and user scope, a request ID, and the last release or policy change. Compare the symptom with service logs, metrics and the audit trail. For a spend discrepancy, check the reporting window, model and search components, stream lag, worker health, hourly/daily rollup status and any estimated or backfilled rows. Verify restored traffic and downstream cost recording before closing the incident. [Operations runbook](../operations.en.md)

### 9.9 Employee onboarding — final gate

Before opening to employees (see the [pre-onboarding checks](../operations.en.md#before-onboarding-employees)):

- [ ] OIDC login redirects to Cognito — **dev-login must be off**
  (`DEV_LOGIN_ENABLED=false`). Verify a complete OIDC configuration and
  successful signed-token login before enabling employee access.
- [ ] `network.allowed_cidrs` = employee egress ranges (not `0.0.0.0/0`,
  not the installer's `/32`). This value reconciles to ALB SG ingress —
  **never edit the SG directly.**
- [ ] admin console narrower still — `network.admin_internal: true`
  or admin ingress CIDRs.
- [ ] Domain/TLS attached (`route53-acm`) — Cowork requires HTTPS.
- [ ] `infra.bedrock_allowed_model_arns` reviewed.
- [ ] `oidc.vk_ttl_hours` per policy.
- [ ] Backups verified (§12); `VIRTUAL_KEY_ENCRYPTION_KEY` escrowed.
- [ ] Hand employees [client-setup](../client-setup.en.md) + `./deploy client-values`.

---

## 10. Capacity operations

`capacity_tier` is the only dial you normally touch
(`deploy.sizing` for surgical overrides). Signals for moving a band:

| Signal | Meaning | Action |
|---|---|---|
| `gateway_bedrock_executor_queue_depth > 0` sustained | Slot saturation — CPU-HPA is blind to it (SSE holds threads without burning CPU) | Band up or `min_replicas` up |
| `MaxConnectionsError` | Redis pool exhaustion | `deploy.sizing.redis.pool_size` |
| `budget_deduct_failed` / `budget_lost_sentinel_failed` | Budget accounting Redis failures (self-healing, but pressure signal) | Pool size / node type |
| TTFB up + 503s | Direct slot-starvation symptom | Band up |
| `gateway_bedrock_executor_inflight` peak < 30% of slots | Over-provisioned | Band down (keep min ≥ 2 for HA) |

- Executor saturation is measured by a custom metric → Prometheus →
  adapter → HPA (`executorInflightTarget`). Without the observability
  stack it stays `<unknown>` and only CPU-HPA runs.
- Validate capacity claims with `loadtest/sse_loadgen.py` — real
  concurrency, not estimates (see [loadtest](../loadtest.en.md)).

---

## 11. Updates, rollback, teardown

- **App updates:** `gateway.yaml` edit → `./deploy apply --plan` →
  `apply`. Image pins via `images.tag` / `images.tags`.
- **Infra modules:** split ownership — `body_logging`/`irsa`/`cognito`/
  `agentcore_runtime` are applied by `deploy apply`'s managed overlay;
  **everything else (vpc/eks/aurora/elasticache) is human-owned** —
  `terraform plan/apply -var-file=<env>-generated.tfvars -target=module.<name>`
  in the env dir. The generated var-file carries the bootstrap wizard's
  answers — running without it can show false drift or revert managed
  values.
- **Migrations are forward-only** — no DB rollback. Roll back by pinning
  the previous image tag; schema stays forward.
- **Cluster/Aurora engine upgrades:** [upgrade-eks-cluster](../upgrade-eks-cluster.en.md) runbook.
- **Teardown:** `./deploy teardown` (app only) /
  `teardown --infra` (app + terraform env infra, with preview + confirm).

---

### Release and rollback evidence

Record the prior tag per workload, migration revision, rollout owner, rollback trigger and test identities. Review `./deploy apply --config "<config-path>" --context "<eks-context>" --plan` for replacement, environment drift, permission changes and image differences. After applying, verify migration completion, Pod/target health, gateway 5xx and time to first token, and worker lag. Then run an administrator sign-in and one approved employee request. In EKS:

```bash
kubectl --context "<eks-context>" -n llm-gateway rollout status deployment/gateway-proxy
./deploy doctor --config "<config-path>" --context "<eks-context>"
./deployment/scripts/smoke-test.sh --env "<dev-or-prod>" --context "<eks-context>"
```

Add `--with-bedrock` when the test is approved to make a live model invocation. If a rollback trigger fires, gather failed-workload and migration evidence, pin schema-compatible prior images, and repeat the plan and verification. An earlier image alone cannot revert migrated data.

## 12. Disaster recovery

### 12.1 What survives

| Asset | Where | Loss impact |
|---|---|---|
| DB (keys/users/usage/budget/audit) | Aurora (ecs/eks), pgdata volume (compose) | **Fatal** — PITR/restore |
| `gateway.yaml`, `.env` | Deploy host + Secrets Manager | Regenerable — except the VK key |
| `VIRTUAL_KEY_ENCRYPTION_KEY` | `.env` / Secrets Manager | **All VKs undecryptable** |
| Images | ECR | Rebuild/re-push |
| Redis | ElastiCache | Volatile — rebuilds itself |
| Cognito pool | terraform + AWS | Rare — users/groups are data, not terraform |

### 12.2 RPO/RTO

| Backend | RPO | RTO |
|---|---|---|
| compose | 24h (pg_dump cron) | hours |
| ecs/eks | ~5 min (Aurora PITR, retention dev 7d / prod 14d) | 30–60 min |

These are planning figures from the existing deployment guidance. At handover, measure achievable recovery time and verify retention and recoverable time ranges from the actual environment. Record which workloads use the direct cluster endpoint or RDS Proxy. A restore rehearsal should capture the recovery point, loss window, connection cutover and time to successful login, gateway request, usage recording and audit retrieval.

### 12.3 Aurora restore (eks/ecs)

PITR creates a **new cluster** — never overwrites:

1. `aws rds restore-db-cluster-to-point-in-time` → new cluster id.
2. `aws rds create-db-instance` to attach an instance.
3. **RDS Proxy deployments: repoint the proxy target first** — prod eks
   reads `rds_proxy_endpoint`, so the proxy target is what must change
   before apps see the new cluster.
4. Update the `/llm-gateway/<env>/db` secret's `host` to the new
   endpoint → restart pods (eks: `rollout restart`; ecs:
   `update-service --force-new-deployment`).
   ⚠️ This is the **one deliberate exception** to "the `/db` secret is
   terraform-owned — never hand-edit" (§5.1, §14). After recovery, point
   the env terraform at the restored cluster (or run the equivalent
   apply) — otherwise a later `terraform apply` can revert the endpoint.
5. PITR window expired → `restore-db-cluster-from-snapshot` (prod keeps
   a final snapshot on delete).

### 12.4 `VIRTUAL_KEY_ENCRYPTION_KEY` loss

There is **no recovery** — reissue everyone:

```bash
openssl rand -hex 32   # new key → eks: /llm-gateway/<env>/app secret,
                       # then rollout restart; compose: .env
# revoke all ACTIVE VKs (admin API or SQL), users re-run gateway-cli login
```

Agent/service keys must be **re-issued too** — and every Secrets Manager
entry holding their plaintext (§14 inventory) updated.

Prevention is everything: keep the key in the team vault **and** a
second escrow location.

### 12.5 Rehearsal (quarterly)

- Restore the latest backup to a throwaway cluster/DB — untested backups
  don't count.
- Verify `auth.users`/`model.model_aliases` row counts, run
  `smoke-test.sh` / `doctor`.

---

## 13. Troubleshooting quick map

| Symptom | First check |
|---|---|
| Every request fails AssumeRole | `model.routing_profiles` or `model.model_aliases` carrying non-NULL ARNs intended for another account → NULL for in-account (§9.3) |
| 401 on a valid agent key | **Repair auth** on Agents page |
| VK issuance 403 for a user | Missing `Claude_*` team group |
| admin-ui 403 or user registration fails | Verify the configured group claim, team mapping, and page role. For `user_not_provisioned`, sign out and sign in again (§9.1). |
| Login loops / SSO broken | Cognito callback URLs vs actual hostnames (`domain.hosts`, `nextauthUrl`) |
| Model call `AccessDenied … Subscribe` | Marketplace subscription missing in the **target** account |
| Slow + 503 under load | Executor queue depth → capacity band (§10) |
| `deploy doctor` HIGH findings | Read the item — drift, version skew, migration job failures are all reported with remediation |

---

## 14. Handover checklist — what transfers beyond documents

Documents live in git; these do not. Verify each item is delivered and
received — an unchecked row here is a future incident.

### Values & state (cannot be committed — hand over separately)

- [ ] Real `deployment/gateway.yaml` + `values-eks-fargate-<env>.local.yaml`
      from the deploy host — committed copies are placeholders
- [ ] `terraform.tfvars` per env dir (gitignored — real account IDs,
      CIDRs, ARNs)
- [ ] tfstate bucket/table names + access
- [ ] Cognito pool ID, client IDs, hosted UI domain
- [ ] `./deploy doctor --capture` snapshot at handover time — the
      baseline of live state

### Access

- [ ] Deploy EC2: SSH key, instance ID, IAM instance profile
- [ ] AWS account(s): console/role access for SSIR operators
- [ ] Cognito admin account for admin-ui — or SSIR admin accounts
      mapped via `custom:groups` (§8.2)
- [ ] Repository access — `nds/dev` branch + `upstream-sync.md` process

### Secrets escrow

- [ ] `VIRTUAL_KEY_ENCRYPTION_KEY` — stored in **two** places; loss
      means every VK is dead (§12.4)
- [ ] `/llm-gateway/<env>/db` and `/app` secret paths — note they are
      terraform-owned; never overwrite the db secret manually

### People & coordination

- [ ] SSIR-side ADFS administrator (SAML federation counterpart)
- [ ] DNS/domain owner
- [ ] Network team — owner of `allowed_cidrs` egress ranges

### Live inventory

- [ ] Active service-agent keys + the Secrets Manager paths holding
      their plaintext
- [ ] Marketplace subscriptions currently active
- [ ] Current `capacity_tier` and observed headroom (§10 signals)

### Operations ownership

- [ ] **Observability stack** — `deployment/observability/` (Prometheus,
      Grafana, otel-collector, prometheus-adapter), installed manually
      via `install.sh` — **not** part of the managed overlay. The
      executor-saturation metric feeding HPA depends on it. Confirm it
      is deployed and who owns/watches Grafana
- [ ] **Alerting** — `features.alerting` (managed overlay) creates the
      SNS topic + Aurora/ElastiCache CloudWatch alarms + RDS event
      subscription, and `log_shipping` routes container logs to
      CloudWatch (eks). Email subscriptions need a manual confirm —
      verify they are ACTIVE, not PendingConfirmation
      ([AWS alerts and logs](../observability.en.md#aws-alerts-and-logs)). ALB 5xx/UnHealthyHost
      alarms are auto-discovered by `deploy apply` (eks) — verify they
      exist after the apply following ingress creation
      (see the AWS alerts runbook). Pod/app-level alerts: in-cluster Alertmanager + gateway/cost
      PrometheusRules ship with the observability stack; enable the
      observability alerting flag and create `alertmanager-slack-<env>`
      ([EKS metrics and Alertmanager](../observability.en.md#eks-metrics-and-alertmanager)). For each environment, record the observed Prometheus retention and storage. The development baseline is 24h with ephemeral storage.
- [ ] **Notification channel** — `features.notifications.provider`
      (ses/smtp/internal_api): sender address ownership, who receives
      budget-alert emails
- [ ] **AWS-level cost** — account billing alerts are separate from
      gateway budgets; decide who watches the Aurora/ElastiCache/ALB bill
- [ ] **Change process** — which branch prod is built from (`nds/dev`),
      who reviews `apply --plan` diffs, whether a dev/staging stack is
      kept for pre-prod applys
- [ ] **Log locations** — CloudWatch log groups / `kubectl -n
      llm-gateway logs` per service; which team monitors them
- [ ] **Deploy EC2 succession** — the deploy host holds the real yaml +
      `.local.yaml` + gen outputs; treat it as an asset to back up or
      document rebuild steps for (its loss is recoverable via
      `doctor --capture` but costs time)

### Known limitations & deferred items

- [ ] EKS API endpoint access — record live public/private settings and
      allowed CIDRs, the owner of any exception, and its review date
- [ ] `bi_insight` chat agent — opt-in via `features.bi_insight`;
      when enabled, `deploy apply` provisions its infra through the
      managed overlay (`module.agentcore_runtime`); off by default
- [ ] Single-region design — full region loss is out of scope (§12.2)
- [ ] Migrations are forward-only — no DB rollback (§11)
- [ ] Body logging stores unmasked payloads — enable for debugging only
- [ ] ECS is a supported target. Record whether it is used in the environments covered by the handover
- [ ] **policy-helper / AI Governance Hub (US-21) — deferred to the
      NDS repo restructure.** gateway-proxy ≥ `1.0.91` already serves
      `GET /v1/policy`, but nothing calls it until the PC helper is
      installed. Enabling it would give per-user/team `/model` lists
      (21-A) and, with the separate
      [ai-governance-hub](https://github.com/gonsoomoon-ml/ai-governance-hub)
      repo, team-managed plugins/MCP/locks with approvals (21-B).
      Adopting it under NDS means porting the us-deploy procedure
      ([8-G-policy-helper](../../us-llm-gateway/ops/8-G-policy-helper.md))
      into `gateway.yaml` + the managed overlay, plus deciding whether
      the hub stays a separate repo — parked by agreement.

### Environment handover record

| Field | Evidence to attach |
|---|---|
| Access and routing | Account, Region, identity used to deploy, EKS context/namespace or ECS cluster, gateway/admin hostnames, DNS/SSO/network owners |
| Release and rollback | Current and prior image per service, Pod image IDs or task revisions, migration result, deployment plan and rollback trigger |
| Recovery | Backup timestamp, recoverable range, key escrow attestations, restore run time and validated queries |
| Incident coverage | On-call contacts, provider escalation paths, first response expectations, per-environment alert route test and metrics retention |
| Ownership and exceptions | Open issues, owner, review date and agreed follow-up. Link to restricted records that contain private configuration |

### Acceptance — SSIR's first week

- [ ] `./deploy doctor` on the live env → no unexpected HIGH findings
- [ ] `deployment/scripts/smoke-test.sh --env <env>` passes
- [ ] One Aurora PITR rehearsal to a throwaway cluster (§12.3)
- [ ] One test user onboarded end-to-end: Cognito group → sync → CLI
      login → VK → a live request
- [ ] If `features.alerting` enabled: SNS subscriptions all ACTIVE,
      one `set-alarm-state` test delivered
      ([AWS alerts and logs](../observability.en.md#aws-alerts-and-logs))

---

Add a separate acceptance record for each environment. Include service images and flags, verified DB restore point, declared retention, test user roles, configuration/state locations, the owner of each alert route, and the incident escalation channel. Keep credential values in the approved vault. Carry unresolved entries forward with a named owner and due date.

## 15. Where the details live

| Need | Document |
|---|---|
| Deploy host setup | [install-prereq](../install-prereq.en.md) |
| EKS install/update/adopt | [install-eks](../install-eks.en.md) ([install-compose](../install-compose.en.md), [install-ecs](../install-ecs.en.md) for other backends) |
| Full `gateway.yaml` reference | [gateway-yaml](../gateway-yaml.en.md) |
| Domain modes | [domain](../domain.en.md) |
| ADFS/SAML deployment and user registration | [SAML deployment](../adfs-sso.en.md), [AD integration](../ad-sync.en.md) |
| Ops runbook depth | [operations](../operations.en.md) |
| Update/rollback/teardown | [update](../update.en.md) |
| Cluster/engine upgrades | [EKS and Aurora upgrades](../upgrade-eks-cluster.en.md) |
| Legacy EKS migration reference | [Pre-adoption EKS procedure](../upgrade-eks-legacy.en.md) |
| DR | [dr-runbook](../dr-runbook.en.md) |
| Observability & alerting | [observability](../observability.en.md) |
| Load testing | [loadtest](../loadtest.en.md) |
| BI chat agent | [chat-agent](../chat-agent.en.md) |
| Administrator procedures and screenshots | [English HTML guide](../guides/admin-guide.en.html), [Korean guide index](../guides/README.md) |
| Employee setup | [client-setup](../client-setup.en.md), [client-admin](../client-admin.en.md), [cowork/](../cowork/README.en.md) |
| Design rationale | [decisions](../decisions.en.md) |

---

*This guide is generated from the NDS deployment model. If a procedure
here disagrees with the tool, the tool is right — `gateway.yaml` and
`./deploy` are the source of truth.*
