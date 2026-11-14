# Admin UI Operations Guide

Operational guide for the AWSome AI Gateway admin console. Each document
covers one page: what it shows, which controls it exposes, and the
administrator workflows built on it.

> All screenshots were captured from a live deployment with the UI locale
> set to English (the **KO/EN** toggle in the top-right header). Emails,
> endpoint URLs, and Cognito identifiers in the screenshots are masked
> placeholders — the real deployment shows actual values.

## Roles

The console is role-gated. A user's role is resolved by admin-api from
Cognito group membership (admin mapping) or by the **Team leader** flag
set inside the console.

| Role | Sees |
|---|---|
| **Administrator** | Every page except My Usage |
| **Team leader** | Budget Management, Analytics, Integration, CLI, My Usage |
| **Developer** | Integration, CLI, My Usage |

Routes that are not listed for a role are default-deny — the user is
redirected to the [403 page](access-denied.md).

## Page index

| Page | Route | Roles | Guide |
|---|---|---|---|
| Dashboard | `/` | Admin | [dashboard.md](dashboard.md) |
| Users/Teams | `/users` | Admin | [users-teams.md](users-teams.md) |
| Model Management | `/models` | Admin | [models.md](models.md) |
| App Policy | `/apps` | Admin | [app-policy.md](app-policy.md) |
| Budget Management | `/budgets` | Admin, Team leader | [budgets.md](budgets.md) |
| API Keys | `/keys` | Admin | [api-keys.md](api-keys.md) |
| Agents | `/agents` | Admin | [agents.md](agents.md) |
| Agent detail | `/agents/{userId}` | Admin | [agents.md](agents.md#agent-detail) |
| Monitoring | `/monitoring` | Admin | [monitoring.md](monitoring.md) |
| Body Logs | `/monitoring/body-logs` | Admin | [monitoring.md](monitoring.md#body-logs) |
| Analytics | `/analytics` | Admin, Team leader | [analytics.md](analytics.md) |
| BI Insight | `/chat` | Admin | [bi-insight.md](bi-insight.md) |
| Audit Log | `/audit` | Admin | [audit-log.md](audit-log.md) |
| Integration | `/integration` | All | [integration.md](integration.md) |
| CLI | `/cli` | All | [cli.md](cli.md) |
| My Usage | `/my` | Team leader, Developer | [my-usage.md](my-usage.md) |
| Access denied | `/403` | — | [access-denied.md](access-denied.md) |

### Legacy redirects

These URLs still work but redirect to their replacement pages:

| Old URL | Redirects to |
|---|---|
| `/service-accounts` | `/agents` |
| `/analytics/models` | `/analytics` (filters preserved) |
| `/rate-limits` | `/users?from=rate-limits` — rate limits now live per-scope on the Users/Teams page |

## Common UI elements

- **Sidebar** — primary navigation. Items the current role cannot open
  are hidden, not disabled.
- **Header** — language toggle (**KO/EN**), signed-in identity (display
  name, email, role badge), and **Logout**.
- **Period selectors** — Dashboard and Analytics default to the current
  calendar month; use *This month / Last month / Select period / Custom
  range* to change the window.
- **Client tabs** — Dashboard and Analytics split metrics by client
  family (*All, Claude Code, Cowork, Codex, Other*). Client identity is
  derived from the `User-Agent` / key type of each request.

## Screenshots

All images referenced by the guides live in [screenshots/](screenshots/).
