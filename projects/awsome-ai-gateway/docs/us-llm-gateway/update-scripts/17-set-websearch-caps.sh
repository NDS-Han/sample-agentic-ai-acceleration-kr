#!/bin/bash
# ---------------------------------------------------------------------------
# 17-set-websearch-caps.sh
#
# WHAT: put the four web-search cost caps into the deploy EC2's values file
#       (gatewayProxy.env) — result size per search, results per search,
#       searches per turn, loop iterations — then prove it by rendering the
#       chart. Dry-run shows what the chart renders today vs the target.
# WHY:  every search result is fed back into the next model turn and billed
#       again on every iteration. One Cowork stock-price question measured
#       6 calls · 12 searches · 275k input tokens · $3.62 (2026-09-16); a single
#       call with 4 parallel searches was 132k input tokens. The gateway's code
#       defaults (60000 chars / 10 results / 4 per turn / 5 iterations) are
#       generous; these caps cut the bill ~60% with no image rebuild.
# UNDO: restore the backup this script writes, then install-eks.sh again.
#
# Targets come from config.env (WEB_SEARCH_MAX_RESULT_CHARS etc.); unset means
# the defaults below. Takes effect at the next install-eks.sh <env> (rollout).
# Also carries WEB_SEARCH_TRACE_MODE (text | native — the 2026-09-17 native-block
# probe for Cowork; string-valued) and WEB_SEARCH_DIGEST_CHARS (per-result excerpt
# replayed in native mode; 0 = same as the trimmed result the model saw).
#
# Usage:
#   bash 17-set-websearch-caps.sh                 # dry-run
#   bash 17-set-websearch-caps.sh --apply
#   bash 17-set-websearch-caps.sh --values <path> # test against another file
# ---------------------------------------------------------------------------
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/_lib.sh"

APPLY=0; VALUES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)   APPLY=1;     shift ;;
    --values)  VALUES="$2"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

load_config
: "${WEB_SEARCH_MAX_RESULT_CHARS:=12000}"
: "${WEB_SEARCH_MAX_RESULTS_DEFAULT:=5}"
: "${WEB_SEARCH_MAX_SEARCHES_PER_TURN:=3}"
: "${WEB_SEARCH_MAX_ITERATIONS:=2}"
: "${WEB_SEARCH_DIGEST_CHARS:=0}"
: "${WEB_SEARCH_TRACE_MODE:=text}"
KEYS=(WEB_SEARCH_MAX_RESULT_CHARS WEB_SEARCH_MAX_RESULTS_DEFAULT WEB_SEARCH_MAX_SEARCHES_PER_TURN WEB_SEARCH_MAX_ITERATIONS WEB_SEARCH_DIGEST_CHARS)
STR_KEYS=(WEB_SEARCH_TRACE_MODE)
ALL_KEYS=("${KEYS[@]}" "${STR_KEYS[@]}")
declare -A CODE_DEFAULT=( [WEB_SEARCH_MAX_RESULT_CHARS]=60000 [WEB_SEARCH_MAX_RESULTS_DEFAULT]=10
                          [WEB_SEARCH_MAX_SEARCHES_PER_TURN]=4 [WEB_SEARCH_MAX_ITERATIONS]=5
                          [WEB_SEARCH_DIGEST_CHARS]=0 [WEB_SEARCH_TRACE_MODE]=text )
for k in "${KEYS[@]}"; do [[ "${!k}" =~ ^[0-9]+$ ]] || die "$k must be an integer (config.env): ${!k}"; done
for k in "${STR_KEYS[@]}"; do [[ "${!k}" =~ ^(text|native)$ ]] || die "$k must be text|native (config.env): ${!k}"; done

ROOT="$(cd "$LIB_DIR/../../.." && pwd)"
CHART="$ROOT/deployment/charts/llm-gateway"
V="${VALUES:-$CHART/values-eks-fargate-$DEPLOY_ENV.yaml}"
[ -f "$V" ] || die "values file not found: $V"
command -v helm >/dev/null 2>&1 || die "helm not found"

render_env() {   # <values> → "KEY value" for the gateway-proxy Deployment env
  helm template t "$CHART" -f "$1" 2>/dev/null | awk '
    /^kind: Deployment/ { dep=1 } /^kind: / && !/Deployment/ { dep=0 }
    /^  name: .*gateway-proxy$/ && dep { gp=1 } /^  name: / && !/gateway-proxy$/ { gp=0 }
    gp && /- name: WEB_SEARCH_/ { k=$3 } gp && k!="" && /value:/ { v=$2; gsub(/"/,"",v); print k, v; k="" }'
}
declare -A CUR
while read -r k v; do CUR[$k]=$v; done < <(render_env "$V")

hdr "web search caps — $(basename "$V") (rendered) vs target"
printf '  %-34s %-24s %s\n' "key" "rendered now" "target"
CHANGES=0
for k in "${ALL_KEYS[@]}"; do
  cur="${CUR[$k]:-}"; want="${!k}"; shown="${cur:-(unset -> code default ${CODE_DEFAULT[$k]})}"
  if [ "$cur" = "$want" ]; then printf '  %-34s %-24s %s\n' "$k" "$shown" "$want"
  else printf '  %-34s %-24s %s  <- change\n' "$k" "$shown" "$want"; CHANGES=$((CHANGES+1)); fi
done

if [ "$CHANGES" -eq 0 ]; then echo; ok "values already carries the target caps — nothing to do"; exit 0; fi
if [ "$APPLY" -eq 0 ]; then
  cat <<EOT

  Nothing changed yet ($CHANGES key(s) differ).
  Apply:  bash $(basename "$0") --apply     (then ./deployment/scripts/install-eks.sh $DEPLOY_ENV)
EOT
  exit 0
fi

confirm "Writing $CHANGES web-search cap(s) under gatewayProxy.env in $V (backup kept in $SNAP_DIR)."
BAK="$SNAP_DIR/${TS}-values-$DEPLOY_ENV-17.bak"
cp "$V" "$BAK" || die "backup failed"
note "backup: $BAK"

# Two passes over the backup. Pass 1: which of the keys exist under
# gatewayProxy: -> env:, and whether WEB_SEARCH_ENABLED is there to anchor on.
# Pass 2: rewrite existing keys in place; insert missing ones after
# WEB_SEARCH_ENABLED (or right after `  env:` if that anchor is absent).
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
awk -v map="$(for k in "${ALL_KEYS[@]}"; do printf '%s=%s;' "$k" "${!k}"; done)" '
BEGIN { n = split(map, kv, ";"); for (i = 1; i <= n; i++) { if (kv[i] == "") continue; split(kv[i], p, "="); want[p[1]] = p[2] } }
function line(k) { return "    " k ": \"" want[k] "\"" }
function emit_missing(   k) { for (k in want) if (!has[k] && !done[k]) { print line(k); done[k] = 1 } }
NR == FNR {
  if ($0 ~ /^[A-Za-z][A-Za-z0-9]*:/) { top = $1; sub(/:$/, "", top); sub2 = "" }
  else if ($0 ~ /^  [A-Za-z][A-Za-z0-9]*:/) { sub2 = $1; sub(/:$/, "", sub2); if (top == "gatewayProxy" && sub2 == "env") seen_env = 1 }
  else if (top == "gatewayProxy" && sub2 == "env" && $0 ~ /^    [A-Z_]+:/) { k = $1; sub(/:$/, "", k); has[k] = 1; if (k == "WEB_SEARCH_ENABLED") anchor = 1 }
  next
}
FNR == 1 { top = ""; sub2 = ""; if (!seen_env) { print "ERROR: no gatewayProxy: -> env: block in values" > "/dev/stderr"; exit 2 } }
/^[A-Za-z][A-Za-z0-9]*:/ { if (top == "gatewayProxy" && sub2 == "env") emit_missing(); top = $1; sub(/:$/, "", top); sub2 = ""; print; next }
/^  [A-Za-z][A-Za-z0-9]*:/ {
  if (top == "gatewayProxy" && sub2 == "env") emit_missing()
  sub2 = $1; sub(/:$/, "", sub2); print
  if (top == "gatewayProxy" && sub2 == "env" && !anchor) emit_missing()
  next
}
{
  if (top == "gatewayProxy" && sub2 == "env" && $0 ~ /^    [A-Z_]+:/) {
    k = $1; sub(/:$/, "", k)
    if (k in want) { print line(k); done[k] = 1; next }
    print
    if (k == "WEB_SEARCH_ENABLED") emit_missing()
    next
  }
  print
}
END { if (top == "gatewayProxy" && sub2 == "env") emit_missing() }
' "$BAK" "$BAK" > "$TMP" || die "values edit failed — live file untouched (backup: $BAK)"

declare -A NEW
while read -r k v; do NEW[$k]=$v; done < <(render_env "$TMP")
bad_n=0
for k in "${ALL_KEYS[@]}"; do [ "${NEW[$k]:-}" = "${!k}" ] || { bad "$k renders ${NEW[$k]:-(unset)}, expected ${!k}"; bad_n=$((bad_n+1)); }; done
[ "$bad_n" -eq 0 ] || die "edited values do not render the target caps — live file untouched (backup: $BAK)"
echo "  changed lines (backup -> new):"; diff "$BAK" "$TMP" | grep -E '^[<>]' | sed 's/^/    /'
cp "$TMP" "$V" || die "could not write $V"
ok "values updated: $V (helm renders all ${#ALL_KEYS[@]} keys)"

hdr "Next steps"
cat <<EOT
  cd $ROOT && ./deployment/scripts/install-eks.sh $DEPLOY_ENV     # rollout only, no image rebuild
  afterwards: bash 16-usage-recent.sh --hours 1 --client cowork   # compare tokens/cost per request
EOT
