# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""deploy upgrade eks — EKS 컨트롤플레인·매니지드 애드온의 홉 단위 업그레이드.

일반 apply 는 클러스터 버전을 건드리지 않는다(eks_cluster_version 은 managed
overlay 키가 아니다) — 버전 변경은 순서가 있는 절차형 작업이라 이 명령이 담당:

    라이브 → 목표 minor 까지 한 칸씩 반복 (다중 점프는 EKS 가 거부)
    홉마다 ① eks_cluster_version + eks_addon_versions 를 env tfvars 에 기록
          ② terraform apply -target=module.eks (스코프 밖 변경 없음)
          ③ cluster ACTIVE + version 일치 대기
          ④ Fargate 파드 전량 재생성 (kubelet 은 파드 재생성 시에만 갱신된다 —
              variables.tf 의 홉 규칙 ③)

버전 기록은 env terraform.tfvars(gitignore, env 소유 파일) — yaml 의
infra.eks_version 은 "기대 버전" 선언이고, 실제 적용 선언은 tfvars 다.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from .capture import _aws_json
from .eks_apply import _run, _run_quiet, _tf_init, _warn_stale_backend, tf_outputs
from .schema import GatewayConfig

# EKS 애드온명 → eks_addon_versions tfvar 키
_ADDONS = {"coredns": "coredns", "kube-proxy": "kube_proxy",
           "vpc-cni": "vpc_cni"}

_VER_RE = re.compile(r"^1\.(\d+)$")


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


def _addon_defaults(k8s_version: str, region: str) -> dict[str, str]:
    """목표 k8s 버전 호환 기본 애드온 버전 — describe-addon-versions 의
    defaultVersion=true 조합을 고른다."""
    out: dict[str, str] = {}
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
            if name not in _ADDONS or _ADDONS[name] in out:
                continue
            for av in addon.get("addonVersions") or []:
                comps = av.get("compatibilities") or []
                if any(c.get("clusterVersion") == k8s_version
                       and c.get("defaultVersion") for c in comps):
                    out[_ADDONS[name]] = str(av.get("addonVersion") or "")
                    break
        token = r.get("nextToken") or ""
        if not token or len(out) == len(_ADDONS):
            break
    missing = sorted(set(_ADDONS.values()) - set(out))
    if missing:
        # 기본값을 못 찾은 애드온을 비운 채 tfvars 를 쓰면 variables.tf 의
        # 낡은 핀이 그대로 적용돼 버전 skew 가 생긴다 — fail-closed.
        raise UpgradeError(
            f"{k8s_version} 호환 애드온 기본 버전 조회 실패: "
            f"{', '.join(missing)} — describe-addon-versions 결과를 확인하세요")
    return out


def _read_tfvars(env_dir: Path) -> str:
    p = env_dir / "terraform.tfvars"
    return p.read_text() if p.exists() else ""


def _write_version_tfvars(env_dir: Path, version: str,
                          addons: dict[str, str]) -> None:
    """env terraform.tfvars 의 버전 선언을 갱신 — 기존 할당을 지우고 끝에 쓴다.

    버전 기록은 tfvars(variables.tf 기본값이 아닌 env 소유 선언)에 둔다 —
    홉 중간 실패해도 tfvars 가 마지막 시도 버전을 남기고, 재실행은 라이브
    조회로 재개된다."""
    text = _read_tfvars(env_dir)
    text = re.sub(r"(?m)^eks_cluster_version\s*=.*\n", "", text)
    text = re.sub(r"(?ms)^eks_addon_versions\s*=\s*\{.*?^\}", "", text)
    block = f'eks_cluster_version = "{version}"\n'
    if addons:
        block += "eks_addon_versions = {\n" + "".join(
            f"  {k} = \"{addons[k]}\"\n"
            for k in ("coredns", "kube_proxy", "vpc_cni") if addons.get(k)
        ) + "}\n"
    (env_dir / "terraform.tfvars").write_text(
        text.rstrip() + "\n\n" + block if text.strip() else block)


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


def _recreate_pods(context: str) -> None:
    """Fargate kubelet 갱신 = 파드 재생성. 클러스터 전체 deployment 재시작 —
    coredns/ESO/ALB controller 도 Fargate 라 릴리스 외도 포함해야 skew 가
    풀린다."""
    ctx = ["--kube-context", context] if context else []
    _run(["kubectl", "rollout", "restart", "deployment", "--all-namespaces",
          *ctx])
    _run(["kubectl", "wait", "--for=condition=available", "deployments",
          "--all", "--all-namespaces", "--timeout=10m", *ctx])


def cmd_upgrade_eks(cfg: GatewayConfig, env_dir: Path, gen_dir: Path,
                    cfg_path: Path, *, to: str, yes: bool, plan: bool,
                    context: str = "") -> int:
    target = to or cfg.infra.eks_version or ""
    if not target:
        raise SystemExit("목표 버전이 없습니다 — --to 1.<minor> 또는 gateway.yaml "
                         "infra.eks_version 을 선언하세요")
    tgt_minor = _minor(target)
    init = _tf_init(cfg, env_dir)
    if init.returncode != 0:
        _warn_stale_backend(cfg, env_dir)
        raise SystemExit(f"terraform init 실패(rc={init.returncode})")
    cluster = str(tf_outputs(env_dir).get("cluster_name") or "")
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
    if tgt_minor == live_minor:
        print(f"이미 {live_v} — 업그레이드 불필요")
        return 0
    hops = [f"1.{m}" for m in _hops(live_minor, tgt_minor)]
    print(f"업그레이드 계획: {live_v} → {' → '.join(hops)} "
          f"(클러스터 {cluster}, {len(hops)} 홉)")
    print("  각 홉: tfvars 기록 → terraform apply -target=module.eks → "
          "ACTIVE 대기 → 전체 deployment 재생성(Fargate kubelet 갱신)")
    # 홉별 애드온 기본값 미리 계산해 계획에 표시
    addon_plan = {v: _addon_defaults(v, cfg.aws.region) for v in hops}
    for v in hops:
        print(f"  {v}: "
              + (", ".join(f"{k}={a}" for k, a in addon_plan[v].items())
                 or "애드온 기본값 조회 실패/없음"))
    if plan:
        print("plan 전용 — 변경 없음")
        return 0
    if not yes:
        ans = input("위 절차로 진행합니다 — 파드 재생성(짧은 재기동)이 홉마다 "
                    "발생합니다. 계속? [y/N] ")
        if ans.strip().lower() != "y":
            print("중단")
            return 1

    managed = gen_dir / "terraform.tfvars"
    var_args = [f"-var-file={managed}"] if managed.exists() else []
    for v in hops:
        print(f"=== 홉 {v} ===", file=sys.stderr)
        _write_version_tfvars(env_dir, v, addon_plan[v])
        try:
            _run(["terraform", "apply", "-auto-approve", "-input=false",
                  "-target=module.eks", *var_args], cwd=env_dir)
        except SystemExit:
            raise SystemExit(
                f"홉 {v} terraform apply 실패 — terraform.tfvars 는 {v} 를 "
                "기록 중입니다. 원인 해결 후 재실행하면 라이브 버전 기준으로 "
                "재개됩니다")
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

