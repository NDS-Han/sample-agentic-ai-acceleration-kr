#!/bin/bash
# ---------------------------------------------------------------------------
# 13-bump-image-tags.sh
#
# WHAT: set the image.tag of every service in the deploy EC2's values file
#       (values-eks-fargate-<env>.yaml — the only copy with real endpoints) to
#       the tags the repo template carries at HEAD. Dry-run shows a per-service
#       table; --apply edits the file (backup first) and proves the result by
#       rendering the chart with helm.
# WHY:  rebuild-image.sh builds whatever tag helm renders. After an upstream
#       sync the code is new but the EC2 values still name the OLD tags, so a
#       rebuild would overwrite the old image under the old name — and helm
#       rollback by tag becomes impossible. New code needs new tag names, and
#       seven of them by hand is the easiest place to slip.
# UNDO: restore the backup this script writes (snapshots/<ts>-values-<env>.yaml.bak)
#
# Usage:
#   bash 13-bump-image-tags.sh [dev|prod]            # dry-run (default env: dev)
#   bash 13-bump-image-tags.sh dev --apply
#   bash 13-bump-image-tags.sh dev --values <path>   # test against another file
# ---------------------------------------------------------------------------
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/_lib.sh"

ENV="dev"; APPLY=0; VALUES=""
while [ $# -gt 0 ]; do
  case "$1" in
    dev|prod)   ENV="$1";     shift ;;
    --apply)    APPLY=1;      shift ;;
    --values)   VALUES="$2";  shift 2 ;;
    -h|--help)  sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

ROOT="$(cd "$LIB_DIR/../../.." && pwd)"                      # projects/awsome-ai-gateway
CHART="$ROOT/deployment/charts/llm-gateway"
V="${VALUES:-$CHART/values-eks-fargate-$ENV.yaml}"
[ -f "$V" ] || die "values file not found: $V"
command -v helm >/dev/null 2>&1 || die "helm not found"

# ── Template tags: the committed values file (git HEAD), not the EC2 copy ──
REL="$(git -C "$CHART" rev-parse --show-prefix 2>/dev/null)values-eks-fargate-$ENV.yaml"
TPL=$(mktemp); trap 'rm -f "$TPL" "$TPL.edited"' EXIT
git -C "$ROOT" show "HEAD:$REL" > "$TPL" 2>/dev/null \
  || die "cannot read the committed template (git show HEAD:$REL)"

render_tags() {   # <values file> → "service tag" lines (what helm would pull)
  helm template t "$CHART" -f "$1" 2>/dev/null \
    | grep -oE 'image: "[^"]+"' | sed 's/image: "//; s/"$//' | sort -u \
    | grep '/llm-gateway/' | sed 's#.*/llm-gateway/##; s#:# #'
}

declare -A WANT CUR NEW
while read -r svc tag; do WANT[$svc]=$tag; done < <(render_tags "$TPL")
while read -r svc tag; do CUR[$svc]=$tag;  done < <(render_tags "$V")
[ ${#WANT[@]} -gt 0 ] || die "template renders no images — chart/values problem"

# values top-level key → service (scheduler shares the admin-api image and must track its tag)
declare -A KEY_SVC=( [gatewayProxy]=gateway-proxy [adminApi]=admin-api [adminUi]=admin-ui
                     [scheduler]=admin-api [notificationWorker]=notification-worker
                     [costRecorderWorker]=cost-recorder-worker [migration]=migration )

# Keys whose block carries no explicit 4-space `tag:` (helm then uses the chart
# default). Such a key gets an explicit pin on --apply, so say so in the table.
declare -A PINNED
while read -r k; do PINNED[$k]=1; done < <(awk '
  /^[A-Za-z][A-Za-z0-9]*:/ { key = $1; sub(/:$/, "", key); seen[key] = 1 }
  key != "" && /^    tag:[ \t]*"/ { has[key] = 1 }
  END { for (k in seen) if (has[k]) print k }' "$V")
declare -A SVC_KEY=( [gateway-proxy]=gatewayProxy [admin-api]=adminApi [admin-ui]=adminUi
                     [notification-worker]=notificationWorker [cost-recorder-worker]=costRecorderWorker [migration]=migration )

hdr "Image tags — $(basename "$V") vs committed template"
CHANGES=0
printf '  %-22s %-22s %-22s\n' "service" "current (EC2 values)" "template (git HEAD)"
for svc in gateway-proxy admin-api admin-ui notification-worker cost-recorder-worker migration; do
  c="${CUR[$svc]:-(none)}"; w="${WANT[$svc]:-(none)}"; mark=""
  if [ "$c" != "$w" ]; then mark="  <- change"; CHANGES=$((CHANGES+1))
  elif [ -z "${PINNED[${SVC_KEY[$svc]}]:-}" ]; then c="$c (chart default)"; mark="  <- pin explicitly"; CHANGES=$((CHANGES+1)); fi
  printf '  %-22s %-22s %-22s%s\n' "$svc" "$c" "$w" "$mark"
done
[ -n "${PINNED[scheduler]:-}" ] && note "scheduler.image.tag follows admin-api (same image)" \
  || { note "scheduler.image.tag is not pinned in the values — it will be pinned to the admin-api tag"; CHANGES=$((CHANGES+1)); }

if [ "$CHANGES" -eq 0 ]; then echo; ok "tags already match the template — nothing to do"; exit 0; fi
if [ "$APPLY" -eq 0 ]; then
  cat <<EOF

  Nothing changed yet ($CHANGES service(s) differ).
  Apply:  bash $(basename "$0") $ENV --apply
EOF
  exit 0
fi

confirm "Rewriting image.tag for $CHANGES service(s) in $V (backup kept in $SNAP_DIR)."

BAK="$SNAP_DIR/${TS}-values-$ENV.yaml.bak"
cp "$V" "$BAK" || die "backup failed"
note "backup: $BAK"

# Block-aware edit, two passes over the backup. Pass 1 learns, per top-level
# key, whether an `image:` block and a 4-space `tag:` exist. Pass 2 rewrites
# the tag (dropping a now-stale inline comment), or inserts `image:`/`tag:` where the EC2 copy never had one (the
# template gains such keys over time — notificationWorker did). A managed key
# missing altogether is appended as a new block. Comments stay as they are;
# helm proves the result before the live file is touched.
awk -v map="$(for k in "${!KEY_SVC[@]}"; do printf '%s=%s;' "$k" "${WANT[${KEY_SVC[$k]}]:-}"; done)" '
BEGIN { n = split(map, kv, ";"); for (i = 1; i <= n; i++) { if (kv[i] == "") continue; split(kv[i], p, "="); if (p[2] != "") want[p[1]] = p[2] } }
function tagline(k) { return "    tag: \"" want[k] "\"" }
NR == FNR {
  if ($0 ~ /^[A-Za-z][A-Za-z0-9]*:/) { key = $1; sub(/:$/, "", key); seen[key] = 1 }
  else if (key != "") {
    if ($0 ~ /^  image:[ \t]*$/)   has_image[key] = 1
    if ($0 ~ /^    tag:[ \t]*"/)   has_tag[key] = 1
  }
  next
}
FNR == 1 { key = "" }
/^[A-Za-z][A-Za-z0-9]*:/ {
  key = $1; sub(/:$/, "", key); print
  if ((key in want) && !has_image[key] && !has_tag[key]) { print "  image:"; print tagline(key); done[key] = 1 }
  next
}
{
  if ((key in want) && !done[key]) {
    if ($0 ~ /^    tag:[ \t]*"/) { sub(/tag:[ \t]*"[^"]*"/, "tag: \"" want[key] "\""); sub(/[ \t]+#.*$/, ""); done[key] = 1 }
    else if ($0 ~ /^  image:[ \t]*$/ && !has_tag[key]) { print; print tagline(key); done[key] = 1; next }
  }
  print
}
END { for (k in want) if (!seen[k]) { print ""; print k ":"; print "  image:"; print tagline(k) } }
' "$BAK" "$BAK" > "$TPL.edited" || die "awk edit failed"

# Prove it with helm before touching the live file.
while read -r svc tag; do NEW[$svc]=$tag; done < <(render_tags "$TPL.edited")
bad_n=0
for svc in "${!WANT[@]}"; do
  [ "${NEW[$svc]:-}" = "${WANT[$svc]}" ] || { bad "$svc renders ${NEW[$svc]:-(none)}, expected ${WANT[$svc]}"; bad_n=$((bad_n+1)); }
done
[ "$bad_n" -eq 0 ] || die "edited values do not render the template tags — live file untouched (backup: $BAK)"
echo "  changed lines (backup -> new):"
diff "$BAK" "$TPL.edited" | grep -E '^[<>]' | sed 's/^/    /'
cp "$TPL.edited" "$V" || die "could not write $V"
ok "values updated: $V (helm renders the template tags for every service)"

hdr "Next steps"
cat <<EOF
  for s in migration gateway-proxy admin-api admin-ui notification-worker cost-recorder-worker; do
    ./deployment/scripts/rebuild-image.sh \$s $ENV || break; done
  ./deployment/scripts/install-eks.sh $ENV
EOF
