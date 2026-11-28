# Administration API and data contracts

[한국어](admin-api.md) · **English** · [Comparison index](README.en.md)

## Mounted capabilities

Eight router modules were added. `apps.py` manages defaults and model access, `audit_reconcile.py` checks audit records, `body_logs.py` manages restricted body-log access, `service_accounts.py` handles long-lived service credentials, and `settings.py` exposes operating settings. `web_search_quota.py` covers search policy and usage, `aicm_admin.py` mounts the integration mirror, and `managed_fields.py` manages field ownership. See the [application's router registration](../../../admin-api/src/app/main.py).

Existing endpoints grew full and per-entity Cognito sync, effective organization/team/user policy, model context pricing and account routing, official pricing previews, client and date-aware analytics, usage and audit queries. They also gained post-commit cache invalidation and request ID propagation. The token contract regression fixes shipped in `8040b1d5`.

## AICM administration boundary

- `AICM_API_ENABLED` gates `/aicm/v1/admin/*`. Its allowlist contains 26 policy entries and 25 unique method/path pairs. Two entries apply different scopes to the same budget summary path.
- Human administrators mint scoped `svc-` credentials via `POST /admin/service-tokens`. The mirror rejects general service tokens and human JWTs. Ordinary admin routes reject the scoped integration tokens. Check scope, CIDR, rate limits, expiry and rotation.
- The old `itg-` credentials and legacy issuance, provisioning and deactivation paths were removed. Confirm the current contract and replace affected credentials before cutover.
- The mirror restricts writes against protected administrator, system and service accounts. Batch user spend seeding is atomic and audited.
- With `AICM_MANAGED_LOCK_ENABLED`, a human edit to a managed field can return a conflict. Review ownership and the `RELEASE_AICM_MANAGED_FIELD` audit action before release.

[Mirror allowlist](../../../admin-api/src/app/routers/aicm_admin.py) · [Managed-field router](../../../admin-api/src/app/routers/managed_fields.py) · [Integration client contract](../aicm/README.md)

## Budget and schema

| Revisions | Change |
|---|---|
| 0024–0057 | Unique and lookup indexes, model catalog, routing and long-context prices, app restrictions, team default user caps, notifications, team source, service accounts, key hashes, usage sessions and BI reports |
| 0058–0061 | Search costs and policies, v2 daily, monthly and hourly rollups and dirty marks |
| 0062–0063 | Cost precision and durability |
| 0064–0065 | Scoped integration tokens and idempotency schema. The final mirror does not use the idempotency table |
| 0069–0071 | Cost-alert configuration, app-group budgets and AICM field ownership |

There are 45 more numbered migration files, from 27 to 72. The new range is 0024–0071 with 0066–0068 absent at the endpoint. Claude Code and Cowork group caps interact with their respective user app caps. Zero is a block and deleting the group cap removes that constraint. Record the schema revision and prior image compatibility before a change. [Migration files](../../../db/versions) · [Operations handover](../op-guide/README.md)
