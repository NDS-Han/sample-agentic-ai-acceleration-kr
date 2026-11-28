# NDS Gateway changes since upstream

[한국어](README.md) · **English** · [Operations handover](../op-guide/README.md)

## Comparison range

- Fixed upstream base: `e3a3975745716bc7413c125a50a6bac3fcd94c95`, upstream PR #17 merged on 14 July 2026 KST.
- NDS endpoint: `98e825a011a79f56b89bb89ef61ffe95d612f15b`, the last commit on `codex/nds-bilingual-docs` before this report, on 28 November 2026 KST.
- On 28 November, the upstream default branch resolved to the fixed base and the remote `origin/nds/dev` resolved to `93d502c6`. The local `nds/dev` resolved to `de4909bc`.

| Scope | Commits | Changed files | Lines added | Lines removed |
|---|---:|---:|---:|---:|
| Entire repository at the fixed endpoint | 1,638 | 2,135 | 399,066 | 16,830 |
| Gateway project at that endpoint | 1,554¹ | 1,849 | 335,000 | 16,830 |
| Remote `origin/nds/dev`, gateway file totals | 1,635² | 1,828 | 334,639 | 16,830 |
| Local `nds/dev`, gateway file totals | 1,637² | 1,811 | 333,967 | 16,830 |

¹ Path-filtered commit history. ² Full repository history for that endpoint. The full fixed range includes 90 merge commits. Counts exclude the base itself. File and line counts compare the two trees, so they include code, tests, docs and generated specifications at the endpoint. Binary changes count as files.

## Net changes by component

The following file and text counts group `git diff --numstat` by path in the gateway project. `docs/` includes documentation, captures, the deployment CLI and its tests.

| Path | Files | Added | Removed |
|---|---:|---:|---:|
| `docs/` | 349 | 69,873 | 1,202 |
| `admin-ui/` | 324 | 49,693 | 7,478 |
| `gateway-proxy/` | 251 | 60,573 | 2,872 |
| `admin-api/` | 236 | 60,803 | 2,555 |
| `deployment/` | 152 | 16,384 | 1,325 |
| `installer/` and `cowork-installer/` | 156 | 31,676 | 0 |
| Notification and cost workers | 120 | 19,950 | 511 |
| `db/` | 75 | 8,266 | 162 |
| `gateway-cli/`, `policy-helper/`, `gateway-clients/` | 53 | 4,246 | 395 |
| Chat agent, pricing Lambda, load test and mock | 39 | 2,911 | 182 |

## What changed

| Area | Detail |
|---|---|
| [Deployment](deployment.en.md) | A common declarative model for Compose, ECS and EKS. Adds live capture, preview, scoped updates, SAML settings and operating validation. |
| [Identity and credentials](identity.en.md) | Web sign-in can provision a user without issuing a VK. CLI exchange, bulk sync, agent credentials and separate integration tokens serve distinct workflows. |
| [Administration API and schema](admin-api.en.md) | App and group budget settings, web-search quotas, protected AICM operations, ownership of individual fields, rollups and reconciliation migrations. |
| [Gateway request path](gateway-proxy.en.md) | Effective client and model policy, model and app ARN routing, search limits, budget reservation and settlement, degraded operation and cost durability. |
| [Administrator console](admin-ui.en.md) | App policy, agents and Audit Log workflows, organization policy inspection, cost and budget UI, localized views and lifecycle confirmations. |
| [Client distribution](clients.en.md) | Windows and Linux distributions, the Cowork installer and managed profile, environment values and policy-helper integration. |
| [Workers and observability](workers-aux.en.md) | Usage and cost rollups, replay and reconciliation, cost alerts, BI Insight, pricing sources and load testing. |

The gateway project already had the core UI, API, proxy, CLI, Cognito OIDC, Codex routes, workers, and deployment directories at the base. The comparison describes how NDS extended these components. Added top-level items include the deployment entry point, installers, policy helper, pricing Lambda, mock service and streaming load tests.

## Integration milestones

| Commits | Change |
|---|---|
| `5faffc30`, `9664a2c3` | Introduced declarative deployment and consolidated the tool under `docs/NDS` |
| `01b4b8d3`, `535684f6`, `a23e3932`, `92d64e0a` | Web-search provisioning, Route 53 and ACM workflow, and EKS/ECS capacity bands |
| `033d0632`, `1ab19b29`, `81f8d8c6` | Configurable OIDC groups, web provisioning, and the EKS upgrade workflow |
| `7105fa70`, `e3e266d1`, `13912dbe`, `d8bddad4` | Body-log viewer, user-model analytics, web-search costs, and quotas |
| `87ff960b`, `11aad69c`, `23c0c6c8`, `93561142` | Cost precision, durable recording, reconciliation tuning, and web-search backfill |
| `fafbf571`, `e5937270`, `3fb446f8`, `e47702c9` | Budget degradation, proxy isolation, model cache and startup warmup |
| `26ba62b5`, `dab69252`, `7407f84d` | Redis hot path, administration queries and worker batching |
| `e301ee8e`, `df3731a5`, `92e6178e`, `8040b1d5` | AICM ownership, legacy route removal, enforced app-group budgets, and token/settlement regression fixes |
| `33ee0cfc`, `31072728`, `85f608a9`, `6d382cd4` | Alertmanager gate, cost alerts across all targets, ADFS setup and declarative SAML provider details |
| `288660c5`, `de4909bc`, `98e825a0` | Current console captures, operations guidance and bilingual indexes |

## Reproduce the comparison

From the repository root:

```bash
git rev-list --count e3a39757..98e825a0
git log --first-parent --oneline e3a39757..98e825a0
git diff --shortstat e3a39757..98e825a0 -- projects/awsome-ai-gateway
git diff --name-status e3a39757..98e825a0 -- projects/awsome-ai-gateway
```

The deployed development images have separate version pins. On 28 November 2026 KST, admin UI used `4fe6d927`, admin API, proxy, cost recorder and scheduler used `8040b1d5`, and notification-worker used `62493ba1`. Verify the service image and environment flags before applying the committed changes to an operational workflow. [Operations handover](../op-guide/README.md)
