# Agents

**Route:** `/agents` · **Role:** Administrator
*(also serves `/service-accounts`, which redirects here)*

Long-lived virtual keys for server-side apps and agents — identities
created in Cognito, then issued and maintained here.

![Agents](screenshots/09-agents.png)

## Issue long-lived key

**Issue long-lived key** opens a dialog:

1. Pick the **target user** — an existing account that will own the key
   (accounts themselves are provisioned via Cognito, not created here).
2. Confirm — the plaintext key is shown **once** in a copyable block.
   Store it in Secrets Manager immediately; it cannot be retrieved again.

## List actions

| Action | Effect |
|---|---|
| **Repair auth** | Re-registers a key's lost auth mapping. Use when an app gets **401 while the key is still valid** — e.g. after a cache eviction or manual DB edit. |
| **Unmark** | Revokes the account's live service key, then drops the agent marker — the UI blocks unmarking while a key is still valid, so the two happen together. |

Click a row to open the agent's [detail view](#agent-detail).

## Agent detail

**Route:** `/agents/{userId}` · per-agent usage and key inventory.

- **Usage KPIs** — cost, request count, tokens, and last-request latency
  for the selected period.
- **Keys** — every long-lived key on the account: prefix, status,
  issued/expires timestamps, last request, per-key requests and cost.
- **Usage by model** — cost share across the models this agent called.

Use this view to confirm a repair worked — a healthy key has a recent
**last request** and non-zero counters.

## Operational notes

- Rotate keys by **unmarking and re-issuing**: Unmark revokes the live
  key (and the marker), then Issue long-lived key creates a fresh one on
  the same account. Update the secret in Secrets Manager between the two
  steps so the app does not lose access.
- `Unmark` is a label only; keys and history survive it.
- Audit entries for issue/revoke/repair are under
  [Audit Log](audit-log.md) category *API key*.
