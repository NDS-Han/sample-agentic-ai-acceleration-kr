# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""deploy upgrade eks — EKS 컨트롤플레인·매니지드 애드온의 홉 단위 업그레이드.

일반 apply 는 클러스터 버전을 건드리지 않는다(eks_cluster_version 은 managed
overlay 키가 아니다) — 버전 변경은 순서가 있는 절차형 작업이라 이 명령이 담당:

    라이브 → 목표 minor 까지 한 칸씩 반복 (다중 점프는 EKS 가 거부)
    홉마다 ① eks_cluster_version + eks_addon_versions 를 env tfvars 에 기록
          ② terraform plan -out → 허용 범위 검사(module.eks 안 + 파괴 변경 없음
            + 클러스터/애드온 버전 변경이 실제 존재) → 확인 → apply
          ③ cluster ACTIVE + version 일치 대기
          ④ deployment/statefulset 전량 재생성 + rollout status 대기
             (kubelet 은 파드 재생성 시에만 갱신된다 — variables.tf 의 홉 규칙)

버전 기록은 env terraform.tfvars(gitignore, env 소유 파일) — yaml 의
infra.eks_version 은 "기대 버전" 선언이고, 실제 적용 선언은 tfvars 다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from .capture import _aws_json
from .eks_apply import (_check_env_dir_declared, _kube_context_cluster,
                        _run, _run_quiet, _tf_init, _warn_stale_backend,
                        tf_outputs)
from .schema import GatewayConfig

# EKS 애드온명 → eks_addon_versions tfvar 키
_ADDONS = {"coredns": "coredns", "kube-proxy": "kube_proxy",
           "vpc-cni": "vpc_cni"}

_VER_RE = re.compile(r"^1\.(\d+)$")
_ADDON_VER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-eksbuild\.(\d+)$")


class UpgradeError(Exception):
    """사용자에게 보여줄 업그레이드 중단 사유."""


def _minor(v) -> int:
    # 비문자열 거부 — float 1.30 을 str() 하면 "1.3" 으로 minor 가 왜곡된다.
    if not isinstance(v, str) or not (m := _VER_RE.fullmatch(v.strip())):
        raise UpgradeError(f"버전 형식이 '1.<minor>' 가 아닙니다: {v!r}")
    return int(m.group(1))


def _hops(live_minor: int, tgt_minor: int) -> list[int]:
    """라이브 → 목표의 홉 minor 목록 (한 칸씩). 다중 점프는 EKS 가 거부하고
    다운그레이드는 절차 자체가 다르므로 여기서 차단."""
    if tgt_minor == live_minor:
        raise UpgradeError("목표 버전이 라이브와 같습니다 — 업그레이드 불필요")
    if tgt_minor < live_minor:
        raise UpgradeError(
            "다운그레이드는 지원하지 않습니다 — EKS minor 롤백은 7일 이내 "
            "직전 minor 로만 가능하고 계획된 다운타임이 필요합니다")
    return list(range(live_minor + 1, tgt_minor + 1))


def _describe(cluster: str, region: str) -> dict:
    try:
        return (_aws_json(["eks", "describe-cluster", "--name", cluster],
                          region).get("cluster") or {})
    except Exception as e:
        raise UpgradeError(f"describe-cluster 실패: {e}")


def _addon_key(v: str) -> tuple[int, int, int, int] | None:
    """v1.22.4-eksbuild.14 → 비교 가능 튜플. 파싱 실패 시 None."""
    m = _ADDON_VER_RE.fullmatch(v.strip())
    return tuple(int(g) for g in m.groups()) if m else None


def _addon_current(cluster: str, region: str) -> dict[str, str]:
    """describe-addon 으로 현재 설치 버전 — 조회 실패는 전체 중단이 아니라
    그 애드온만 빈 값(기본값으로 수렴)으로 둔다."""
    out: dict[str, str] = {}
    for aws_name, key in _ADDONS.items():
        try:
            ver = str((_aws_json(["eks", "describe-addon",
                                  "--cluster-name", cluster,
                                  "--addon-name", aws_name], region)
                       .get("addon") or {}).get("addonVersion") or "")
        except Exception:
            ver = ""
        if ver:
            out[key] = ver
    return out


def _addon_defaults(k8s_version: str, region: str,
                    current: dict[str, str] | None = None) -> dict[str, str]:
    """목표 k8s 버전 호환 애드온 버전 결정.

    기본은 describe-addon-versions 의 defaultVersion 조합. 단 현재 설치
    버전이 목표 k8s 와 호환으로 명시돼 있으면 그대로 유지하고, 기본값이
    현재보다 낮은데 현재도 비호환이면 안전하게 고를 수 없어 중단한다 —
    애드온 다운그레이드 시도를 자동으로 하지 않는다."""
    defaults: dict[str, str] = {}
    compatible: dict[str, set[str]] = {k: set() for k in _ADDONS.values()}
    token = ""
    while True:
        argv = ["eks", "describe-addon-versions",
                "--kubernetes-version", k8s_version]
        if token:
            argv += ["--starting-token", token]
        try:
            r = _aws_json(argv, region)
        except Exception as e:
            raise UpgradeError(f"describe-addon-versions 실패: {e}")
        for addon in r.get("addons") or []:
            name = addon.get("addonName")
            if name not in _ADDONS:
                continue
            key = _ADDONS[name]
            for av in addon.get("addonVersions") or []:
                comps = av.get("compatibilities") or []
                hit = [c for c in comps
                       if c.get("clusterVersion") == k8s_version]
                if not hit:
                    continue
                ver = str(av.get("addonVersion") or "")
                compatible[key].add(ver)
                if key not in defaults and any(
                        c.get("defaultVersion") for c in hit):
                    defaults[key] = ver
        token = r.get("nextToken") or ""
        if not token or len(defaults) == len(_ADDONS):
            break
    missing = sorted(set(_ADDONS.values()) - set(defaults))
    if missing:
        # 기본값을 못 찾은 애드온을 비운 채 tfvars 를 쓰면 variables.tf 의
        # 낡은 핀이 그대로 적용돼 버전 skew 가 생긴다 — fail-closed.
        raise UpgradeError(
            f"{k8s_version} 호환 애드온 기본 버전 조회 실패: "
            f"{', '.join(missing)} — describe-addon-versions 결과를 확인하세요")
    out: dict[str, str] = {}
    for key, default in defaults.items():
        cur = (current or {}).get(key, "")
        if not cur or cur in compatible[key]:
            # 미설치/조회실패 → 기본값, 현재가 이미 호환 → 유지 (불필요한
            # 애드온 롤링 방지)
            out[key] = cur if cur in compatible[key] else default
            continue
        dk, ck = _addon_key(default), _addon_key(cur)
        if dk and ck and dk >= ck:
            out[key] = default  # 기본값이 더 새 버전 = 호환 방향으로 전진
            continue
        raise UpgradeError(
            f"{key}: 현재 {cur} 는 k8s {k8s_version} 호환 목록에 없고 기본값 "
            f"{default} 보다 높습니다 — 애드온 다운그레이드 여부를 수동으로 "
            "판단해 eks_addon_versions 를 직접 선언하세요")
    return out


def _read_tfvars(env_dir: Path) -> str:
    p = env_dir / "terraform.tfvars"
    return p.read_text() if p.exists() else ""


def _strip_map_key(text: str, key: str) -> str:
    """HCL map 할당 `key = { … }` 한 블록을 브레이스 균형으로 삭제 —
    한 줄/여러 줄 형식과 블록 안 중첩 맵을 모두 견딘다(정규식 `.*?^}` 는
    한 줄 맵이나 후속 블록에서 깨진다)."""
    m = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*\{{", text)
    if not m:
        return text
    start = text.rfind("\n", 0, m.start()) + 1
    depth, i = 0, text.index("{", m.start())
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                if end < len(text) and text[end] == "\n":
                    end += 1
                return text[:start] + text[end:]
        i += 1
    raise UpgradeError(f"terraform.tfvars 의 {key} 블록이 닫히지 않습니다")


def _write_version_tfvars(env_dir: Path, version: str,
                          addons: dict[str, str]) -> None:
    """env terraform.tfvars 의 버전 선언을 갱신 — 기존 할당을 지우고 끝에 쓴다.

    - 파일이 없으면 중단: tfvars 는 access_entries/vpc 등 사람 소유 값을
      담으므로, 부재 상태에서 버전 키만 든 파일을 만들면 그 값들이 기본값
      으로 초기화된다 (신규 머신 함정).
    - .bak 백업 + 임시파일→os.replace 원자 쓰기: gitignore 되어 백업이
      없는 파일이라 중간 실패로 잘리면 복구 불가다."""
    p = env_dir / "terraform.tfvars"
    if not p.exists():
        raise UpgradeError(
            f"{p} 가 없습니다 — access_entries 등 env 선언이 들어있는 파일인데 "
            "부재 상태로 버전 키만 쓰면 나머지가 기본값으로 초기화됩니다. "
            "이 env 의 terraform.tfvars 를 먼저 복원하세요")
    text = p.read_text()
    # 스칼라: 들여쓰기·말줄 개행 유무 모두 흡수
    text = re.sub(r"(?m)^\s*eks_cluster_version\s*=.*\n?", "", text)
    text = _strip_map_key(text, "eks_addon_versions")
    block = f'eks_cluster_version = "{version}"\n'
    if addons:
        block += ("# deploy upgrade eks 가 describe-addon-versions "
                  "실측으로 기록 — 홉마다 갱신됨\n"
                  "eks_addon_versions = {\n" + "".join(
                      f'  {k} = "{addons[k]}"\n'
                      for k in ("coredns", "kube_proxy", "vpc_cni")
                      if addons.get(k)) + "}\n")
    body = (text.rstrip() + "\n\n" + block) if text.strip() else block
    shutil.copy2(p, p.with_suffix(".tfvars.bak"))
    fd, tmp = tempfile.mkstemp(dir=env_dir, prefix=".tfvars-", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _inspect_upgrade_plan(env_dir: Path, plan_path: Path,
                          want_version: str) -> tuple[list[str], list[str], bool]:
    """저장된 plan 을 terraform show -json 으로 검사.

    반환 (bad, changes, has_version_bump):
    - bad: module.eks 범위 밖 변경, destroy/replace/forget/moved·import —
      발견 시 절대 apply 하지 않는다 (managed apply 의 _inspect_managed_plan
      과 같은 폭발 반경 원칙).
    - has_version_bump: aws_eks_cluster version 변경 포함 여부 — false 면
      다른 tfvars(auto.tfvars/-var-file)가 버전 키를 덮어쓴 것이므로
      apply 해도 "No changes" 가 되고 wait 만 50분 허비한다."""
    r = _run_quiet(["terraform", "show", "-json", str(plan_path)],
                   cwd=env_dir, timeout=120)
    if r.returncode != 0:
        return ([f"terraform show -json 실패(rc={r.returncode}) — plan 검증 "
                 "불가"], [], False)
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return (["terraform show -json 출력 파싱 실패 — plan 검증 불가"], [],
                False)
    bad: list[str] = []
    changes: list[str] = []
    bump = False
    for rc_ in data.get("resource_changes") or []:
        if rc_.get("mode") != "managed":
            continue
        addr = str(rc_.get("address", ""))
        change = rc_.get("change") or {}
        acts = change.get("actions") or []
        if rc_.get("previous_address") or change.get("importing"):
            bad.append(f"{addr} — moved/import 변경은 검증 불가")
            continue
        if not acts or acts == ["no-op"] or acts == ["read"]:
            continue
        in_scope = addr == "module.eks" or addr.startswith("module.eks.")
        if not in_scope:
            bad.append(f"{addr} — target 모듈(module.eks) 밖")
            continue
        if any(a in ("delete", "replace", "forget") for a in acts):
            # 업그레이드 중 fargate profile/access entry 의 재생성·삭제는
            # 클러스터 장애로 직결 — 절대 자동 승인하지 않는다.
            bad.append(f"{addr} — 파괴적 변경({'+'.join(acts)}) — "
                       "terraform 단독 검토 필요")
            continue
        before = change.get("before") or {}
        after = change.get("after") or {}
        changes.append(addr)
        if "aws_eks_cluster.this" in addr:
            if str(after.get("version") or "") == want_version \
                    and str(before.get("version") or "") != want_version:
                bump = True
    return bad, changes, bump


def _wait_version(cluster: str, want: str, region: str,
                  timeout_s: int = 3000) -> None:
    """컨트롤플레인 업그레이드는 홉당 10-20분 — ACTIVE+버전 일치까지 폴링.
    describe 의 일시 실패(스로틀링/네트워크)는 재시도한다."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            c = _describe(cluster, region)
        except UpgradeError as e:
            print(f"  … describe-cluster 일시 실패 — {e}", file=sys.stderr)
            time.sleep(30)
            continue
        v, st = str(c.get("version") or ""), str(c.get("status") or "")
        print(f"  … {v} / {st}", file=sys.stderr)
        if v == want and st == "ACTIVE":
            return
        if st in ("FAILED", "DEGRADED"):
            raise UpgradeError(f"클러스터 상태 {st} — 콘솔에서 업그레이드 실패 "
                               "원인을 확인하세요")
        time.sleep(30)
    raise UpgradeError(f"{timeout_s // 60}분 대기 후에도 {want} ACTIVE 미달성")


def _insight_blockers(cluster: str, region: str) -> list[str]:
    """EKS upgrade insights 의 ERROR 항목 — list-insights 미지원/실패는
    보조 검사라 차단하지 않고 경고만."""
    try:
        r = _aws_json(["eks", "list-insights", "--cluster-name", cluster],
                      region)
    except Exception:
        return []
    out = []
    for ins in r.get("insights") or []:
        st = str((ins.get("insightStatus") or {}).get("status") or "")
        if st == "ERROR":
            out.append(f"{ins.get('name', '?')}: "
                       f"{(ins.get('insightStatus') or {}).get('reason', '')}")
    return out


def _verify_context(context: str, cluster: str) -> None:
    """kubectl 이 맞는 클러스터를 가리키는지 — 미지정 current-context 도
    검사한다 (--context 생략하면 아무 검증 없이 다른 클러스터를 재시작할
    수 있다)."""
    got = _kube_context_cluster(context)
    if not got:
        print("⚠ kubectl context 의 클러스터를 판독하지 못했습니다 — "
              "파드 재생성 대상이 맞는지 직접 확인하세요", file=sys.stderr)
        return
    if got != cluster:
        raise UpgradeError(
            f"kubectl context 는 '{got}' 를 가리키는데 업그레이드 대상은 "
            f"'{cluster}' 입니다 — 다른 클러스터의 워크로드를 재시작하는 것을 "
            "차단했습니다. --context 로 올바른 context 를 지정하세요")


def _recreate_pods(context: str) -> None:
    """Fargate kubelet 갱신 = 파드 재생성. deployment+statefulset 전량 —
    coredns/ESO/ALB controller 도 Fargate 라 릴리스 외도 포함해야 skew 가
    풀린다. rollout status 를 워크로드별로 기다려 restart 가 실제로 끝난
    뒤에만 다음 홉으로 간다(Available 조건은 구 파드로 True 라 못 믿는다)."""
    ctx = ["--context", context] if context else []
    items = _run_quiet(
        ["kubectl", "get", "deploy,sts", "-A", "-o",
         "jsonpath={range .items[*]}{.kind}{\"/\"}{.metadata.name}{\" \"}"
         "{.metadata.namespace}{\"\\n\"}{end}", *ctx], timeout=30)
    workloads = []
    if items.returncode == 0:
        for line in items.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                workloads.append(tuple(parts))  # (kind/name, ns)
    if not workloads:
        # 목록 조회 실패 시에도 최소한 전체 재시작은 시도 — 대기 없이는
        # skew 검증이 안 되므로 조회 실패 자체를 표면화한다.
        print("⚠ 워크로드 목록 조회 실패 — 재시작만 하고 개별 대기는 건너뜁니다",
              file=sys.stderr)
        _run(["kubectl", "rollout", "restart", "deployment", "--all-namespaces",
              *ctx])
        return
    # kube-system(coredns) → 나머지 순으로 대기 — DNS 가 먼저 안정돼야
    # 앱 파드 기동이 붙는다.
    workloads.sort(key=lambda w: 0 if w[1] == "kube-system" else 1)
    for kind_name, ns in workloads:
        _run_quiet(["kubectl", "rollout", "restart", kind_name, "-n", ns, *ctx],
                   timeout=60)
    for kind_name, ns in workloads:
        print(f"  … {ns}/{kind_name}", file=sys.stderr)
        r = _run_quiet(["kubectl", "rollout", "status", kind_name, "-n", ns,
                        "--timeout=10m", *ctx], timeout=640)
        if r.returncode != 0:
            raise UpgradeError(
                f"rollout 대기 실패: {ns}/{kind_name} — "
                f"{(r.stderr or r.stdout or '').strip()[:200]}. "
                "파드 재생성이 끝나지 않았습니다 — 해결 후 재실행하면 "
                "남은 단계를 이어서 진행합니다")


def _addon_matches(cluster: str, region: str, want: dict[str, str]) -> bool:
    """현재 애드온 버전이 기대값과 같은지 — 홉 재개 시 apply 생략 판단."""
    cur = _addon_current(cluster, region)
    return all(cur.get(k) == v for k, v in want.items())


def cmd_upgrade_eks(cfg: GatewayConfig, env_dir: Path, gen_dir: Path,
                    cfg_path: Path, *, to: str, yes: bool, plan: bool,
                    context: str = "") -> int:
    _check_env_dir_declared(cfg, env_dir, hard=True)
    target = to or cfg.infra.eks_version or ""
    if not target:
        raise SystemExit("목표 버전이 없습니다 — --to 1.<minor> 또는 gateway.yaml "
                         "infra.eks_version 을 선언하세요")
    tgt_minor = _minor(target)
    init = _tf_init(cfg, env_dir)
    if init.returncode != 0:
        _warn_stale_backend(cfg, env_dir)
        raise SystemExit(f"terraform init 실패(rc={init.returncode})")
    outs = tf_outputs(env_dir)
    cluster = str(outs.get("cluster_name") or "")
    if not cluster:
        raise SystemExit("terraform output cluster_name 이 비어 있습니다 — "
                         "env_dir/state 를 확인하세요")
    live = _describe(cluster, cfg.aws.region)
    live_v, st = str(live.get("version") or ""), str(live.get("status") or "")
    if not live_v:
        raise UpgradeError("describe-cluster 응답에 version 이 없습니다")
    live_minor = _minor(live_v)
    if st != "ACTIVE":
        raise UpgradeError(f"클러스터 상태가 {st} 입니다 — ACTIVE 일 때만 "
                           "업그레이드할 수 있습니다")
    # insights 사전 점검 — ERROR insight 는 UpdateClusterVersion 을 차단한다.
    blockers = _insight_blockers(cluster, cfg.aws.region)
    if blockers:
        raise UpgradeError(
            "EKS upgrade insight 에 ERROR 가 있습니다 — 해결 후 재시도:\n  "
            + "\n  ".join(blockers[:5]))

    addon_current = _addon_current(cluster, cfg.aws.region)
    if tgt_minor <= live_minor:
        # 라이브가 이미 목표 — 그래도 애드온 drift/미완료 홉이 있을 수 있어
        # 남은 수렴을 확인한다(중간 홉 실패 재개 경로).
        if tgt_minor < live_minor:
            _hops(live_minor, tgt_minor)  # 다운그레이드 거부 메시지 재사용
        want = _addon_defaults(target, cfg.aws.region, addon_current)
        if _addon_matches(cluster, cfg.aws.region, want):
            print(f"이미 {live_v} + 애드온 정합 — 업그레이드 불필요")
            return 0
        hops = [target]
        addon_plan = {target: want}
        print(f"클러스터는 이미 {live_v} 이지만 애드온이 기대값과 다릅니다 — "
              "남은 수렴(애드온/파드 재생성)을 진행합니다")
    else:
        hops = [f"1.{m}" for m in _hops(live_minor, tgt_minor)]
        addon_plan = {v: _addon_defaults(v, cfg.aws.region, addon_current)
                      for v in hops}
    print(f"업그레이드 계획: {live_v} → {' → '.join(hops)} "
          f"(클러스터 {cluster}, {len(hops)} 홉)")
    print("  각 홉: tfvars 기록 → plan 범위 검사 → terraform apply "
          "-target=module.eks → ACTIVE 대기 → 워크로드 재생성(kubelet 갱신)")
    for v in hops:
        print(f"  {v}: "
              + ", ".join(f"{k}={a}" for k, a in addon_plan[v].items()))
    if plan:
        print("plan 전용 — 변경 없음")
        return 0
    # 컨텍스트 검증은 확인 전에 — 승인한 뒤에 다른 클러스터를 재시작하는 걸
    # 미리 차단한다.
    _verify_context(context, cluster)
    if not yes:
        ans = input("위 절차로 진행합니다 — 홉마다 컨트롤플레인 업그레이드"
                    "(10~20분)와 전체 워크로드 재생성이 발생합니다. "
                    "계속? [y/N] ")
        if ans.strip().lower() != "y":
            print("중단")
            return 1

    managed = gen_dir / "terraform.tfvars"
    var_args = [f"-var-file={managed}"] if managed.exists() else []
    for v in hops:
        print(f"=== 홉 {v} ===", file=sys.stderr)
        _write_version_tfvars(env_dir, v, addon_plan[v])
        plan_file = env_dir / f".upgrade-{v}.tfplan"
        p = _run_quiet(["terraform", "plan", "-out", str(plan_file),
                        "-target=module.eks", "-input=false", *var_args],
                       cwd=env_dir, timeout=600)
        if p.returncode != 0:
            raise SystemExit(
                f"홉 {v} terraform plan 실패(rc={p.returncode}) — "
                f"{(p.stderr or '').strip()[:300]}")
        bad, changes, bump = _inspect_upgrade_plan(env_dir, plan_file, v)
        for c in changes:
            print(f"  plan: {c}")
        if bad:
            raise UpgradeError(
                "plan 범위 검사 실패 — 다음 변경은 업그레이드가 승인할 수 "
                "없습니다:\n  " + "\n  ".join(bad[:8]) +
                "\n해당 drift 를 먼저 해소한 뒤 재시도하세요")
        cur = _describe(cluster, cfg.aws.region)
        already = str(cur.get("version") or "") == v
        if not changes and not already:
            # No changes 인데 버전도 안 올라가 있으면 다른 tfvars 가 버전
            # 키를 덮어쓴 것 — apply 해도 wait 만 허비한다.
            raise UpgradeError(
                f"plan 이 비어 있는데 클러스터는 아직 {v} 가 아닙니다 — "
                "*.auto.tfvars/-var-file 이 eks_cluster_version 을 덮어쓰는지 "
                "확인하세요")
        if changes:
            try:
                _run(["terraform", "apply", "-input=false", str(plan_file)],
                     cwd=env_dir)
            except SystemExit:
                raise SystemExit(
                    f"홉 {v} terraform apply 실패 — terraform.tfvars 는 {v} 를 "
                    "기록 중입니다. 원인 해결 후 재실행하면 라이브 버전 기준으로 "
                    "재개됩니다")
        plan_file.unlink(missing_ok=True)
        _wait_version(cluster, v, cfg.aws.region)
        _recreate_pods(context)
        print(f"홉 {v} 완료", file=sys.stderr)

    print(f"업그레이드 완료: {live_v} → {hops[-1]}")
    # 선언 파일의 기대 버전 갱신 — yaml 이 라이브를 반영하게
    _absorb_eks_version(cfg_path, hops[-1])
    return 0


def _absorb_eks_version(cfg_path: Path, version: str) -> None:
    """완료된 버전을 gateway.yaml 의 infra.eks_version 에 기록한다 —
    선언(기대) = 라이브가 되도록. 파일이 없거나 쓰기 실패면 안내만."""
    try:
        from .cli import _atomic_write, _annotate_capture_yaml, _set_path, _gd
        import yaml as _yaml
        if not cfg_path.exists():
            return
        doc = _yaml.safe_load(cfg_path.read_text()) or {}
        if _gd(doc, "infra").get("eks_version") == version:
            return
        _set_path(doc, "infra.eks_version", version)
        body = _yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
        _atomic_write(cfg_path, _annotate_capture_yaml(body, doc))
        cfg_path.chmod(0o600)
        print(f"gateway.yaml 의 infra.eks_version 을 {version} 으로 갱신했습니다 "
              "(수작업 주석은 소실됩니다)")
    except Exception as e:
        print(f"ⓘ gateway.yaml 갱신 건너뜀({e}) — infra.eks_version 을 "
              f"{version} 으로 직접 기록하세요", file=sys.stderr)
