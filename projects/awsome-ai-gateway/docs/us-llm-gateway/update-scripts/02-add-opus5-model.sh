#!/bin/bash
# ---------------------------------------------------------------------------
# 02-add-opus5-model.sh
#
# WHAT: register the model alias from config.env plus its price row
#       (existing models are left untouched — unless --remap, see below)
# WHY:  the Bedrock model is already callable in the region, but the gateway
#       has no alias for it, so no client can request it
#       Values come from config.env: MODEL_ALIAS / MODEL_PROVIDER_ID /
#       MODEL_DISPLAY_NAME / MODEL_DESCRIPTION
#       --remap: upstream migration 0027 pre-registers claude-opus-5 /
#       claude-sonnet-5 as global.anthropic.* (Global routing, Global prices).
#       A plain INSERT ... ON CONFLICT DO NOTHING then changes nothing and the
#       gateway keeps routing globally. --remap rewrites provider_model_id of
#       an existing alias to MODEL_PROVIDER_ID (e.g. us.anthropic.*) and sets it
#       ACTIVE. Prices are NOT touched here — run 08-set-model-pricing.sh.
# UNDO: 99-rollback.sh — flips status to INACTIVE (never DELETE: several FKs
#       reference model_aliases and none declare ON DELETE). For --remap the
#       previous provider_model_id is written to snapshots/<ts>-02-remap-rollback.sql
#
# Usage:
#   Fill MODEL_PRICE_* in config.env, then:
#       bash 02-add-opus5-model.sh              # dry-run
#       bash 02-add-opus5-model.sh --apply
#
#   Or pass prices on the command line, which overrides config.env:
#       bash 02-add-opus5-model.sh --input 0.005 --output 0.025 \
#            --cache-5m 0.00625 --cache-1h 0.01 --cache-read 0.0005 --apply
#
#   If team_allowed_models is in whitelist mode (00 reports this):
#   ... --team-id <uuid>   also inserts the matching team allow row
# ---------------------------------------------------------------------------
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/_lib.sh"

P_IN=""; P_OUT=""; P_C5M=""; P_C1H=""; P_CREAD=""
TEAM_ID=""; APPLY=0; REMAP=0

usage() {
  cat <<EOF
$(basename "$0") — register the model alias defined in config.env

Prices (USD per 1K tokens) — required, from config.env or these flags
  --input      <n>   input tokens          (config: MODEL_PRICE_INPUT)
  --output     <n>   output tokens         (config: MODEL_PRICE_OUTPUT)
  --cache-5m   <n>   cache write, 5m TTL   (config: MODEL_PRICE_CACHE_5M)
  --cache-1h   <n>   cache write, 1h TTL   (config: MODEL_PRICE_CACHE_1H)
  --cache-read <n>   cache read            (config: MODEL_PRICE_CACHE_READ)

  Flags win over config.env. Filling config.env is preferred: the values stay
  recorded, and MODEL_PRICE_ASOF documents when they were last checked.

Optional
  --team-id <uuid>   only when team_allowed_models is in whitelist mode
  --remap            if the alias already exists with a different
                     provider_model_id (upstream seeds claude-opus-5 as
                     global.anthropic.*), rewrite it to MODEL_PROVIDER_ID and
                     set ACTIVE. Without this flag an existing alias is left
                     as is and the dry-run tells you so.
  --apply            actually apply (otherwise dry-run)

Why prices are mandatory
  With no price row, router_service.py:51-52 substitutes zero without raising
  and cost_recorder.py:24-39 multiplies straight through, so every call is
  logged at \$0. Requests keep succeeding, which makes this very easy to miss
  while the budget is quietly bypassed. Hence this script refuses to proceed
  without explicit prices.

  Look the prices up on the AWS Bedrock pricing page — the Pricing API does
  not expose newer models, so they cannot be fetched automatically.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --input)      P_IN="$2";    shift 2 ;;
    --output)     P_OUT="$2";   shift 2 ;;
    --cache-5m)   P_C5M="$2";   shift 2 ;;
    --cache-1h)   P_C1H="$2";   shift 2 ;;
    --cache-read) P_CREAD="$2"; shift 2 ;;
    --team-id)    TEAM_ID="$2"; shift 2 ;;
    --remap)      REMAP=1;      shift ;;
    --apply)      APPLY=1;      shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

require_env

# config.env supplies the prices; command-line flags override them.
P_IN="${P_IN:-$MODEL_PRICE_INPUT}"
P_OUT="${P_OUT:-$MODEL_PRICE_OUTPUT}"
P_C5M="${P_C5M:-$MODEL_PRICE_CACHE_5M}"
P_C1H="${P_C1H:-$MODEL_PRICE_CACHE_1H}"
P_CREAD="${P_CREAD:-$MODEL_PRICE_CACHE_READ}"

# Config supplies the model identity; these locals keep the SQL readable.
ALIAS="$MODEL_ALIAS"
MODEL_ID="$MODEL_PROVIDER_ID"
DISPLAY="$MODEL_DISPLAY_NAME"
DESCRIPTION="$MODEL_DESCRIPTION"

# ── Validate prices ─────────────────────────────────────────────────────────
missing=()
[ -z "$P_IN" ]    && missing+=(--input)
[ -z "$P_OUT" ]   && missing+=(--output)
[ -z "$P_C5M" ]   && missing+=(--cache-5m)
[ -z "$P_C1H" ]   && missing+=(--cache-1h)
[ -z "$P_CREAD" ] && missing+=(--cache-read)
if [ ${#missing[@]} -gt 0 ]; then
  bad "prices not set: ${missing[*]}"
  note "Fill MODEL_PRICE_* in config.env, or pass the flags below."
  echo
  usage
  exit 1
fi
for v in "$P_IN" "$P_OUT" "$P_C5M" "$P_C1H" "$P_CREAD"; do
  [[ "$v" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "prices must be numeric: $v"
done
# A zero price row behaves exactly like a missing one: cost aggregates to 0.
for pair in "input:$P_IN" "output:$P_OUT"; do
  name="${pair%%:*}"; val="${pair#*:}"
  awk "BEGIN{exit !($val > 0)}" || die "$name price is 0 — cost tracking would be meaningless."
done

# ── SQL ─────────────────────────────────────────────────────────────────────
# ── Current state of the alias ─────────────────────────────────────────
# upstream migration 0027 seeds claude-opus-5 / claude-sonnet-5 as
# global.anthropic.* — a plain INSERT ... ON CONFLICT would silently do nothing.
CUR_MODEL_ID=""; CUR_STATUS=""; CUR_PRICE_ROWS=0
cur=$(run_sql "\\pset format unaligned
\\pset fieldsep '|'
\\pset tuples_only on
SELECT 'A', provider_model_id, status FROM model.model_aliases WHERE alias = '$ALIAS';
SELECT 'P', count(*) FROM model.model_pricings WHERE model_alias = '$ALIAS';") \
  || die "could not read the current state of $ALIAS (see output above)"
while IFS='|' read -r tag f1 f2; do
  case "$tag" in
    A) CUR_MODEL_ID="$f1"; CUR_STATUS="$f2" ;;
    P) CUR_PRICE_ROWS="$f1" ;;
  esac
done <<<"$cur"

ALIAS_ACTION="INSERT"                       # INSERT | SAME | REMAP | EXISTS
if [ -n "$CUR_MODEL_ID" ]; then
  if [ "$CUR_MODEL_ID" = "$MODEL_ID" ] && [ "$CUR_STATUS" = "ACTIVE" ]; then
    ALIAS_ACTION="SAME"
  elif [ "$REMAP" -eq 1 ]; then
    ALIAS_ACTION="REMAP"
  else
    ALIAS_ACTION="EXISTS"
  fi
fi

# Shape follows the existing seed (db/init/03_seed_data.sql:97-111) and
# migration 0004_add_opus_4_6.py:34-46.
SQL_ALIAS="INSERT INTO model.model_aliases
    (alias, provider, provider_model_id, endpoint_url, api_format, status,
     description, display_name, created_by)
VALUES ('$ALIAS', 'BEDROCK', '$MODEL_ID', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
        '$DESCRIPTION', '$DISPLAY', '$SEED_ADMIN_UUID')
ON CONFLICT (alias) DO NOTHING;"
if [ "$ALIAS_ACTION" = "REMAP" ]; then
  SQL_ALIAS="UPDATE model.model_aliases
   SET provider_model_id = '$MODEL_ID', status = 'ACTIVE',
       display_name = '$DISPLAY', description = '$DESCRIPTION'
 WHERE alias = '$ALIAS';"
fi

# effective_from must be <= now(): a future-dated row behaves like no row.
SQL_PRICE="INSERT INTO model.model_pricings
    (id, model_alias,
     input_price_per_1k_tokens, output_price_per_1k_tokens,
     cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
     cache_read_price_per_1k_tokens,
     effective_from, created_by)
SELECT gen_random_uuid(), '$ALIAS',
       $P_IN, $P_OUT, $P_C5M, $P_C1H, $P_CREAD,
       now(), '$SEED_ADMIN_UUID'
WHERE NOT EXISTS (
    SELECT 1 FROM model.model_pricings WHERE model_alias = '$ALIAS');"

SQL_TEAM=""
if [ -n "$TEAM_ID" ]; then
  SQL_TEAM="INSERT INTO model.team_allowed_models (team_id, model_alias, created_by)
VALUES ('$TEAM_ID', '$ALIAS', '$SEED_ADMIN_UUID')
ON CONFLICT DO NOTHING;"
fi

hdr "What will be registered"
cat <<EOF
  alias              $ALIAS
  provider           BEDROCK
  provider_model_id  $MODEL_ID
                     ^ if this is an INFERENCE_PROFILE-only model it needs a geo prefix
  api_format         BEDROCK_NATIVE
  status             ACTIVE

  Prices (USD per 1K tokens)
    input        $P_IN
    output       $P_OUT
    cache 5m     $P_C5M
    cache 1h     $P_C1H
    cache read   $P_CREAD
    as of        ${MODEL_PRICE_ASOF:-(not recorded)}
EOF
[ -n "$TEAM_ID" ] && echo "  team_allowed_models  allow row for team $TEAM_ID"

hdr "Alias state in the DB"
case "$ALIAS_ACTION" in
  INSERT) note "$ALIAS is not registered — will INSERT" ;;
  SAME)   ok "$ALIAS already maps to $MODEL_ID (ACTIVE) — alias row unchanged" ;;
  REMAP)  warn "$ALIAS exists as $CUR_MODEL_ID ($CUR_STATUS) — will REMAP to $MODEL_ID and set ACTIVE" ;;
  EXISTS) warn "$ALIAS exists as $CUR_MODEL_ID ($CUR_STATUS), not $MODEL_ID."
          note "Nothing will be changed. Re-run with --remap to rewrite provider_model_id." ;;
esac
if [ "$CUR_PRICE_ROWS" -gt 0 ]; then
  note "$ALIAS already has $CUR_PRICE_ROWS price row(s) — the price INSERT above is skipped."
  note "Set prices with: bash 08-set-model-pricing.sh --alias $ALIAS"
fi

hdr "Currently ACTIVE models"
run_sql "SELECT alias, provider_model_id FROM model.model_aliases
          WHERE status='ACTIVE' ORDER BY alias;"

if [ "$APPLY" -eq 0 ]; then
  cat <<EOF

  Nothing applied yet.
  Apply:  bash $(basename "$0") ${TEAM_ID:+--team-id $TEAM_ID }$( { [ "$ALIAS_ACTION" = EXISTS ] || [ "$ALIAS_ACTION" = REMAP ]; } && printf -- '--remap ' )--apply
EOF
  exit 0
fi

[ "$ALIAS_ACTION" = "EXISTS" ] && die "$ALIAS already exists as $CUR_MODEL_ID — pass --remap to change it, or leave it."
case "$ALIAS_ACTION" in
  REMAP) confirm "Remapping $ALIAS: $CUR_MODEL_ID -> $MODEL_ID (status ACTIVE). Other models are not modified." ;;
  *)     confirm "Registering $ALIAS as ACTIVE. Existing models are not modified." ;;
esac

hdr "Applying"
run_sql "$SQL_ALIAS" || die "alias $ALIAS_ACTION failed"
ok "alias $ALIAS_ACTION done"
if [ "$CUR_PRICE_ROWS" -gt 0 ]; then
  note "price rows already exist for $ALIAS — pricing INSERT skipped (use 08-set-model-pricing.sh)"
else
  run_sql "$SQL_PRICE" || die "pricing INSERT failed"
  ok "pricing registered"
fi
if [ -n "$SQL_TEAM" ]; then
  run_sql "$SQL_TEAM" || die "team_allowed_models INSERT failed"
  ok "team allow row registered"
fi

printf 'UPDATE model.model_aliases SET status='"'"'INACTIVE'"'"' WHERE alias='"'"'%s'"'"';\n' \
  "$ALIAS" > "$SNAP_DIR/${TS}-02-opus5-rollback.sql"
if [ "$ALIAS_ACTION" = "REMAP" ]; then
  printf "UPDATE model.model_aliases SET provider_model_id='%s', status='%s' WHERE alias='%s';\n" \
    "$CUR_MODEL_ID" "$CUR_STATUS" "$ALIAS" > "$SNAP_DIR/${TS}-02-remap-rollback.sql"
  note "remap rollback SQL written: $SNAP_DIR/${TS}-02-remap-rollback.sql"
  note "model:$ALIAS is cached in Redis for 300s — the new mapping is live within 5 min"
fi

hdr "Verification"
run_sql "SELECT a.alias, a.provider_model_id, a.status,
                p.input_price_per_1k_tokens, p.output_price_per_1k_tokens
           FROM model.model_aliases a
           LEFT JOIN model.model_pricings p ON p.model_alias = a.alias
          WHERE a.alias = '$ALIAS';"

hdr "Next steps"
cat <<EOF
  NOTE: takes up to 5 minutes to take effect (model / model:list cache TTL 300s).
        Until then Claude Code gets a 404 from GET /v1/models/{id} and will not
        even attempt a call — an easy point to misread as "it does not work".

  Next:  bash 03-create-cloudfront.sh
         (after 5 minutes) bash 04-verify.sh
EOF
