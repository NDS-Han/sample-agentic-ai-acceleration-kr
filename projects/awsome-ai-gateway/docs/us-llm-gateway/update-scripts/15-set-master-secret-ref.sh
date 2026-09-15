#!/bin/bash
# ---------------------------------------------------------------------------
# 15-set-master-secret-ref.sh
#
# WHAT: point the chart at the RDS-managed master-password secret. Writes two
#       keys into the deploy EC2's values file (database.external.
#       masterPasswordRemoteKey / masterPasswordRemoteProperty — and masterUser
#       if the live master user differs), then proves it by rendering the chart.
# WHY:  the migration Job runs init SQL and GRANTs as the DB master user, so
#       ESO must fetch the master password from the secret RDS itself rotates
#       (rds!cluster-<uuid>). Without these keys ESO looks for master_password
#       in /llm-gateway/<env>/db — a copy terraform no longer keeps and RDS
#       rotates every 7 days — and the Job dies on a 5-minute timeout or an
#       auth error. Nothing is served from that path, so the failure is quiet.
# UNDO: restore the backup this script writes (snapshots/<ts>-values-<env>-15.bak)
#
# Usage:
#   bash 15-set-master-secret-ref.sh                 # dry-run: live facts vs rendered chart
#   bash 15-set-master-secret-ref.sh --apply
#   bash 15-set-master-secret-ref.sh --cluster <id>  # Aurora cluster id (default llm-gateway-<env>)
#   bash 15-set-master-secret-ref.sh --values <path> # test against another file
# ---------------------------------------------------------------------------
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/_lib.sh"

APPLY=0; VALUES=""; CLUSTER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)    APPLY=1;      shift ;;
    --values)   VALUES="$2";  shift 2 ;;
    --cluster)  CLUSTER="$2"; shift 2 ;;
    -h|--help)  sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

require_env
ROOT="$(cd "$LIB_DIR/../../.." && pwd)"                      # projects/awsome-ai-gateway
CHART="$ROOT/deployment/charts/llm-gateway"
V="${VALUES:-$CHART/values-eks-fargate-$DEPLOY_ENV.yaml}"
CL="${CLUSTER:-${HELM_RELEASE}-${DEPLOY_ENV}}"
[ -f "$V" ] || die "values file not found: $V"
command -v helm >/dev/null 2>&1 || die "helm not found"

# ── Live facts: master user + the secret RDS rotates ─────────────────────
hdr "Aurora cluster $CL"
read -r MASTER_USER SECRET_ARN < <(aws rds describe-db-clusters --db-cluster-identifier "$CL" \
  --query 'DBClusters[0].[MasterUsername,MasterUserSecret.SecretArn]' --output text 2>/dev/null)
[ -n "${MASTER_USER:-}" ] && [ "$MASTER_USER" != "None" ] || die "cannot read cluster $CL (wrong --cluster, or no rds:DescribeDBClusters permission)"
[ -n "${SECRET_ARN:-}" ] && [ "$SECRET_ARN" != "None" ] \
  || die "cluster $CL has no RDS-managed master secret (manage_master_user_password is off) — nothing to point at"
SECRET_NAME=$(aws secretsmanager describe-secret --secret-id "$SECRET_ARN" --query Name --output text 2>/dev/null)
[ -n "$SECRET_NAME" ] && [ "$SECRET_NAME" != "None" ] || die "cannot resolve the secret name for $SECRET_ARN"
note "master user      $MASTER_USER"
note "master secret    $SECRET_NAME"

# ESO must be allowed to read rds!cluster-* (declared in the irsa module since the first install)
POL_ARN="arn:aws:iam::$AWS_ACCOUNT_ID:policy/${HELM_RELEASE}-${DEPLOY_ENV}-external-secrets"
ver=$(aws iam get-policy --policy-arn "$POL_ARN" --query Policy.DefaultVersionId --output text 2>/dev/null)
doc=$(aws iam get-policy-version --policy-arn "$POL_ARN" --version-id "${ver:-v1}" --query PolicyVersion.Document --output json 2>/dev/null)
if [[ "$doc" == *'rds!cluster-*'* ]]; then ok "ESO IAM policy allows secret:rds!cluster-* ($POL_ARN $ver)"
else warn "could not confirm rds!cluster-* read on $POL_ARN — if the ExternalSecret stays SecretSyncedError after ⑦, terraform apply the irsa module"; fi

# ── What the chart renders today from the values file ─────────────────────
render_ref() {   # <values> → "key|property|masterUser" as the chart would render them
  helm template t "$CHART" -f "$1" 2>/dev/null | awk '
    /secretKey: master_password/ { f=1; next }
    f && /key:/      { k=$2; gsub(/"/,"",k) }
    f && /property:/ { p=$2; gsub(/"/,"",p); f=0 }
    /- name: DB_MASTER_USER/ { g=1; next }
    g && /value:/ { u=$2; gsub(/"/,"",u); g=0 }
    END { print k "|" p "|" u }'
}
IFS='|' read -r CUR_KEY CUR_PROP CUR_USER <<<"$(render_ref "$V")"

hdr "Rendered chart vs live"
printf '  %-28s %-46s %s\n' "" "rendered from $(basename "$V")" "live"
declare -A WANT
CHANGES=0
if [ "$CUR_KEY" = "$SECRET_NAME" ]; then printf '  %-28s %-46s %s\n' "masterPasswordRemoteKey" "$CUR_KEY" "$SECRET_NAME"
else printf '  %-28s %-46s %s  <- change\n' "masterPasswordRemoteKey" "${CUR_KEY:-(unset -> /llm-gateway/$DEPLOY_ENV/db)}" "$SECRET_NAME"; WANT[masterPasswordRemoteKey]="$SECRET_NAME"; CHANGES=$((CHANGES+1)); fi
if [ "$CUR_PROP" = "password" ]; then printf '  %-28s %-46s %s\n' "masterPasswordRemoteProperty" "$CUR_PROP" "password"
else printf '  %-28s %-46s %s  <- change\n' "masterPasswordRemoteProperty" "${CUR_PROP:-(unset)}" "password"; WANT[masterPasswordRemoteProperty]="password"; CHANGES=$((CHANGES+1)); fi
if [ "$CUR_USER" = "$MASTER_USER" ]; then printf '  %-28s %-46s %s\n' "masterUser" "$CUR_USER" "$MASTER_USER"
else printf '  %-28s %-46s %s  <- change\n' "masterUser" "${CUR_USER:-(unset)}" "$MASTER_USER"; WANT[masterUser]="$MASTER_USER"; CHANGES=$((CHANGES+1)); fi

if [ "$CHANGES" -eq 0 ]; then echo; ok "values already points the migration Job at the RDS-managed master secret — nothing to do"; exit 0; fi
if [ "$APPLY" -eq 0 ]; then
  cat <<EOT

  Nothing changed yet ($CHANGES key(s) to set under database.external).
  Apply:  bash $(basename "$0") --apply
EOT
  exit 0
fi

confirm "Writing $CHANGES key(s) under database.external in $V (backup kept in $SNAP_DIR)."
BAK="$SNAP_DIR/${TS}-values-$DEPLOY_ENV-15.bak"
cp "$V" "$BAK" || die "backup failed"
note "backup: $BAK"

# Two passes: learn which keys exist under database: -> external:, then replace
# them in place or insert the missing ones right after `    host:` (or after
# `  external:` when there is no host line). Comments stay untouched.
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
awk -v map="$(for k in "${!WANT[@]}"; do printf '%s=%s;' "$k" "${WANT[$k]}"; done)" '
BEGIN { n = split(map, kv, ";"); for (i = 1; i <= n; i++) { if (kv[i] == "") continue; split(kv[i], p, "="); want[p[1]] = p[2] } }
function line(k) { return "    " k ": \"" want[k] "\"" }
function emit_missing(   k) { for (k in want) if (!has[k] && !done[k]) { print line(k); done[k] = 1 } }
NR == FNR {
  if ($0 ~ /^[A-Za-z][A-Za-z0-9]*:/) { top = $1; sub(/:$/, "", top); sub2 = "" }
  else if ($0 ~ /^  [A-Za-z][A-Za-z0-9]*:/) { sub2 = $1; sub(/:$/, "", sub2); if (top == "database" && sub2 == "external") seen_ext = 1 }
  else if (top == "database" && sub2 == "external" && $0 ~ /^    [A-Za-z][A-Za-z0-9]*:/) { k = $1; sub(/:$/, "", k); has[k] = 1; if (k == "host") has_host = 1 }
  next
}
FNR == 1 { top = ""; sub2 = ""; if (!seen_ext) { print "ERROR: no database: -> external: block in values" > "/dev/stderr"; exit 2 } }
/^[A-Za-z][A-Za-z0-9]*:/ { if (top == "database" && sub2 == "external") emit_missing(); top = $1; sub(/:$/, "", top); sub2 = ""; print; next }
/^  [A-Za-z][A-Za-z0-9]*:/ {
  if (top == "database" && sub2 == "external") emit_missing()
  sub2 = $1; sub(/:$/, "", sub2); print
  if (top == "database" && sub2 == "external" && !has_host) emit_missing()
  next
}
{
  if (top == "database" && sub2 == "external" && $0 ~ /^    [A-Za-z][A-Za-z0-9]*:/) {
    k = $1; sub(/:$/, "", k)
    if (k in want) { print line(k); done[k] = 1; next }
    print
    if (k == "host") emit_missing()
    next
  }
  print
}
END { if (top == "database" && sub2 == "external") emit_missing() }
' "$BAK" "$BAK" > "$TMP" || die "values edit failed — live file untouched (backup: $BAK)"

IFS='|' read -r NEW_KEY NEW_PROP NEW_USER <<<"$(render_ref "$TMP")"
if [ "$NEW_KEY" = "$SECRET_NAME" ] && [ "$NEW_PROP" = "password" ] && [ "$NEW_USER" = "$MASTER_USER" ]; then
  echo "  changed lines (backup -> new):"; diff "$BAK" "$TMP" | grep -E '^[<>]' | sed 's/^/    /'
  cp "$TMP" "$V" || die "could not write $V"
  ok "values updated: $V (chart renders key=$NEW_KEY property=$NEW_PROP masterUser=$NEW_USER)"
else
  die "edited values render key=$NEW_KEY property=$NEW_PROP masterUser=$NEW_USER — live file untouched (backup: $BAK); set the keys by hand under database.external"
fi

hdr "Next steps"
cat <<EOT
  The keys take effect at the next install-eks.sh $DEPLOY_ENV (8-D ⑦): the ExternalSecret
  is recreated, ESO fetches the master password from $SECRET_NAME, the migration
  Job reads it as DB_MASTER_PASSWORD.
EOT
