# Analytics (ROI Dashboard)

**Route:** `/analytics` · **Role:** Administrator, Team leader
*(also serves `/analytics/models`, which redirects here)*

Aggregated cost and usage analytics — the reporting counterpart to the
live [Monitoring](monitoring.md) page.

![Analytics](screenshots/10-analytics.png)

## Filters

| Control | Effect |
|---|---|
| **This Month / Last Month / Select Period** | Month presets and any month in the data. |
| **Custom Range** | Explicit start/end dates (`mm/dd/yyyy` pickers appear inline). |
| **By Model** | Restricts all charts to one model. |
| **Refresh** | Re-pulls data without changing filters. |
| **Export** | Downloads the current view — CSV or JSON. |

Custom Range mode:

![Custom Range](screenshots/36-analytics-custom-range.png)

The **Select Period** dropdown lists every month present in the data:

![Period selector](screenshots/37-analytics-period.png)

**Export** offers a format choice:

![Export menu](screenshots/38-analytics-export.png)

**By Model** scopes every card and chart to a single model:

![By Model filter](screenshots/39-analytics-bymodel.png)

## KPI cards

- **Successful Requests** — total and error count/rate.
- **Total Tokens** — all token types combined.
- **Total Cost** — USD for the period.
- **Average Cost per User** — total ÷ active users.

## Charts

- **Cost trend** — daily cost and request lines, one series per team.
- **Cost by Model** — per-model spend bars for the period.
- **Token Analysis** — donut splitting input / output / cache-read /
  cache-write token volumes.
- **Top spenders** and **model cost detail** panels — the *By Model*
  filter drives the per-model drill-down.

## Operational notes

- The `/analytics/models` URL still works — it redirects here with
  `?model=` filters preserved.
- Export produces a CSV or JSON of the current filtered view, suitable
  for importing into finance tooling.
- Team leaders see the same charts but scoped to their own team.
