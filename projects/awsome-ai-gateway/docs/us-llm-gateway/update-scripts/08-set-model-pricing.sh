#!/bin/bash
# ---------------------------------------------------------------------------
# 08-set-model-pricing.sh
#
# WHAT: bring model.model_pricings in line with pricing.tsv — for every alias
#       whose open price row differs from the table, close that row
#       (effective_until = now()) and insert a new one (effective_from = now()).
#       Aliases that are not registered in the DB are skipped (02 registers
#       models); aliases whose prices already match are skipped.
# WHY:  the gateway multiplies usage by whatever row is open, so a stale or
#       wrong tier silently mis-bills every call. pricing.tsv is the committed
#       source of truth (Standard tier for us.* profiles — see its header).
#       Past usage_logs are NOT recomputed: cost is fixed at record time.
# UNDO: snapshots/<ts>-08-pricing-rollback.sql re-inserts the previous values
#       as a new row (forward fix; rows are never deleted).
#
# Usage:
#       bash 08-set-model-pricing.sh                 # dry-run: current vs table
#       bash 08-set-model-pricing.sh --apply         # apply, then verify
#       bash 08-set-model-pricing.sh --alias claude-sonnet-5 --apply
#       bash 08-set-model-pricing.sh --print-sql     # show SQL, no AWS needed
#       bash 08-set-model-pricing.sh --apply --wait  # apply + wait 300s cache TTL
# ---------------------------------------------------------------------------
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/_lib.sh"

TSV="$LIB_DIR/pricing.tsv"
APPLY=0; PRINT_SQL=0; WAIT=0
ONLY=()

usage() {
  cat <<EOF
$(basename "$0") — set model prices from pricing.tsv (USD per 1K tokens)

  --tsv <path>       price table (default: $TSV)
  --alias <alias>    limit to one alias (repeatable)
  --apply            actually apply (otherwise dry-run)
  --wait             after --apply, wait 300s for the model cache to expire
  --print-sql        print the SQL that would run and exit (no AWS/kubectl)

How a row is chosen
  The open row = effective_until IS NULL. It is closed and replaced only when at
  least one of the five prices, or the long-prompt rate card (long_* columns),
  differs from the table. effective_from is the
  apply time: earlier usage_logs keep the price they were recorded with.

Where the numbers come from
  pricing.tsv header. Edit the table (and its asof/source columns) when AWS
  changes the price list; do not pass prices on the command line.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tsv)       TSV="$2";        shift 2 ;;
    --alias)     ONLY+=("$2");    shift 2 ;;
    --apply)     APPLY=1;         shift ;;
    --wait)      WAIT=1;          shift ;;
    --print-sql) PRINT_SQL=1;     shift ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

# ── Parse pricing.tsv ─────────────────────────────────────────────────────
# Columns: alias input output cache_5m cache_1h cache_read
#          long_above long_input long_output long_cache_5m long_cache_1h long_cache_read
#          asof source
# The six long_* columns are the long-prompt rate card (DB long_context_*): all
# numbers, or all "-" for none. They are stored here as "" when "-".
[ -f "$TSV" ] || die "price table not found: $TSV"
declare -a ALIASES=()
declare -A T_IN T_OUT T_C5M T_C1H T_CREAD T_ASOF T_SRC
declare -A T_LABOVE T_LIN T_LOUT T_LC5M T_LC1H T_LCREAD
num_re='^[0-9]+\.[0-9]{1,8}$'
int_re='^[1-9][0-9]*$'
alias_re='^[A-Za-z0-9][A-Za-z0-9._-]*$'
date_re='^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
lineno=0
while IFS=$'\t' read -r a in out c5m c1h cread labove lin lout lc5m lc1h lcread asof src extra; do
  lineno=$((lineno + 1))
  [ -z "${a// }" ] && continue
  [[ "$a" == \#* ]] && continue
  [ "$a" = "alias" ] && continue            # header
  [ -n "${extra:-}" ] && die "$TSV:$lineno: more than 14 columns"
  [ -n "${src:-}" ] || die "$TSV:$lineno: expected 14 tab-separated columns"
  [[ "$a" =~ $alias_re ]] || die "$TSV:$lineno: bad alias '$a'"
  for v in "$in" "$out" "$c5m" "$c1h" "$cread"; do
    [[ "$v" =~ $num_re ]] || die "$TSV:$lineno: price must be a decimal with up to 8 places: '$v'"
  done
  # A zero input/output price bills every call at \$0 (router_service falls back to 0).
  awk "BEGIN{exit !($in > 0 && $out > 0)}" || die "$TSV:$lineno: input/output price is 0 for $a"
  if [ "$labove$lin$lout$lc5m$lc1h$lcread" = "------" ]; then
    labove=""; lin=""; lout=""; lc5m=""; lc1h=""; lcread=""
  else
    # Half a rate card bills the missing buckets at the short rate without saying so
    # (gateway falls back per bucket), so it is all six or none.
    [[ "$labove" =~ $int_re ]] \
      || die "$TSV:$lineno: long_above must be a token count, or all six long_* columns '-': '$labove'"
    for v in "$lin" "$lout" "$lc5m" "$lc1h" "$lcread"; do
      [[ "$v" =~ $num_re ]] \
        || die "$TSV:$lineno: long_* price must be a decimal with up to 8 places, or all six '-': '$v'"
    done
    awk "BEGIN{exit !($lin > 0 && $lout > 0)}" || die "$TSV:$lineno: long input/output price is 0 for $a"
  fi
  [[ "$asof" =~ $date_re ]] || die "$TSV:$lineno: asof must be YYYY-MM-DD: '$asof'"
  [ -n "${T_IN[$a]:-}" ] && die "$TSV:$lineno: duplicate alias $a"
  if [ ${#ONLY[@]} -gt 0 ]; then
    keep=0; for o in "${ONLY[@]}"; do [ "$o" = "$a" ] && keep=1; done
    [ $keep -eq 1 ] || continue
  fi
  ALIASES+=("$a")
  T_IN[$a]=$in; T_OUT[$a]=$out; T_C5M[$a]=$c5m; T_C1H[$a]=$c1h; T_CREAD[$a]=$cread
  T_LABOVE[$a]=$labove; T_LIN[$a]=$lin; T_LOUT[$a]=$lout
  T_LC5M[$a]=$lc5m; T_LC1H[$a]=$lc1h; T_LCREAD[$a]=$lcread
  T_ASOF[$a]=$asof; T_SRC[$a]=$src
done < "$TSV"
[ ${#ALIASES[@]} -gt 0 ] || die "no aliases selected from $TSV${ONLY:+ (filter: ${ONLY[*]})}"

sql_list() {                      # 'a','b','c'
  local out="" a
  for a in "${ALIASES[@]}"; do out="${out:+$out,}'$a'"; done
  printf '%s' "$out"
}

# One psql run returns both the alias status and the open price rows, tagged
# by a leading column so the two result sets can be told apart.
SQL_READ="\\pset format unaligned
\\pset fieldsep '|'
\\pset tuples_only on
SELECT 'A', alias, status FROM model.model_aliases
 WHERE alias IN ($(sql_list)) ORDER BY alias;
SELECT 'P', model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
       cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
       cache_read_price_per_1k_tokens,
       long_context_threshold_tokens, long_context_input_price_per_1k_tokens,
       long_context_output_price_per_1k_tokens, long_context_cache_creation_5m_price_per_1k_tokens,
       long_context_cache_creation_1h_price_per_1k_tokens, long_context_cache_read_price_per_1k_tokens,
       effective_from
  FROM model.model_pricings
 WHERE effective_until IS NULL AND model_alias IN ($(sql_list))
 ORDER BY model_alias, effective_from;
$PRICE_SCALE_SQL"

sqlv() { [ -n "$1" ] && printf '%s' "$1" || printf 'NULL'; }   # "" → SQL NULL

# INSERT for one price row: <alias> <in> <out> <c5m> <c1h> <cread> <6 long_* or "">
price_insert_sql() {
  cat <<EOF
INSERT INTO model.model_pricings
    (id, model_alias,
     input_price_per_1k_tokens, output_price_per_1k_tokens,
     cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
     cache_read_price_per_1k_tokens,
     long_context_threshold_tokens, long_context_input_price_per_1k_tokens,
     long_context_output_price_per_1k_tokens, long_context_cache_creation_5m_price_per_1k_tokens,
     long_context_cache_creation_1h_price_per_1k_tokens, long_context_cache_read_price_per_1k_tokens,
     effective_from, created_by)
VALUES (gen_random_uuid(), '$1', $2, $3, $4, $5, $6,
        $(sqlv "$7"), $(sqlv "$8"), $(sqlv "$9"), $(sqlv "${10}"), $(sqlv "${11}"), $(sqlv "${12}"),
        now(), '$SEED_ADMIN_UUID');
EOF
}

apply_sql_for() {                 # <alias> → close open row + insert table row
  local a="$1"
  cat <<EOF
UPDATE model.model_pricings SET effective_until = now()
 WHERE model_alias = '$a' AND effective_until IS NULL;
EOF
  price_insert_sql "$a" "${T_IN[$a]}" "${T_OUT[$a]}" "${T_C5M[$a]}" "${T_C1H[$a]}" "${T_CREAD[$a]}" \
    "${T_LABOVE[$a]}" "${T_LIN[$a]}" "${T_LOUT[$a]}" "${T_LC5M[$a]}" "${T_LC1H[$a]}" "${T_LCREAD[$a]}"
}

if [ "$PRINT_SQL" -eq 1 ]; then
  : "${SEED_ADMIN_UUID:=00000000-0000-4000-a000-000000000010}"
  echo "-- read (dry-run) --"; printf '%s\n' "$SQL_READ"
  echo; echo "-- apply (per alias, inside one transaction) --"; echo "BEGIN;"
  for a in "${ALIASES[@]}"; do apply_sql_for "$a"; done
  echo "COMMIT;"
  exit 0
fi

require_env

# ── Read current state ────────────────────────────────────────────────────
hdr "Reading current prices (${#ALIASES[@]} alias(es) from $(basename "$TSV"))"
declare -A DB_STATUS CUR_IN CUR_OUT CUR_C5M CUR_C1H CUR_CREAD CUR_FROM CUR_N
declare -A CUR_LABOVE CUR_LIN CUR_LOUT CUR_LC5M CUR_LC1H CUR_LCREAD
DB_SCALE=""
raw=$(run_sql "$SQL_READ") || die "could not read model_pricings (see output above)"
while IFS='|' read -r tag f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12 f13; do
  case "$tag" in
    A) DB_STATUS[$f1]="$f2" ;;
    S) DB_SCALE="$f1" ;;
    P) CUR_N[$f1]=$(( ${CUR_N[$f1]:-0} + 1 ))
       CUR_IN[$f1]=$f2; CUR_OUT[$f1]=$f3; CUR_C5M[$f1]=$f4; CUR_C1H[$f1]=$f5
       CUR_CREAD[$f1]=$f6
       CUR_LABOVE[$f1]=$f7; CUR_LIN[$f1]=$f8; CUR_LOUT[$f1]=$f9
       CUR_LC5M[$f1]=$f10; CUR_LC1H[$f1]=$f11; CUR_LCREAD[$f1]=$f12
       CUR_FROM[$f1]=$f13 ;;
  esac
done <<<"$raw"

norm() { awk "BEGIN{printf \"%.8f\", $1}"; }
# Same price? Both empty (no long-prompt rate card) counts as same; one empty does not.
same_price() { [ -z "$1$2" ] || { [ -n "$1" ] && [ -n "$2" ] && [ "$(norm "$1")" = "$(norm "$2")" ]; }; }
same_all() {                       # <alias> → 0 if the open row already equals the table row
  local a="$1"
  same_price "${CUR_IN[$a]}"     "${T_IN[$a]}"     &&
  same_price "${CUR_OUT[$a]}"    "${T_OUT[$a]}"    &&
  same_price "${CUR_C5M[$a]}"    "${T_C5M[$a]}"    &&
  same_price "${CUR_C1H[$a]}"    "${T_C1H[$a]}"    &&
  same_price "${CUR_CREAD[$a]}"  "${T_CREAD[$a]}"  &&
  [ "${CUR_LABOVE[$a]}" = "${T_LABOVE[$a]}" ]      &&
  same_price "${CUR_LIN[$a]}"    "${T_LIN[$a]}"    &&
  same_price "${CUR_LOUT[$a]}"   "${T_LOUT[$a]}"   &&
  same_price "${CUR_LC5M[$a]}"   "${T_LC5M[$a]}"   &&
  same_price "${CUR_LC1H[$a]}"   "${T_LC1H[$a]}"   &&
  same_price "${CUR_LCREAD[$a]}" "${T_LCREAD[$a]}"
}
long_str() {                       # <above> <in> <out> <c5m> <c1h> <cread> → one display line
  [ -z "$1" ] && { printf 'none'; return; }
  printf 'prompt > %s tokens: %s / %s / %s / %s / %s' "$1" "$2" "$3" "$4" "$5" "$6"
}
t_long()   { long_str "${T_LABOVE[$1]}" "${T_LIN[$1]}" "${T_LOUT[$1]}" "${T_LC5M[$1]}" "${T_LC1H[$1]}" "${T_LCREAD[$1]}"; }
cur_long() { long_str "${CUR_LABOVE[$1]}" "${CUR_LIN[$1]}" "${CUR_LOUT[$1]}" "${CUR_LC5M[$1]}" "${CUR_LC1H[$1]}" "${CUR_LCREAD[$1]}"; }

# ── Diff ──────────────────────────────────────────────────────────────────
hdr "Current vs table (USD per 1K tokens: input / output / cache5m / cache1h / cacheread)"
CHANGE=()
for a in "${ALIASES[@]}"; do
  if [ -z "${DB_STATUS[$a]:-}" ]; then
    warn "$a  NOT REGISTERED — skipped (register with 02 first)"; continue
  fi
  st="${DB_STATUS[$a]}"
  if [ -z "${CUR_N[$a]:-}" ]; then
    printf '  %-28s %-8s current: (no open price row)\n' "$a" "$st"
    printf '  %-28s %-8s table:   %s / %s / %s / %s / %s   -> INSERT\n' "" "" \
      "${T_IN[$a]}" "${T_OUT[$a]}" "${T_C5M[$a]}" "${T_C1H[$a]}" "${T_CREAD[$a]}"
    printf '  %-28s %-8s   long:  %s\n' "" "" "$(t_long "$a")"
    CHANGE+=("$a"); continue
  fi
  printf '  %-28s %-8s current: %s / %s / %s / %s / %s   (since %s)\n' "$a" "$st" \
    "${CUR_IN[$a]}" "${CUR_OUT[$a]}" "${CUR_C5M[$a]}" "${CUR_C1H[$a]}" "${CUR_CREAD[$a]}" "${CUR_FROM[$a]:0:19}"
  printf '  %-28s %-8s   long:  %s\n' "" "" "$(cur_long "$a")"
  if same_all "$a"; then
    printf '  %-28s %-8s table:   same                                   -> SKIP\n' "" ""
  else
    printf '  %-28s %-8s table:   %s / %s / %s / %s / %s   -> CHANGE (asof %s)\n' "" "" \
      "${T_IN[$a]}" "${T_OUT[$a]}" "${T_C5M[$a]}" "${T_C1H[$a]}" "${T_CREAD[$a]}" "${T_ASOF[$a]}"
    printf '  %-28s %-8s   long:  %s\n' "" "" "$(t_long "$a")"
    CHANGE+=("$a")
  fi
  [ "${CUR_N[$a]}" -gt 1 ] && warn "$a has ${CUR_N[$a]} open rows — all will be closed"
  [ "$st" != "ACTIVE" ] && note "$a is $st — price row still updated (takes effect if re-activated)"
done

# The DB rounds a longer price on insert without an error (0.0001375 → 0.000138 on
# a 6-place column). Such an alias is left out — the rest of the table still applies.
[ -n "$DB_SCALE" ] || die "could not read the price column scale of model.model_pricings"
KEEP=(); SKIP_SCALE=()
for a in "${CHANGE[@]}"; do
  long_v=""
  for v in "${T_IN[$a]}" "${T_OUT[$a]}" "${T_C5M[$a]}" "${T_C1H[$a]}" "${T_CREAD[$a]}" \
           "${T_LIN[$a]}" "${T_LOUT[$a]}" "${T_LC5M[$a]}" "${T_LC1H[$a]}" "${T_LCREAD[$a]}"; do
    [ -n "$v" ] && [ "$(price_places "$v")" -gt "$DB_SCALE" ] && { long_v="$v"; break; }
  done
  if [ -n "$long_v" ]; then
    SKIP_SCALE+=("$a")
    warn "$a  SKIPPED — price $long_v needs $(price_places "$long_v") decimal places, model_pricings keeps $DB_SCALE (the DB would round it). Deploy the US-19 images (NUMERIC(12,8)) first."
  else
    KEEP+=("$a")
  fi
done
CHANGE=("${KEEP[@]}")

if [ ${#CHANGE[@]} -eq 0 ] && [ ${#SKIP_SCALE[@]} -gt 0 ]; then
  echo; die "nothing applied — ${SKIP_SCALE[*]} skipped (decimal places, see above)"
fi
if [ ${#CHANGE[@]} -eq 0 ]; then
  echo; ok "nothing to change — every registered alias already matches $(basename "$TSV")"; exit 0
fi

if [ "$APPLY" -eq 0 ]; then
  cat <<EOF

  Nothing applied yet (${#CHANGE[@]} alias(es) would change: ${CHANGE[*]}).
  Apply:  bash $(basename "$0")${ONLY:+ --alias ${ONLY[*]}} --apply
EOF
  exit 0
fi

# ── Apply (single transaction) ────────────────────────────────────────────
confirm "Closing the open price row and inserting the table row for: ${CHANGE[*]}
Past usage_logs keep their recorded cost; new calls use the new prices within 5 min."

SQL_APPLY="BEGIN;"
for a in "${CHANGE[@]}"; do SQL_APPLY="$SQL_APPLY
$(apply_sql_for "$a")"; done
SQL_APPLY="$SQL_APPLY
COMMIT;
\\pset format unaligned
\\pset fieldsep '|'
\\pset tuples_only on
SELECT 'V', model_alias, count(*),
       min(input_price_per_1k_tokens), min(output_price_per_1k_tokens),
       min(cache_creation_5m_price_per_1k_tokens), min(cache_creation_1h_price_per_1k_tokens),
       min(cache_read_price_per_1k_tokens),
       min(long_context_threshold_tokens), min(long_context_input_price_per_1k_tokens),
       min(long_context_output_price_per_1k_tokens), min(long_context_cache_creation_5m_price_per_1k_tokens),
       min(long_context_cache_creation_1h_price_per_1k_tokens), min(long_context_cache_read_price_per_1k_tokens)
  FROM model.model_pricings
 WHERE effective_until IS NULL AND model_alias IN ($(sql_list))
 GROUP BY model_alias ORDER BY model_alias;"

# Rollback = forward re-insert of the values that were open before this run.
RB="$SNAP_DIR/${TS}-08-pricing-rollback.sql"
{
  echo "-- generated $(date -Is) by 08-set-model-pricing.sh; re-inserts the previous open prices"
  echo "BEGIN;"
  for a in "${CHANGE[@]}"; do
    [ -n "${CUR_N[$a]:-}" ] || { echo "-- $a had no open row before; nothing to restore"; continue; }
    echo "UPDATE model.model_pricings SET effective_until = now() WHERE model_alias = '$a' AND effective_until IS NULL;"
    price_insert_sql "$a" "${CUR_IN[$a]}" "${CUR_OUT[$a]}" "${CUR_C5M[$a]}" "${CUR_C1H[$a]}" "${CUR_CREAD[$a]}" \
      "${CUR_LABOVE[$a]}" "${CUR_LIN[$a]}" "${CUR_LOUT[$a]}" "${CUR_LC5M[$a]}" "${CUR_LC1H[$a]}" "${CUR_LCREAD[$a]}"
  done
  echo "COMMIT;"
} > "$RB"
note "rollback SQL written: $RB"

hdr "Applying"
out=$(run_sql "$SQL_APPLY") || { printf '%s\n' "$out"; die "apply failed — transaction rolled back, nothing changed"; }

hdr "Verification (open rows after apply)"
fail=0
while IFS='|' read -r tag a n in out5 c5m c1h cread labove lin lout lc5m lc1h lcread; do
  [ "$tag" = "V" ] || continue
  keep=0; for c in "${CHANGE[@]}"; do [ "$c" = "$a" ] && keep=1; done
  [ $keep -eq 1 ] || continue
  got="$in / $out5 / $c5m / $c1h / $cread; long: $(long_str "$labove" "$lin" "$lout" "$lc5m" "$lc1h" "$lcread")"
  if [ "$n" = "1" ] && same_price "$in" "${T_IN[$a]}" && same_price "$out5" "${T_OUT[$a]}" \
     && same_price "$c5m" "${T_C5M[$a]}" && same_price "$c1h" "${T_C1H[$a]}" \
     && same_price "$cread" "${T_CREAD[$a]}" && [ "$labove" = "${T_LABOVE[$a]}" ] \
     && same_price "$lin" "${T_LIN[$a]}" && same_price "$lout" "${T_LOUT[$a]}" \
     && same_price "$lc5m" "${T_LC5M[$a]}" && same_price "$lc1h" "${T_LC1H[$a]}" \
     && same_price "$lcread" "${T_LCREAD[$a]}"; then
    ok "$a  open row = table ($got)"
  else
    bad "$a  open rows=$n  values=$got (expected table values)"; fail=1
  fi
done <<<"$out"
[ $fail -eq 0 ] || die "verification failed — inspect model.model_pricings for the aliases above"

hdr "Next steps"
cat <<EOF
  NOTE: the gateway caches model:{alias} (prices included) in Redis for 300s.
        New calls are billed at the new prices within 5 minutes; nothing to restart.
  Check: admin UI > Models > pricing, or bash 04-verify.sh
EOF
[ "$WAIT" -eq 1 ] && wait_cache 300
exit 0
