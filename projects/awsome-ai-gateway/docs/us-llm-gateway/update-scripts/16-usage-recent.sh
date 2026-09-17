#!/bin/bash
# ---------------------------------------------------------------------------
# 16-usage-recent.sh
#
# WHAT: list the most recent gateway requests with their token counts, web
#       search count and cost, plus a total for the window. Read-only.
# WHY:  "what did that Cowork question cost?" — the answer is in usage_logs
#       (cache tokens included), not in any client-side view.
#
# Usage:
#   bash 16-usage-recent.sh                       # last 2 hours, all clients, 40 rows
#   bash 16-usage-recent.sh --hours 6 --client cowork --limit 100
#   bash 16-usage-recent.sh --print-sql           # show the query only (no AWS)
# ---------------------------------------------------------------------------
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/_lib.sh"

HOURS=2; CLIENT=""; LIMIT=40; PRINT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --hours)   HOURS="$2";  shift 2 ;;
    --client)  CLIENT="$2"; shift 2 ;;
    --limit)   LIMIT="$2";  shift 2 ;;
    --print-sql) PRINT=1;   shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done
[[ "$HOURS" =~ ^[0-9]+$ ]] && [[ "$LIMIT" =~ ^[0-9]+$ ]] || die "--hours and --limit must be integers"
[[ "$CLIENT" =~ ^[A-Za-z0-9_-]*$ ]] || die "--client: letters, digits, - _ only"
CF=""; [ -n "$CLIENT" ] && CF="AND client = '$CLIENT'"

SQL=$(cat <<Q
\\echo ''
\\echo '--- requests in the last ${HOURS}h ${CLIENT:+(client=$CLIENT)} — newest first, cost = tokens x model price at the time ---'
SELECT to_char(requested_at AT TIME ZONE 'UTC', 'MM-DD HH24:MI:SS') AS utc,
       client, model_alias AS model, status,
       input_tokens AS "in", output_tokens AS "out",
       cache_creation_tokens AS cache_w, cache_read_tokens AS cache_r,
       reasoning_tokens AS think, web_search_count AS ws,
       round(cost_usd::numeric, 4) AS cost_usd,
       round(latency_ms / 1000.0, 1) AS sec
  FROM usage.usage_logs
 WHERE requested_at > now() - interval '${HOURS} hours' $CF
 ORDER BY requested_at DESC
 LIMIT $LIMIT;
\\echo ''
\\echo '--- totals for the same window ---'
SELECT count(*) AS requests,
       sum(input_tokens) AS "in", sum(output_tokens) AS "out",
       sum(cache_creation_tokens) AS cache_w, sum(cache_read_tokens) AS cache_r,
       sum(web_search_count) AS ws,
       round(sum(cost_usd)::numeric, 4) AS cost_usd
  FROM usage.usage_logs
 WHERE requested_at > now() - interval '${HOURS} hours' $CF;
Q
)
if [ "$PRINT" -eq 1 ]; then printf '%s\n' "$SQL"; exit 0; fi
require_env >/dev/null
OUT=$(run_sql "$SQL" 2>&1) || { echo "$OUT"; die "query failed"; }
echo "$OUT"
