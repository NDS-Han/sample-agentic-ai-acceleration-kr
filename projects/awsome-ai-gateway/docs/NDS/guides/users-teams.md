# Users/Teams

**Route:** `/users` · **Role:** Administrator
*(also serves `/rate-limits`, which redirects here — rate limits are
configured per scope on this page)*

Organization browser and per-scope policy editor. The organization tree
(organization → department → team → member) is **sourced from Cognito
groups** — structure changes are made in the Cognito console and pulled
in with **Sync Cognito**.

![Users/Teams](screenshots/07-users.png)

## Header controls

| Control | Effect |
|---|---|
| **Sync Cognito** | Pulls users/groups from Cognito into admin-api. Runs the fail-closed reconciler — incomplete fetches deactivate nothing. |
| **Search email · name · team** | Filters the tree to matching nodes. |
| **Show empty teams** | Includes teams with zero members. |

**Sync Cognito** asks for confirmation before running — it pulls all
groups and user details from the Cognito User Pool into the local DB:

![Sync Cognito confirmation](screenshots/44-users-sync-result.png)

## Organization tree (left pane)

Expanding the tree shows the hierarchy — organization, departments,
then teams with their member counts:

![Expanded organization tree](screenshots/42-users-org-expanded.png)

Select any node to load its detail panel:

- **Organization / Department / Team** — member counts, app access
  policy, and scope-level settings.
- **Member** — user detail including role, team, and effective policy.

## Detail panel (right pane)

For an organization node the panel shows counts and the **App access
policy** card:

- Chips for **Claude Code / Cowork / Codex**.
- *Unrestricted* = no organization-level restriction (all chips selected
  or none selected behaves the same).
- Selecting a subset restricts every user under the node who has no more
  specific team or individual override.

A **team node** shows the full per-team editor:

![Team detail panel](screenshots/43-users-team-detail.png)

- **Current team leaders** — list with **Remove** per leader, plus a
  member picker and **Set as team leader** to designate another. Team
  leader is a console flag, not a Cognito group.
- **App access policy** — per-scope allowlist of client apps. More
  specific scopes override the organization default.
- **Team Model Access Management** — which catalog aliases the team may
  call (`N allowed`).
- **Rate limit** — request/token limits; *Unrestricted* inherits the
  organization default.
- **Effective Policy** — read-only resolved result of every layer for
  this scope, so you can verify what actually applies.
- **Budget** — read-only pointer to [Budget Management](budgets.md).
- **Force re-authentication for team** — invalidates every member's
  active virtual keys; users must run `gateway-cli login` again.

Team and member nodes additionally expose:

- **App access** (`ScopeAppAccessPanel`) — per-scope allowlist of client
  apps. More specific scopes override the organization default.
- **Rate limits** (`ScopeRateLimitPanel`) — request/token limits per
  scope; teams can inherit the organization default, and members can
  inherit the team default or carry an individual cap.
- **Effective policy** (`EffectivePolicyCard`) — the resolved result of
  all layers for that scope, so you can verify what actually applies.

Member nodes also carry per-user status controls — activate/deactivate
the account, assign or remove the team-leader flag, and force
re-authentication for that individual user.

## Operational notes

- Membership and org structure changes belong in Cognito (or your IdP
  group mapping), **not** this console — the UI is read-only for tree
  structure except app-access and rate-limit policy.
- **Team leader** designation is done here (it is not a Cognito group);
  the flag survives Cognito syncs.
- After syncing, check the summary for `reconcile_incomplete` warnings —
  they mean a fetch failed and deactivation was skipped on purpose.

## Related

- Per-user usage is visible to admins in
  [Analytics](analytics.md) filtered by user — members see their own
  numbers on their `/my` page (not accessible to admins).
- The old `/rate-limits` URL lands here with `?from=rate-limits`.
