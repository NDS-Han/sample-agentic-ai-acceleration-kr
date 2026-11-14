# Monitoring

**Route:** `/monitoring` · **Role:** Administrator

Live health and traffic view — request rates, latency, errors, and the
body-logging control that governs what is captured for deep inspection.

![Monitoring](screenshots/12-monitoring.png)

## Body logging banner

The top banner shows the **Invocation Body Logging** state:

- **ON** — complete request/response bodies are being stored in S3.
  They are **not masked** — whatever a user put in a prompt is stored
  verbatim.
- Toggle it off to stop capture; changes reach the gateway within a few
  seconds.
- Turning it **off** asks for confirmation first — the dialog warns that
  bodies from the off period cannot be recovered later:

![Turn-off confirmation](screenshots/32-monitoring-toggle-confirm.png)

- The caption **"Capturing — prompt and response bodies are being stored
  in S3 without masking"** stays visible while it is on.
- The banner links to the [Body Logs](#body-logs) browser:

## Last 1 Hour Summary

| Card | Meaning |
|---|---|
| **Total Requests** | Requests in the last hour. |
| **Errors** | Count and percentage of failed requests. |
| **Avg / P95 Latency** | Round-trip latency to the model endpoint. |
| **Total Cost** | USD spent in the window. |
| **Active models (catalog)** | Models currently usable — the catalog ceiling, not what was actually called. |

Below the cards are **model traffic** and **user traffic** tables for the
same window — which models and users carried the load.

## Usage log

Chronological event feed for the last 24 hours, filterable by **Type**
(API request, auth, admin action, …). Each entry expands for the
request metadata.

## Operational notes

- Body logging is a debugging tool, not an always-on audit — turn it on
  while reproducing a problem, then off again. Storage is unmasked S3
  and views are recorded in the audit log.
- The page reads from the live metrics path, not the aggregated
  analytics store — figures can differ from
  [Analytics](analytics.md) for the same window.

## Body Logs

**Route:** `/monitoring/body-logs` · **Role:** Administrator

Browser over the captured request/response bodies — available only
while body logging is on (the toggle above). Two views:

**Sessions** — calls grouped into time-bucketed sessions per user.
Filter by period, user email/ID, client, or **Errors only**:

![Body Logs — sessions](screenshots/13-monitoring-body-logs.png)

Expanding a session lists its individual calls — timestamp, model,
latency, cost, and in→out token counts — with a **Load bodies** link
to pull the stored payloads:

![Body Logs — session expanded](screenshots/51-bodylogs-session-expanded.png)

**By request** — a flat list of recent captured calls, filterable by
user. The **Request ID lookup** at the top is the tool for user
reports: paste the `X-Request-Id` the caller saw to jump straight to
that request's bodies:

![Body Logs — by request](screenshots/52-bodylogs-by-request.png)

**View** on a row opens the captured prompt and response — with
**Copy JSON** / **Raw JSON** for inspection. This is what "unmasked"
means in practice: the literal user prompt is visible:

![Body viewer — request & response bodies](screenshots/53-bodylogs-request-expanded.png)

Every view of a body is written to the [Audit Log](audit-log.md) as a
`View body log` event — the top banner states this explicitly because
the contents are unmasked.
