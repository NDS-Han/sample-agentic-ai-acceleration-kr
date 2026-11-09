#!/bin/bash
# ---------------------------------------------------------------------------
# 20-enable-body-logging.sh
#
# WHAT: open the infra gate for request/response body logging —
#       docs/us-llm-gateway/ops/8-V-body-logging.md.
#         (no step)  status: tfvars flag + terraform outputs + pod env + values
#         tfvars     append enable_body_logging = true to terraform.tfvars
#                    (terraform plan/apply stays a manual step)
#         env        put FIREHOSE_STREAM_NAME + BODY_LOG_S3_BUCKET under
#                    gatewayProxy.env, and BODY_LOG_S3_BUCKET under adminApi.env
#                    (the admin-api key drives the viewer + provisioned gate)
#         verify     read-only: bucket exists / stream ACTIVE / pod env set
#         disable    remove the env keys (collection stops; infra stays)
# WHY:  body logging is gated twice — the admin /monitoring toggle only works
#       when the proxy pod actually has the sink env vars. The values file is
#       the source of truth because deploys go through install-eks.sh, which
#       re-injects terraform outputs via --set on every run: a bare
#       helm upgrade -f would drop those injections and roll release values
#       back to placeholders. So this script writes the two keys into
#       values-eks-fargate-<env>.yaml and leaves the rollout to
#       install-eks.sh — exactly like 19-admin-login.sh leaves helm to it.
# UNDO: restore the backup --apply prints (snapshots/), then install-eks.sh
#       <env> for values, or edit terraform.tfvars for the flag.
#
# ⚠️  When collection is ON, full request JSON and streaming SSE text are
#     stored to S3 unmasked — user prompts included. Review privacy/security
#     before enabling the admin toggle. Objects expire via lifecycle (90d).
#
# Usage:
#   bash 20-enable-body-logging.sh                 # status
#   bash 20-enable-body-logging.sh tfvars [--apply]
#   bash 20-enable-body-logging.sh env [--apply]
#   bash 20-enable-body-logging.sh verify
#   bash 20-enable-body-logging.sh disable [--apply]
#   --values <path>  edit/render another values file (test)
#
# Order: tfvars → terraform plan/apply → env → install-eks.sh <env> → verify
#        → admin /monitoring toggle ON. disable → install-eks.sh <env>.
# ---------------------------------------------------------------------------
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/_lib.sh"

STEP=status; APPLY=0; VALUES=""
while [ $# -gt 0 ]; do
  case "$1" in
    status|tfvars|env|verify|disable) STEP="$1"; shift ;;
    --apply)   APPLY=1; shift ;;
    --values)  VALUES="$2"; shift 2 ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    *) die "Unknown argument: $1 (see --help)" ;;
  esac
done
case "$STEP" in status|verify) [ "$APPLY" = 0 ] || die "--apply goes with tfvars | env | disable" ;; esac

require_env
for c in helm python3 terraform; do
  command -v "$c" >/dev/null 2>&1 || die "$c not found. Run this on the deployer EC2."
done

ROOT="$(cd "$LIB_DIR/../../.." && pwd)"
CHART="$ROOT/deployment/charts/llm-gateway"
V="${VALUES:-${HELM_VALUES_FILE:-$CHART/values-eks-fargate-$DEPLOY_ENV.yaml}}"
[ -f "$V" ] || die "values file not found: $V"
V=$(readlink -f "$V")
TF_DIR="${TF_DIR:-$ROOT/deployment/terraform/environments/llm-gateway-$DEPLOY_ENV}"
TFVARS="$TF_DIR/terraform.tfvars"
[ -d "$TF_DIR" ] || die "terraform env dir not found: $TF_DIR"
[ -f "$TFVARS" ] || die "terraform.tfvars not found: $TFVARS"
DEP_PROXY="${HELM_RELEASE}-gateway-proxy"
DEP_ADMIN="${HELM_RELEASE}-admin-api"

BODY_KEYS=(FIREHOSE_STREAM_NAME BODY_LOG_S3_BUCKET)

# ── Reads ───────────────────────────────────────────────────────────────────
# Effective var.enable_body_logging, evaluated the way apply would see it —
# terraform parses tfvars and every *.auto.tfvars, so a grep of terraform.tfvars
# alone can read a different truth than apply would.
tf_var_body_logging() {
  local out
  out=$(echo 'var.enable_body_logging' | terraform -chdir="$TF_DIR" console 2>/dev/null | tail -1)
  [ "$out" = true ] || [ "$out" = false ] || return 1
  printf '%s\n' "$out"
}

# Where the attribute is assigned (terraform.tfvars or a *.auto.tfvars).
tf_attr_files() {
  command grep -lE '^[[:space:]]*enable_body_logging[[:space:]]*=' \
    "$TFVARS" "$TF_DIR"/*.auto.tfvars 2>/dev/null || true
}

tf_output() { terraform -chdir="$TF_DIR" output -raw "$1" 2>/dev/null || true; }

# "KEY<TAB>value" for a deployment's container env, from the chart rendered
# with <values>. $2 = deployment name suffix (gateway-proxy | admin-api).
# Reading the render (not the file) is what makes inherited values from a
# parent values file visible — same rule as 15·17·19.
render_deploy_env() {
  local out
  out=$(helm template "$HELM_RELEASE" "$CHART" -f "$1" 2>&1) \
    || { printf 'helm template failed for %s:\n%s\n' "$1" "$(tail -5 <<<"$out")" >&2; return 1; }
  # ⚠️ heredoc(<<'PY') 으로 스크립트를 넘기면 안 된다 — <<<"$out" 가 stdin 을
  # 덮어 helm 출력을 스크립트로 실행해 SyntaxError 난다. -c 형태 유지.
  python3 -c '
import sys, yaml
suffix = sys.argv[1]
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get("kind") == "Deployment" and d["metadata"]["name"].endswith(suffix):
        for e in d["spec"]["template"]["spec"]["containers"][0].get("env") or []:
            if "value" in e:
                print(e["name"] + "\t" + str(e["value"]))
        break
' "$2" <<<"$out"
}

declare -A RV   # rendered: RV[FIREHOSE_STREAM_NAME]=...
# load_rendered <map> <values-file> [deploy-suffix] [key-prefix] — nameref 로
# 채운다. prefix 를 주면 키가 "<prefix>.KEY" 로 저장돼 여러 deployment 의 env 를
# 한 맵에 담을 수 있다 (apply_values_edit 의 섹션별 대조). 전역 RV 대신 지역
# 맵을 받을 수 있어야 NV 체크가 실제로 동작한다.
load_rendered() {
  local -n _m="$1"; shift
  local out k v suffix="${2:-gateway-proxy}" prefix="${3:-}"
  out=$(render_deploy_env "$1" "$suffix") || die "could not render env — see the message above"
  while IFS=$'\t' read -r k v; do [ -n "$k" ] && _m["${prefix:+$prefix.}$k"]="$v"; done <<<"$out"
}

# KEY=value lines of the given body-logging keys as a running pod has them —
# empty values included so a key that is present-but-empty stays visible.
# $1 = deployment name; $2 = egrep key pattern (default: both proxy keys).
pod_env() {
  local dep="$1" pat="${2:-^(FIREHOSE_STREAM_NAME|BODY_LOG_S3_BUCKET)=}"
  kubectl get deploy "$dep" -n "$NS" \
    -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}{"="}{.value}{"\n"}{end}' \
    2>/dev/null | grep -E "$pat" || true
}

# Both keys set to non-empty values in the rendered chart → the infra gate
# shows as open in the file/deploy inputs.
rendered_gate_open() {
  [ -n "${RV[FIREHOSE_STREAM_NAME]:-}" ] && [ -n "${RV[BODY_LOG_S3_BUCKET]:-}" ]
}

# ── Values edit (text edit, never a YAML round-trip — comments are docs) ─────
# <src> <dst> then quadruples <SECTION> <op> <KEY> <value>; SECTION is a
# top-level service key (gatewayProxy|adminApi), op is set|del, value is ""
# on del. Keys live at indent 4 under <SECTION>: -> env: (indent 2). 'del'
# touches only lines inside that env block — a same-named key in another
# section survives.
edit_values() {
  python3 - "$@" <<'PY'
import json, re, sys
src, dst, *ops = sys.argv[1:]
L = open(src, encoding="utf-8").read().split("\n")

def ind(l): return len(l) - len(l.lstrip(" "))
def code(l): return l.strip() != "" and not l.lstrip().startswith("#")

def env_block(sec):
    """<sec>.env 블록의 (env: 라인, 블록 끝) — 섹션/env 부재면 None. del op 는
    부재 섹션에서 no-op 이어야 disable 이 블록 없는 파일도 정리할 수 있다."""
    hits = [i for i, l in enumerate(L) if re.match(rf"^{sec}:\s*(#.*)?$", l)]
    if len(hits) > 1:
        sys.exit(f"values: top-level '{sec}:' found {len(hits)} times (need exactly 1)")
    if not hits:
        return None
    s = hits[0]
    e = next((i for i in range(s + 1, len(L)) if code(L[i]) and ind(L[i]) == 0), len(L))
    envs = [i for i in range(s + 1, e) if code(L[i]) and ind(L[i]) == 2 and re.match(r"^\s*env:", L[i])]
    if len(envs) > 1:
        sys.exit(f"values: '{sec}.env:' found {len(envs)} times (need exactly 1)")
    if not envs:
        return None  # env 블록 부재 — del 은 no-op, set 은 호출부가 에러 처리
    i = envs[0]
    if not re.match(r"^\s*env:\s*(#.*)?$", L[i]):
        # inline env({...}) 는 키가 들어있을 수 있어 del 도 억지로 넘기면 안 된다
        sys.exit(f"values: '{sec}.env:' is inline ({L[i].strip()}) — make it a block map first")
    j = i + 1
    while j < e and (not code(L[j]) or ind(L[j]) > 2):
        j += 1
    return i, j

for n in range(0, len(ops), 4):
    sec, op, key, val = ops[n:n + 4]
    if op not in ("set", "del"):
        sys.exit(f"edit_values: unknown op '{op}'")
    blk = env_block(sec)
    if blk is None:
        if op == "del":
            continue  # 섹션/env 블록이 없으면 지울 것도 없다 — no-op
        sys.exit(f"values: '{sec}:' section or env block not found — add the key by hand")
    i, j = blk
    hits = [k for k in range(i + 1, j) if code(L[k]) and ind(L[k]) == 4 and re.match(rf"^\s*{key}:", L[k])]
    if len(hits) > 1:
        sys.exit(f"values: {sec}.env.{key} appears {len(hits)} times — fix the file first")
    if hits:
        # 다음 코드 라인이 이 키보다 깊게 들어가 있으면 다중행 값 — 첫 줄만
        # 바꾸면 나머지가 고아가 되므로 수동 편집을 요구한다.
        nxt = hits[0] + 1
        while nxt < j and not code(L[nxt]):
            nxt += 1
        if nxt < j and ind(L[nxt]) > 4:
            sys.exit(f"values: {sec}.env.{key} is a multi-line value — edit by hand")
    if op == "del":
        if hits:
            del L[hits[0]]
        continue
    line = f"    {key}: {json.dumps(val)}"
    if hits:
        L[hits[0]] = line
        continue
    last = max([k for k in range(i + 1, j) if code(L[k]) and ind(L[k]) >= 4], default=i)
    L[last + 1:last + 1] = [line]

open(dst, "w", encoding="utf-8").write("\n".join(L))
PY
}

# Edit → render → compare with the wanted values → (apply). $1 = label,
# rest = quadruples <SECTION> <op> <KEY> <value> for edit_values. The render
# check compares what the chart would produce against the wanted strings —
# a key name alone proves nothing (the chart ships FIREHOSE_STREAM_NAME: ""
# by default).
apply_values_edit() {
  local label="$1"; shift
  local tmp; tmp=$(mktemp)
  edit_values "$V" "$tmp" "$@" || { rm -f "$tmp"; die "values edit failed — $V untouched"; }
  if cmp -s "$V" "$tmp"; then rm -f "$tmp"; ok "values already carries these — nothing to write"; return 1; fi

  declare -A NV=()
  # 관련 deployment 만 렌더 검증 — op 목록에서 섹션을 모아 suffix 로 변환한다.
  local n=0 sec op key val bad_n=0 want
  local -a args=("$@")
  local -A seen_secs=() sec2suffix=([gatewayProxy]=gateway-proxy [adminApi]=admin-api)
  for ((n = 0; n < ${#args[@]}; n += 4)); do
    sec=${args[n]}
    [ "${sec2suffix[$sec]:-}" ] || { rm -f "$tmp"; die "apply_values_edit: unknown section '$sec'"; }
    if [ -z "${seen_secs[$sec]:-}" ]; then
      seen_secs[$sec]=1
      load_rendered NV "$tmp" "${sec2suffix[$sec]}" "$sec" \
        || { rm -f "$tmp"; die "could not render $sec env — see above"; }
    fi
  done
  for ((n = 0; n < ${#args[@]}; n += 4)); do
    sec=${args[n]}; op=${args[n+1]}; key=${args[n+2]}; val=${args[n+3]}
    want="${NV[$sec.$key]:-}"
    if [ "$op" = del ]; then
      if [ -n "$want" ]; then
        bad "$sec.$key still renders '$want' — expected removed/empty"; bad_n=$((bad_n + 1))
      fi
    elif [ "$want" != "$val" ]; then
      bad "$sec.$key renders '${want:-(unset)}', expected '$val'"; bad_n=$((bad_n + 1))
    fi
  done
  [ "$bad_n" -eq 0 ] || { rm -f "$tmp"; die "edited values do not render the target — $V untouched"; }

  hdr "Planned change — $(basename "$V") (helm renders it)"
  diff -u "$V" "$tmp" | sed -n '3,$p' | sed 's/^/  /'
  if [ "$APPLY" = 0 ]; then
    rm -f "$tmp"
    printf '\n  Nothing written yet. Apply:  bash %s %s --apply\n\n' "$(basename "$0")" "$STEP"
    exit 0
  fi
  confirm "$label"
  local bak="$SNAP_DIR/${TS}-values-$DEPLOY_ENV-20-$STEP.bak"
  cp "$V" "$bak" || { rm -f "$tmp"; die "backup failed"; }
  cp "$tmp" "$V" || { rm -f "$tmp"; die "could not write $V (backup: $bak)"; }
  rm -f "$tmp"
  ok "values updated: $V"
  note "backup: $bak   (undo: cp $bak $V, then install-eks.sh $DEPLOY_ENV)"
  return 0
}

# ── Status (read-only) ─────────────────────────────────────────────────────
print_status() {
  hdr "Body-logging status (env=$DEPLOY_ENV)"
  local flag
  flag=$(tf_var_body_logging) || die "terraform console could not read var.enable_body_logging in $TF_DIR"
  if [ "$flag" = true ]; then ok "tfvars: enable_body_logging = true (effective value)"
  else warn "tfvars: enable_body_logging = false (default — no sink)"; fi

  STREAM="$(tf_output body_log_firehose_stream)"
  BUCKET="$(tf_output body_log_bucket)"
  [ -n "${STREAM:-}" ] && ok "terraform output stream: $STREAM" || warn "terraform output body_log_firehose_stream: none"
  [ -n "${BUCKET:-}" ] && ok "terraform output bucket: $BUCKET" || warn "terraform output body_log_bucket: none"

  PENV="$(pod_env "$DEP_PROXY")"
  if [ -n "$PENV" ]; then
    sed 's/^/       /' <<<"$PENV"
    if grep -q '^FIREHOSE_STREAM_NAME=.' <<<"$PENV" && grep -q '^BODY_LOG_S3_BUCKET=.' <<<"$PENV"; then
      ok "proxy pod env: both keys set — infra gate open"
    else
      warn "proxy pod env: key(s) present but empty — infra gate closed (a present-but-empty key is OFF)"
    fi
  else
    warn "proxy pod env: FIREHOSE_STREAM_NAME / BODY_LOG_S3_BUCKET not set (infra gate closed)"
  fi
  AENV="$(pod_env "$DEP_ADMIN" '^BODY_LOG_S3_BUCKET=')"
  if grep -q '^BODY_LOG_S3_BUCKET=.' <<<"${AENV:-}"; then
    ok "admin-api pod env: BODY_LOG_S3_BUCKET set — viewer/provisioned gate open"
  else
    warn "admin-api pod env: BODY_LOG_S3_BUCKET unset — admin UI shows provisioned=false (viewer locked)"
  fi

  load_rendered RV "$V"
  if rendered_gate_open; then
    ok "values render (proxy): FIREHOSE_STREAM_NAME=${RV[FIREHOSE_STREAM_NAME]} BODY_LOG_S3_BUCKET=${RV[BODY_LOG_S3_BUCKET]}"
  elif [ -n "${RV[FIREHOSE_STREAM_NAME]+x}" ] || [ -n "${RV[BODY_LOG_S3_BUCKET]+x}" ]; then
    warn "values render (proxy): key(s) render but empty — infra gate closed: $V"
  else
    warn "values render (proxy): no body-logging keys: $V"
  fi
  declare -A AV=()
  load_rendered AV "$V" admin-api
  if [ -n "${AV[BODY_LOG_S3_BUCKET]:-}" ]; then
    ok "values render (admin-api): BODY_LOG_S3_BUCKET=${AV[BODY_LOG_S3_BUCKET]}"
  else
    warn "values render (admin-api): BODY_LOG_S3_BUCKET empty/absent — viewer locked: $V"
  fi
  note "collection on/off itself is the admin /monitoring body logging toggle"
}

case "$STEP" in

status)
  print_status
  hdr "Next"
  cat <<EOT
  tfvars 미적용 → bash $(basename "$0") tfvars --apply   then terraform plan/apply ($TF_DIR)
  infra 있고 env 없음 → bash $(basename "$0") env --apply   then cd $ROOT && ./deployment/scripts/install-eks.sh $DEPLOY_ENV
  확인 → bash $(basename "$0") verify
EOT
  ;;

verify)
  print_status
  hdr "Verify"
  rc=0
  BUCKET="$(tf_output body_log_bucket)"
  if [ -n "${BUCKET:-}" ] && aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    ok "bucket $BUCKET reachable"
  else
    bad "bucket check failed: ${BUCKET:-<none>}"; rc=1
  fi
  STREAM="$(tf_output body_log_firehose_stream)"
  if [ -n "${STREAM:-}" ]; then
    st="$(aws firehose describe-delivery-stream --delivery-stream-name "$STREAM" \
        --query 'DeliveryStreamDescription.DeliveryStreamStatus' --output text 2>/dev/null || echo '?')"
    [ "$st" = "ACTIVE" ] && ok "stream $STREAM: ACTIVE" || { bad "stream $STREAM: $st"; rc=1; }
  else
    bad "stream: none"; rc=1
  fi
  PENV="$(pod_env "$DEP_PROXY")"
  if grep -q '^FIREHOSE_STREAM_NAME=.' <<<"$PENV" && grep -q '^BODY_LOG_S3_BUCKET=.' <<<"$PENV"; then
    ok "proxy pod env set — infra gate open"
  else
    bad "proxy pod env missing — infra gate closed"; rc=1
  fi
  # 뷰어 측 배선 — 없으면 수집은 되는데 admin UI가 provisioned=false로 잠겨
  # body-logs 페이지/토글이 사용불가. 수집 조건이 아니라 viewer 조건.
  AENV="$(pod_env "$DEP_ADMIN" '^BODY_LOG_S3_BUCKET=')"
  if grep -q '^BODY_LOG_S3_BUCKET=.' <<<"${AENV:-}"; then
    ok "admin-api pod env set — viewer/provisioned gate open"
  else
    bad "admin-api BODY_LOG_S3_BUCKET missing — collection works but the viewer stays locked"; rc=1
  fi
  echo
  if [ $rc -eq 0 ]; then
    ok "READY — turning on the admin toggle starts collection"
  else
    bad "NOT READY — fix the XX items above"
  fi
  exit $rc
  ;;

tfvars)
  flag=$(tf_var_body_logging) \
    || die "terraform console could not read var.enable_body_logging in $TF_DIR"
  if [ "$flag" = true ]; then
    ok "enable_body_logging = true is already the effective value — nothing to write"
    note "next: terraform plan → apply in $TF_DIR, then env step"
    exit 0
  fi
  declared=$(tf_attr_files)
  if [ -n "$declared" ]; then
    die "enable_body_logging is already assigned in:
$declared
     a second assignment is a terraform error. Flip that line to true by hand,
     then: terraform plan → apply, then: bash $(basename "$0") env"
  fi
  hdr "Planned change — append to $(basename "$TFVARS")"
  cat <<'EOT'
  + # 본문 로깅 sink — 20-enable-body-logging.sh tfvars (수집 on/off 는 admin 토글)
  + enable_body_logging = true
EOT
  if [ "$APPLY" = 0 ]; then
    printf '\n  Nothing written yet. Apply:  bash %s tfvars --apply\n\n' "$(basename "$0")"; exit 0
  fi
  confirm "Writing enable_body_logging to $TFVARS (backup kept in $SNAP_DIR)."
  bak="$SNAP_DIR/${TS}-tfvars-$DEPLOY_ENV-20-bodylog.bak"
  cp "$TFVARS" "$bak" || die "backup failed"
  [ -z "$(tail -c1 "$TFVARS")" ] || echo >> "$TFVARS"
  cat >> "$TFVARS" <<'EOF'

# 본문 로깅 sink — 20-enable-body-logging.sh tfvars (수집 on/off 는 admin 토글)
enable_body_logging = true
EOF
  got=$(tf_var_body_logging) || got=?
  if [ "$got" != true ]; then
    cp "$bak" "$TFVARS"
    die "terraform does not read the new flag back (console says: ${got:-nothing}) — tfvars restored from $bak
     (a *.auto.tfvars overriding it, or the state locked by another terraform run?)"
  fi
  ok "terraform.tfvars updated — terraform reads enable_body_logging = true"
  note "backup: $bak"
  hdr "Next — terraform (you run it, in $TF_DIR)"
  cat <<EOT
  terraform plan -target=module.body_logging -target=module.irsa
    expect: 9 add / 1 change (gateway-proxy IRSA policy in-place) / 0 destroy
    — anything else: stop (ops/8-V)
  terraform apply -target=module.body_logging -target=module.irsa
  then: bash $(basename "$0") env
EOT
  ;;

env)
  STREAM="$(tf_output body_log_firehose_stream)"
  BUCKET="$(tf_output body_log_bucket)"
  [ -n "${STREAM:-}" ] && [ -n "${BUCKET:-}" ] \
    || die "terraform outputs body_log_firehose_stream / body_log_bucket are empty —
     module.body_logging not applied yet? run: bash $(basename "$0") tfvars --apply, then terraform plan/apply"

  load_rendered RV "$V"
  declare -A AV=()
  load_rendered AV "$V" admin-api
  if [ "${RV[FIREHOSE_STREAM_NAME]:-}" = "$STREAM" ] && [ "${RV[BODY_LOG_S3_BUCKET]:-}" = "$BUCKET" ] \
     && [ "${AV[BODY_LOG_S3_BUCKET]:-}" = "$BUCKET" ]; then
    ok "values already renders the terraform outputs — nothing to write"
    note "next: cd $ROOT && ./deployment/scripts/install-eks.sh $DEPLOY_ENV"
    exit 0
  fi
  # adminApi.env.BODY_LOG_S3_BUCKET — 수집(proxy)과 무관하지만 admin API의
  # provisioned 신호 + body-logs 뷰어가 이 버킷을 본다. NDS 경로는 eks_apply가
  # 양쪽을 함께 넣어주고, 이 스크립트는 install-eks.sh 경로의 그 역할.
  apply_values_edit "Writing body-logging env under gatewayProxy.env + adminApi.env in $V (backup kept in $SNAP_DIR)." \
    gatewayProxy set FIREHOSE_STREAM_NAME "$STREAM" \
    gatewayProxy set BODY_LOG_S3_BUCKET "$BUCKET" \
    adminApi set BODY_LOG_S3_BUCKET "$BUCKET" || { echo; exit 0; }
  hdr "Next — deploy (you run it)"
  cat <<EOT
  cd $ROOT && ./deployment/scripts/install-eks.sh $DEPLOY_ENV
    (never a bare helm upgrade — install-eks.sh re-injects the terraform-output
     --set values; a plain -f upgrade would roll them back to placeholders)
  then: bash $(basename "$0") verify  →  admin /monitoring body logging toggle ON
EOT
  ;;

disable)
  load_rendered RV "$V"
  declare -A AV=()
  load_rendered AV "$V" admin-api
  # 렌더가 비어도 파일에 키 라인이 남아 있을 수 있다(KEY: "" 잔류) — 그 경우에도
  # 실제로 지워야 하므로 파일 자체에서 키 존재를 확인한다.
  if [ -z "${RV[FIREHOSE_STREAM_NAME]:-}" ] && [ -z "${RV[BODY_LOG_S3_BUCKET]:-}" ] \
     && [ -z "${AV[BODY_LOG_S3_BUCKET]:-}" ] \
     && ! grep -qE '^[[:space:]]+(FIREHOSE_STREAM_NAME|BODY_LOG_S3_BUCKET):' "$V"; then
    ok "values has no body-logging keys (or empty) — nothing to write"
    exit 0
  fi
  apply_values_edit "Removing body-logging env from gatewayProxy.env + adminApi.env in $V (backup kept in $SNAP_DIR)." \
    gatewayProxy del FIREHOSE_STREAM_NAME "" \
    gatewayProxy del BODY_LOG_S3_BUCKET "" \
    adminApi del BODY_LOG_S3_BUCKET "" || { echo; exit 0; }
  cat <<EOT

  Next: cd $ROOT && ./deployment/scripts/install-eks.sh $DEPLOY_ENV   — proxy env 제거로 수집 중지.
  S3 버킷/Firehose 는 남는다 — 버킷엔 프롬프트 본문이 있을 수 있어(force_destroy=false)
  객체 비우기 후 terraform 에서 수동 처리.
EOT
  ;;

esac
