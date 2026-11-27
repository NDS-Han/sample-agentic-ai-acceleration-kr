# AICM integration guide — `/aicm/v1`

`aicm-v1-openapi.json` in this directory is the machine-readable contract. The surface is
mounted only when `AICM_API_ENABLED=true`.

> **환경 결정(2026-11-26):** AICM 통합 시험은 내부(prod) 환경에서만 한다. dev 는 공개 환경이므로
> `admin-api: AICM_API_ENABLED=false` 를 영구적으로 유지한다. dev 에서는 플래그가 꺼진 상태(404/비마운트)만
> 검증하고, 어떤 검증 목적으로도 dev 에서 이 플래그를 켜지 않는다.

## Option A admin mirror

AICM only changes its gateway client base URL:

```text
apiBase = https://<gateway-host>/aicm/v1
```

The existing paths stay `/admin/...` underneath. Exactly the allowlisted contract endpoints in
`aicm-client-contract.json` are exposed as `/aicm/v1/admin/...`; no other admin path is mounted.
The mirror reuses the same handlers and schemas as `/admin`, so request bodies, response bodies,
status codes (including 204), error envelope, query parameters, and `x-request-id` propagation match.

Notable semantics:

- `[]` in allowed-clients/allowed-models means allow-all (the existing gateway semantics).
- `max_budget_usd` accepts up to four decimal places.
- Team budget `"unlimited"` is accepted for `PUT /admin/budgets/team/{team_id}`. It is stored as
  the maximum `NUMERIC(12,4)` cap (`99999999.9999`) so the gateway hot path is unchanged; in
  practice this unlocks team-level blocking. Do not use that literal as a normal cap.
- `PUT /admin/budgets/spent` is an absolute cumulative seed. Reconciliation treats successful
  `SEED_BUDGET_SPENT` audit deltas as expected ledger input, so a correct seed does not become a
  reconciliation mismatch.
- App-group budgets are advisory control-plane records only: they are stored and returned, but are
  not enforced by gateway-proxy and are not included in spend reservations. This is intentional for
  this deployment because only Claude Code is permitted; Cowork and Codex are not permitted. To
  restrict or revoke access, use `allowed-clients`, the per-app budget for `claude-code`, or the user
  total budget.
- `GET /admin/budgets/summary/team/{team_id}/users?period=YYYY-MM&limit=10000` is the single-page
  team member summary endpoint.
- `POST /admin/service-tokens/rotate` is self-rotation and must never be retried automatically.
  The mirror does not use the idempotency framework; all other contract mutations are naturally
  idempotent absolute-set operations, which is what makes the client retry policy safe.

## Authentication and token lifecycle

Mirror calls use:

```text
Authorization: Bearer svc-<64 hex lowercase chars>
```

This is not a generic service token. It must be an AICM-scoped integration token issued by a human
admin through `POST /admin/service-tokens` with a `scopes` array (human admin JWT; the legacy
`/aicm/v1/service-tokens` bootstrap route was removed). Valid scopes: `users:read`, `users:write`,
`users:sync`, `budgets:read`, `budgets:write`, `models:read`, `tokens:read`, `tokens:rotate`.
`allowed_cidrs` and `rate_limit_per_min` may be set at issuance only together with scopes.

AICM-scoped `svc-` tokens are accepted only on `/aicm/v1/admin/*` contract routes. They are rejected
on every real `/admin/*` route. Generic `svc-` tokens and human JWTs are rejected on the mirror.
Rotation preserves the AICM scopes, CIDR restrictions, lineage, and `svc-` prefix. Token issuance
remains human-admin-only; service tokens cannot issue new service tokens.

> **Legacy removal:** the phase-0a lifecycle routes (`/aicm/v1/users/provision`, `/deactivate`,
> legacy `/aicm/v1/service-tokens` operations) and the stripped-path legacy surface were removed
> (they had zero consumers — AICM manages Cognito directly via AWS SDK and calls only the mirror).
> Legacy `itg-` tokens no longer authenticate anywhere.

## Rate limits and retries

Per-token-lineage fixed-window rate limits are enforced in read/write/sync classes. HTTP 429 responses
carry `Retry-After`. Recommended client behavior is the contract behavior:

- 401: refresh/rotate credential state, then retry once.
- 408, 429, or 5xx: exponential backoff, at most three tries.
- Never retry `POST /admin/service-tokens/rotate` automatically.
- Propagate `x-request-id` on every call.

Cognito sync endpoints can be long-running. The service batches commits, uses a PostgreSQL advisory
lock for whole-pool sync mutual exclusion, and database command timeouts bound individual statements.

## Error format

Errors use the admin-api envelope:

```json
{"error":{"type":"forbidden","code":"insufficient_scope","message":"token lacks required scope"}}
```

Validation failures use the same envelope with field details where available. Mirror calls to a path
outside the contract allowlist are not mounted (404); a valid integration token used on the wrong
AICM surface receives 403 `integration_surface_forbidden`.

## Admin/system target protection

AICM cannot modify gateway ADMIN users, service accounts, or system users through user or user-budget
mutations, including USER-scope `PUT /admin/budgets/spent` seeds. A batch containing any protected
USER scope is refused atomically before budget tables or Redis are touched. Refusals return 403 and
are audited (`AICM_ADMIN_TARGET_REFUSED`). Reads are not target-guarded: the contract needs org-wide
lookup for reconciliation, so protection applies to mutations only. Cognito sync does not demote,
deactivate, or alter admin-group/admin-account users through the sync-path lifecycle protections.
