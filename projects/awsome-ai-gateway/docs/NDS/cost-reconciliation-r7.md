# R7 — Bedrock invocation-log / CloudWatch / CUR cost match (design only)

[한국어](cost-reconciliation-r7.ko.md) · **English**

Status: design (wave 3). Not implemented. The nightly reconciliation job
(`cost-recorder-worker/src/worker/reconciliation.py`, R1–R6) compares our own ledgers with each
other. R7 compares our ledger with what AWS says it charged. It needs AWS reads (CloudWatch Logs
Insights, CloudWatch metrics, CUR in S3/Athena), so it is a separate, opt-in job.

## Sources

| Source | Key | Granularity | Latency | Has tokens | Has $ |
| --- | --- | --- | --- | --- | --- |
| Bedrock model-invocation logs (CloudWatch Logs group, already read by `admin-api services/invocation_log_service.py`) | `requestId` = `usage_logs.bedrock_request_id` | per call | ~1–5 min | input/output, cache read/write | no |
| CloudWatch `AWS/Bedrock` metrics `InputTokenCount`, `OutputTokenCount`, `Invocations` | `ModelId` | 1 min | ~1–3 min | yes (sum) | no |
| Cost and Usage Report (CUR 2.0, Athena) | `line_item_usage_type` (model + token class), `line_item_resource_id`, account | hourly line items | 24–48 h | usage amount = tokens | yes (`line_item_unblended_cost`) |
| AgentCore Web Search | CUR line items for AgentCore gateway search usage | hourly | 24–48 h | queries | yes |

## Matching

**R7a per-call (daily, report_date = yesterday).** For every `usage_logs` row with a
`bedrock_request_id`, look up the invocation-log record (Logs Insights query over the day,
padded by 10 min like `padded_window`). Compare `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_creation_tokens`. Buckets:
- `match` — equal.
- `estimated` — our row has `estimated_usage=true` (client cancel / drain timeout). Report the
  delta (AWS − ours) separately; this is the measured error of the estimator, and the share of
  revenue it affects.
- `missing_in_aws` — we billed, AWS has no record (alarm: phantom charge or log gap).
- `missing_in_ours` — AWS logged a call from our gateway role with no `usage_logs` row (alarm:
  lost cost row; cross-check `usage.cost_dead_letters`).
- `null_id` — rows without `bedrock_request_id`: Mantle plane (no invocation logs) and
  web-search-loop rows (one row covers N Bedrock calls). Counted by reason
  (`classify_null_request_id` already exists), not matched.

For web-search-loop rows, a later improvement is to carry the list of per-turn Bedrock request
ids in the cost entry (new nullable `usage_logs.bedrock_request_ids text[]`), then sum the turns.

**R7b aggregate (daily).** Σ CloudWatch `InputTokenCount`/`OutputTokenCount` per `ModelId` per
hour vs Σ `usage_logs` tokens per `provider_model_id` per hour (all statuses, only accounts the
gateway calls). Tolerance: 0 for hours whose rows are all non-estimated; otherwise report the
difference and the estimated-row share. Catches calls outside the gateway's ledger (e.g. another
workload on the same account) as `aws > ours`.

**R7c money (monthly, after CUR close ~day 3).** Athena query on CUR for the month: Σ
`line_item_unblended_cost` grouped by model usage type (input / output / cache read / cache write)
and AgentCore search, vs Σ `usage_logs.cost_usd` and Σ `web_search_cost_usd` grouped the same
way. Differences come from: price edits not yet in `model.model_pricings`, private/negotiated
pricing, free tier (AgentCore 5,000 queries/month org-wide is not subtracted by us), and
estimated rows. Output: per usage type `aws_cost`, `our_cost`, `diff`, `diff_pct`.

## Storage and alerting

Same table as R1–R6: one `usage.reconciliation_runs` row per R7 run with
`checks = {"r7a": {...}, "r7b": {...}, "r7c": {...}}` and its own `status`. Metrics:
`cost_recorder_reconciliation_mismatches{check="r7a_missing_in_ours"}` etc. Alert on any
`missing_in_*` > 0 and on `r7c.diff_pct` above an operator threshold (prices drift).

## Where it runs and what it needs

- Runs in admin-api's scheduler (it already holds the Logs client and the invocation-log
  settings) or as a CronJob using the worker image; own advisory lock (`727_003`).
- IAM (read-only): `logs:StartQuery`, `logs:GetQueryResults` on the invocation-log group;
  `cloudwatch:GetMetricData` on `AWS/Bedrock`; `athena:StartQueryExecution`,
  `athena:GetQueryResults`, `s3:GetObject` on the CUR bucket + Athena result bucket.
- No new infrastructure if CUR 2.0 + Athena are already enabled for the payer account; otherwise
  this is an infra request for prod (out of scope for dev, see AGENTS.md rule 5).
- Cost: Logs Insights scans the day's invocation logs once (~bytes scanned × $0.005/GB).

## Open questions

1. Which account(s) does the gateway bill to (in-account + cross-account model routes)? R7b/R7c
   need the list; the model-level routing table (`model_aliases.account_role_arn`) is the source.
2. Do invocation logs include cache-write 1h vs 5m split? If not, R7a compares the total only.
3. Negotiated prices: R7c needs an "expected discount" per usage type to avoid permanent noise.
