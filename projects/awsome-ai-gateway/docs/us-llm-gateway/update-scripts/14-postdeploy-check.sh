#!/bin/bash
# ---------------------------------------------------------------------------
# 14-postdeploy-check.sh
#
# WHAT: read-only checks after an upstream sync deploy (ops/8-D). One psql pod
#       run + a few kubectl/helm reads. Prints OK / !! / XX per item and a
#       number block that can be saved before the deploy and compared after.
# WHY:  the things that go wrong here are the quiet kind — a migration that
#       stopped one revision short, prices that silently stayed on the old
#       tier, a seeded alias left ACTIVE, budget rows doubled by a rollout
#       replay. None of them raise; all of them show up in these numbers.
# UNDO: nothing — this script changes nothing.
#
# Usage:
#   bash 14-postdeploy-check.sh                    # checks + numbers
#   bash 14-postdeploy-check.sh --save pre         # before deploy: snapshots/pre.numbers
#   bash 14-postdeploy-check.sh --compare snapshots/pre.numbers   # after: deltas
#   bash 14-postdeploy-check.sh --base-url https://gateway-dev.<domain>   # /health/ready via ALB
#   bash 14-postdeploy-check.sh --no-k8s           # DB-only (no kubectl/helm)
# ---------------------------------------------------------------------------
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/_lib.sh"

SAVE=""; COMPARE=""; BASE_URL=""; K8S=1
TSV="$LIB_DIR/pricing.tsv"
ROOT="$(cd "$LIB_DIR/../../.." && pwd)"          # projects/awsome-ai-gateway
RELEASE="llm-gateway"
SEEDED_ALIASES="'global.anthropic.claude-opus-5','global.anthropic.claude-sonnet-5','gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna','codex-gpt-5.6-sol','codex-gpt-5.6-terra','codex-gpt-5.6-luna','llama-3-70b'"

usage() {
  cat <<EOF
$(basename "$0") — post-deploy checks (read-only)

  --save <name>       write the number block to $SNAP_DIR/<name>.numbers
  --compare <file>    print deltas against a saved number block
  --base-url <url>    check /health/ready through the ALB (default: inside the pod)
  --no-k8s            skip kubectl/helm checks (DB only)
  --tsv <path>        price table (default: $TSV)
EOF
}
while [ $# -gt 0 ]; do
  case "$1" in
    --save)     SAVE="$2";     shift 2 ;;
    --compare)  COMPARE="$2";  shift 2 ;;
    --base-url) BASE_URL="${2%/}"; shift 2 ;;
    --no-k8s)   K8S=0;         shift ;;
    --tsv)      TSV="$2";      shift 2 ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

require_env
FAIL=0; WARN=0
fail() { bad "$1"; FAIL=$((FAIL+1)); }
warnc() { warn "$1"; WARN=$((WARN+1)); }
norm() { awk "BEGIN{printf \"%.6f\", $1}"; }

# ── Expected values from the repo ────────────────────────────────────────
EXPECTED_HEAD=$(ls "$ROOT"/db/versions/[0-9][0-9][0-9][0-9]_*.py 2>/dev/null | sed 's#.*/##' | cut -c1-4 | sort | tail -1)
[ -n "$EXPECTED_HEAD" ] || die "cannot find db/versions under $ROOT"

declare -a ALIASES=(); declare -A T_IN T_OUT T_C5M T_C1H T_CREAD
while IFS=$'\t' read -r a in out c5m c1h cread asof src extra; do
  [ -z "${a// }" ] && continue; [[ "$a" == \#* ]] && continue; [ "$a" = alias ] && continue
  ALIASES+=("$a"); T_IN[$a]=$in; T_OUT[$a]=$out; T_C5M[$a]=$c5m; T_C1H[$a]=$c1h; T_CREAD[$a]=$cread
done < "$TSV"
[ ${#ALIASES[@]} -gt 0 ] || die "no aliases in $TSV"
alist=""; for a in "${ALIASES[@]}"; do alist="${alist:+$alist,}'$a'"; done

# ── One psql run, rows tagged by a leading column ────────────────────────
# B sums only the client=NULL (total) rows: since the phase-2 cost-recorder the
# worker also writes one budget_usages row per app (client='cowork', ...), so
# summing every USER row counts each request twice (measured 1.39x on 2026-09-16).
SQL="\\pset format unaligned
\\pset fieldsep '|'
\\pset tuples_only on
SELECT 'V', version_num FROM public.alembic_version;
SELECT 'T', (to_regclass('public.system_settings') IS NOT NULL)::text;
SELECT 'P', model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
       cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
       cache_read_price_per_1k_tokens
  FROM model.model_pricings WHERE effective_until IS NULL AND model_alias IN ($alist) ORDER BY model_alias;
SELECT 'A', alias, status FROM model.model_aliases WHERE alias IN ($SEEDED_ALIASES) ORDER BY alias;
SELECT 'R', alias, provider_model_id, status FROM model.model_aliases WHERE alias IN ($alist) ORDER BY alias;
SELECT 'U', coalesce(sum(cost_usd),0), count(*)
  FROM usage.usage_logs WHERE requested_at >= date_trunc('month', now());
SELECT 'B', coalesce(sum(used_usd),0)
  FROM budget.budget_usages WHERE scope = 'USER' AND period = to_char(now(), 'YYYY-MM') AND client IS NULL;
SELECT 'W', coalesce(sum(web_search_count),0), count(*) FILTER (WHERE web_search_count > 0)
  FROM usage.usage_logs WHERE requested_at > now() - interval '24 hours';
SELECT 'E', count(*) FILTER (WHERE status = 'SUCCESS'), count(*) FILTER (WHERE status <> 'SUCCESS')
  FROM usage.usage_logs WHERE requested_at > now() - interval '1 hour';
SELECT (to_regclass('public.system_settings') IS NOT NULL) AS has_ss \\gset
\\if :has_ss
SELECT 'S', coalesce((SELECT value::text FROM public.system_settings WHERE key = 'body_logging_enabled'), 'absent');
\\endif"

hdr "1. Database (one psql pod, ~1 min)"
raw=$(run_sql "$SQL") || { printf '%s\n' "$raw"; die "database query failed"; }
DB_HEAD=""; HAS_SS=""; BODYLOG=""; U_COST=""; U_N=""; B_SUM=""; W_SUM=""; W_N=""; E_OK=""; E_BAD=""
declare -A CUR_P SEED_ST ROUTE
while IFS='|' read -r tag f1 f2 f3 f4 f5 f6; do
  case "$tag" in
    V) DB_HEAD="$f1" ;;
    T) HAS_SS="$f1" ;;
    S) BODYLOG="$f1" ;;
    P) CUR_P[$f1]="$f2|$f3|$f4|$f5|$f6" ;;
    A) SEED_ST[$f1]="$f2" ;;
    U) U_COST="$f1"; U_N="$f2" ;;
    B) B_SUM="$f1" ;;
    R) ROUTE[$f1]="$f2|$f3" ;;
    W) W_SUM="$f1"; W_N="$f2" ;;
    E) E_OK="$f1"; E_BAD="$f2" ;;
  esac
done <<<"$raw"

# alembic head
if [ "$DB_HEAD" = "$EXPECTED_HEAD" ]; then ok "alembic_version = $DB_HEAD (repo head $EXPECTED_HEAD)"
else fail "alembic_version = ${DB_HEAD:-?} but repo head is $EXPECTED_HEAD — migration Job did not finish"; fi

# system_settings / body logging
if [ "$HAS_SS" = "true" ]; then
  case "$BODYLOG" in
    absent|false|'"false"') ok "system_settings present, body_logging_enabled = ${BODYLOG} (off)" ;;
    *) warnc "body_logging_enabled = $BODYLOG — prompts are being written to S3; is that intended?" ;;
  esac
else
  fail "public.system_settings missing (migration 0036 not applied)"
fi

# prices vs pricing.tsv
for a in "${ALIASES[@]}"; do
  cur="${CUR_P[$a]:-}"
  if [ -z "$cur" ]; then warnc "$a: no open price row (alias not registered?)"; continue; fi
  IFS='|' read -r ci co c5 c1 cr <<<"$cur"
  if [ "$(norm "$ci")" = "$(norm "${T_IN[$a]}")" ] && [ "$(norm "$co")" = "$(norm "${T_OUT[$a]}")" ] \
     && [ "$(norm "$c5")" = "$(norm "${T_C5M[$a]}")" ] && [ "$(norm "$c1")" = "$(norm "${T_C1H[$a]}")" ] \
     && [ "$(norm "$cr")" = "$(norm "${T_CREAD[$a]}")" ]; then
    ok "$a price = pricing.tsv ($ci / $co / $c5 / $c1 / $cr)"
  else
    fail "$a price $ci / $co / $c5 / $c1 / $cr ≠ price table $TSV — run 08-set-model-pricing.sh --apply; if the DB values are what AWS bills you, edit that file instead (asof/source too) and this turns OK"
  fi
done

# routing of the served aliases: provider_model_id must keep the prefix config.env
# uses (us. = Geo CRIS). 0027 seeds the same alias names as global.* — DO NOTHING
# on conflict, so an existing row must not change; a fresh install needs 02 --remap.
PFX="${MODEL_PROVIDER_ID%%anthropic*}"
if [ -n "$PFX" ] && [ "$PFX" != "$MODEL_PROVIDER_ID" ]; then
  for a in "${ALIASES[@]}"; do
    r="${ROUTE[$a]:-}"; [ -z "$r" ] && continue
    IFS='|' read -r pid st <<<"$r"
    if [ "$st" != "ACTIVE" ]; then warnc "$a is $st (not served)"
    elif [[ "$pid" == "$PFX"* ]]; then ok "$a -> $pid"
    else fail "$a -> $pid — not the $PFX* route config.env expects; run 02-add-opus5-model.sh --remap (or fix in admin UI)"; fi
  done
fi

# seeded aliases that should not be ACTIVE here
for a in $(printf '%s' "$SEEDED_ALIASES" | tr -d "'" | tr ',' ' '); do
  st="${SEED_ST[$a]:-}"
  case "$st" in
    "")       note "$a: not present" ;;
    ACTIVE)   warnc "$a is ACTIVE — set INACTIVE in admin UI > Models (not served by this deployment)" ;;
    *)        ok "$a: $st" ;;
  esac
done

# ── Numbers (save / compare) ──────────────────────────────────────────────
hdr "2. Numbers (this month unless noted)"
NUM="usage_cost_month=$U_COST
usage_rows_month=$U_N
budget_user_sum_month=$B_SUM
web_search_24h=$W_SUM
web_search_rows_24h=$W_N
req_1h_ok=$E_OK
req_1h_err=$E_BAD
alembic=$DB_HEAD"
printf '%s\n' "$NUM" | sed 's/^/  /'
ratio=$(awk "BEGIN{ if ($U_COST > 0) printf \"%.3f\", $B_SUM / $U_COST; else print \"n/a\" }")
note "budget_user_sum / usage_cost = $ratio  (≈1.0 expected; seed-spent or a rollout replay moves it)"
if [ "$ratio" != "n/a" ] && awk "BEGIN{exit !($ratio > 1.05)}"; then
  warnc "budget rows exceed usage cost by >5% — check for a rollout replay (5597f64) or seeded spend"
fi
if [ -n "$SAVE" ]; then
  printf '%s\n' "$NUM" > "$SNAP_DIR/$SAVE.numbers"; note "saved: $SNAP_DIR/$SAVE.numbers"
fi
if [ -n "$COMPARE" ]; then
  [ -f "$COMPARE" ] || die "compare file not found: $COMPARE"
  hdr "2b. Delta vs $(basename "$COMPARE")"
  while IFS='=' read -r k v; do
    now=$(printf '%s\n' "$NUM" | awk -F= -v k="$k" '$1==k{print $2}')
    case "$k" in
      alembic) [ "$v" = "$now" ] && note "$k: $v (unchanged)" || note "$k: $v -> $now" ;;
      *) d=$(awk "BEGIN{printf \"%+.4f\", ($now) - ($v)}"); note "$k: $v -> $now  ($d)" ;;
    esac
  done < "$COMPARE"
  note "budget/usage deltas should track each other; web_search_24h should keep growing if web search is used"
fi

# ── Kubernetes / helm ─────────────────────────────────────────────────────
if [ "$K8S" -eq 1 ]; then
  hdr "3. Cluster"
  notready=$(kubectl -n "$NS" get pods --no-headers 2>/dev/null | awk '$3!="Running" && $3!="Completed" && $3!="Succeeded"' | wc -l)
  total=$(kubectl -n "$NS" get pods --no-headers 2>/dev/null | wc -l)
  if [ "$notready" -eq 0 ] && [ "$total" -gt 0 ]; then ok "pods: $total, none pending/failed"
  else fail "pods not Running: $notready of $total"; kubectl -n "$NS" get pods --no-headers | awk '$3!="Running" && $3!="Completed"' | sed 's/^/     /'; fi

  hist=$(helm -n "$NS" history "$RELEASE" 2>/dev/null | tail -1)
  case "$hist" in *deployed*) ok "helm: $(echo "$hist" | awk '{print "rev "$1" "$7" "$8" "$9}' | cut -c1-70)";;
                  *) fail "helm last revision not 'deployed': $hist";; esac

  job=$(kubectl -n "$NS" get jobs --no-headers 2>/dev/null | command grep -- "-migration" | tail -1)
  case "$job" in
    "") if [ "$DB_HEAD" = "$EXPECTED_HEAD" ]; then note "no migration job left (hook-succeeded cleanup) — alembic check above is the signal"
        else warnc "no migration job found and alembic is behind — check helm history / install-eks.sh output"; fi ;;
    *)  comp=$(echo "$job" | awk '{print $2}'); [ "$comp" = "1/1" ] && ok "migration job $(echo "$job" | awk '{print $1}') completed" \
          || warnc "migration job $(echo "$job" | awk '{print $1}') completions=$comp" ;;
  esac

  ing=$(kubectl -n "$NS" get ingress -o custom-columns='NAME:.metadata.name,CIDRS:.metadata.annotations.alb\.ingress\.kubernetes\.io/inbound-cidrs,PL:.metadata.annotations.alb\.ingress\.kubernetes\.io/security-group-prefix-lists' --no-headers 2>/dev/null)
  printf '%s\n' "$ing" | sed 's/^/     /'
  if printf '%s\n' "$ing" | awk '$2=="<none>"' | command grep -q .; then warnc "an Ingress lost inbound-cidrs — see 8-U 3단계 (06-persist-annotations / 03 --allow-cloudfront)"; else ok "ingress inbound-cidrs present on all"; fi
  gw_pl=$(printf '%s\n' "$ing" | awk '$1 ~ /gateway$/ {print $3}')
  if [ -n "$gw_pl" ] && [ "$gw_pl" != "<none>" ]; then ok "gateway Ingress prefix-list present ($gw_pl)"
  else note "gateway Ingress has no security-group-prefix-lists — fine without CloudFront (US-06 HTTPS); if CloudFront is in use this means 502 (8-U 3단계)"; fi

  hdr "4. /health/ready"
  if [ -n "$BASE_URL" ]; then
    body=$(curl -s -m 10 "$BASE_URL/health/ready"); code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$BASE_URL/health/ready")
  else
    port=$(kubectl -n "$NS" get svc "$RELEASE-gateway-proxy" -o jsonpath='{.spec.ports[0].targetPort}' 2>/dev/null); port="${port:-8000}"
    body=$(kubectl -n "$NS" exec deploy/"$RELEASE-gateway-proxy" -- python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:$port/health/ready',timeout=5).read().decode())" 2>/dev/null); code=$([ -n "$body" ] && echo 200 || echo 000)
  fi
  if [ "$code" = "200" ]; then ok "ready 200: $(printf '%s' "$body" | tr -d '\n' | cut -c1-140)"
  else fail "/health/ready HTTP $code: $(printf '%s' "$body" | cut -c1-140)"; fi
fi

hdr "Result"
[ "$FAIL" -eq 0 ] && ok "no failures ($WARN warning(s))" || bad "$FAIL failure(s), $WARN warning(s) — see above"
[ "$FAIL" -eq 0 ]
