# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""eks backend apply — terraform output → helm upgrade --install.

install-eks.sh 와 같은 계약:
  values 레이어 = chart 기본 → env overlay → gen/<env>/eks/values.yaml(최종)
  --set 동적값 = terraform output(ecr/aurora/redis/IRSA/cognito/body-log)
  migration    = chart 의 pre-upgrade hook Job — 자동 실행, 실패 시 upgrade 실패
  rollback     = helm4 --rollback-on-failure / helm3 --atomic + --cleanup-on-fail

2-phase URL: domain=none 이면 첫 upgrade 후 live ingress 의 ALB hostname 을 읽어
adminUi.nextauthUrl 을 --set 으로 한 번 더 upgrade (install-eks.sh 의
phase-2 와 동일 — --set 은 릴리스 values 에 누적되지 않으므로 매번 주입).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from .schema import GatewayConfig


def _run(argv: list[str], *, cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(str(a) for a in argv)}", file=sys.stderr)
    try:
        r = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=capture, text=True)
    except FileNotFoundError:
        raise SystemExit(f"{argv[0]} 명령을 못 찾았습니다 — helm/kubectl/terraform 설치와 PATH 확인 필요")
    if r.returncode != 0:
        if capture:
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
        raise SystemExit(f"명령 실패 ({r.returncode}): {' '.join(str(a) for a in argv)}")
    return r


def load_meta(gen_dir: Path, repo_root: Path | None = None) -> dict:
    p = gen_dir / "deploy.yaml"
    if not p.exists():
        raise SystemExit(f"{p} 없음 — 먼저 `deploy render` 를 실행하십시오")
    meta = yaml.safe_load(p.read_text()) or {}
    missing = [k for k in ("release", "namespace", "chart") if not meta.get(k)]
    if missing:
        raise SystemExit(f"{p} 가 불완전합니다({', '.join(missing)} 없음) — `deploy render` 로 재생성하십시오")
    # values 레이어 파일이 실제 있는지 — 없으면 helm 이 apply 중간에 죽는다
    for layer in meta.get("values_layers", []):
        lp = Path(layer)
        if not lp.is_absolute() and repo_root:
            lp = repo_root / lp
        if not lp.exists():
            raise SystemExit(f"values layer 없음: {layer} — `deploy render` 로 재생성하십시오")
    return meta


def _run_quiet(argv: list[str], *, cwd: Path | None = None,
               timeout: int = 60) -> subprocess.CompletedProcess:
    """best-effort 조회용 — 바이너리 부재/타임아웃을 traceback 대신 rc≠0 으로."""
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "",
                                           f"{argv[0]} 명령을 못 찾았습니다")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "",
                                           f"{argv[0]} 응답 없음({timeout}s)")


def tf_outputs(env_dir: Path) -> dict:
    r = _run_quiet(["terraform", "output", "-json"], cwd=env_dir, timeout=120)
    if r.returncode != 0:
        return {}
    try:
        return {k: v.get("value") for k, v in json.loads(r.stdout or "{}").items()}
    except json.JSONDecodeError:
        return {}


def _aws_account_id(region: str) -> str:
    r = _run_quiet(["aws", "sts", "get-caller-identity", "--query", "Account",
                    "--output", "text", "--region", region], timeout=30)
    return r.stdout.strip() if r.returncode == 0 else ""


def dynamic_set_args(cfg: GatewayConfig, outs: dict, region: str) -> list[str]:
    """install-eks.sh 의 SET_ARGS 와 동일한 --set 목록 (terraform output 기반)."""
    args: list[str] = []
    if not cfg.images.registry:
        account = _aws_account_id(region)
        if account:
            args += ["--set", f"global.imageRegistry={account}.dkr.ecr.{region}.amazonaws.com"]
        else:
            print("⚠ aws sts 조회 실패 — images.registry 미지정인데 ECR 자동 추론을 "
                  "못했습니다. 차트 기본 registry 가 쓰일 수 있습니다.",
                  file=sys.stderr)

    aurora = outs.get("application_db_endpoint") or outs.get("aurora_endpoint")
    if aurora:
        args += ["--set", f"database.external.host={aurora}"]
    if outs.get("elasticache_endpoint"):
        args += ["--set", f"redis.external.host={outs['elasticache_endpoint']}",
                 "--set", "redis.external.tls=true"]

    for svc, key in (("gatewayProxy", "gateway_proxy_role_arn"),
                     ("adminApi", "admin_api_role_arn")):
        arn = outs.get(key)
        if arn:
            args += ["--set-string",
                     f"{svc}.serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn={arn}"]

    issuer = outs.get("cognito_issuer_url")
    if issuer and not cfg.oidc.enabled:
        # yaml 에 oidc 가 없어도 terraform 이 Cognito 를 만들었으면 활성화 — 기존 스크립트 계약
        args += ["--set", "adminApi.oidc.enabled=true",
                 "--set", f"adminApi.oidc.issuerUrl={issuer}",
                 "--set", "adminApi.oidc.providerName=oidc:cognito",
                 "--set", "adminApi.oidc.groupsClaim=cognito:groups"]

    if cfg.features.body_logging:
        if outs.get("body_log_firehose_stream"):
            args += ["--set",
                     f"gatewayProxy.env.FIREHOSE_STREAM_NAME={outs['body_log_firehose_stream']}"]
        if outs.get("body_log_bucket"):
            args += ["--set",
                     f"gatewayProxy.env.BODY_LOG_S3_BUCKET={outs['body_log_bucket']}"]
    return args


def _rollback_flag() -> str:
    r = _run_quiet(["helm", "version", "--template", "{{.Version}}"], timeout=15)
    major = 3
    m = re.search(r"v(\d+)", r.stdout or "")
    if m:
        major = int(m.group(1))
    return "--rollback-on-failure" if major >= 4 else "--atomic"


def _helm_argv(meta: dict, set_args: list[str], *, dry_run: bool = False,
               extra_sets: list[str] | None = None,
               context: str = "") -> list[str]:
    argv = ["helm", "upgrade", "--install", meta["release"], meta["chart"],
            "--namespace", meta["namespace"], "--create-namespace",
            "--timeout", "15m"]
    if context:
        argv += ["--kube-context", context]
    if not dry_run:
        argv += ["--wait", _rollback_flag(), "--cleanup-on-fail"]
    for f in meta.get("values_layers", []):
        argv += ["--values", f]
    argv += set_args
    argv += extra_sets or []
    if dry_run:
        argv.append("--dry-run")
    return argv


def live_alb_hostname(meta: dict, context: str = "") -> str:
    argv = ["kubectl", "get", "ingress", "-n", meta["namespace"], "-o", "json"]
    if context:
        argv += ["--context", context]
    r = _run_quiet(argv, timeout=60)
    if r.returncode != 0:
        return ""
    try:
        for item in json.loads(r.stdout).get("items", []):
            lb = ((item.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
            if lb and lb[0].get("hostname"):
                return lb[0]["hostname"]
    except (json.JSONDecodeError, KeyError, IndexError):
        pass
    return ""


def plan(cfg: GatewayConfig, gen_dir: Path, repo_root: Path, context: str = "") -> None:
    """helm upgrade --dry-run — 무엇이 바뀌는지만 보여준다."""
    meta = load_meta(gen_dir, repo_root)
    outs = tf_outputs(repo_root / meta["env_dir"]) if meta.get("env_dir") else {}
    if meta.get("env_dir") and not outs:
        print(f"ⓘ {meta['env_dir']} 의 terraform output 이 없습니다 — "
              "동적 --set 값(DB/Redis/IRSA/cognito) 없이 dry-run 합니다", file=sys.stderr)
    set_args = dynamic_set_args(cfg, outs, cfg.aws.region)
    _run(_helm_argv(meta, set_args, dry_run=True, context=context), cwd=repo_root)


def apply(cfg: GatewayConfig, gen_dir: Path, repo_root: Path, context: str = "") -> dict:
    meta = load_meta(gen_dir, repo_root)
    outs = tf_outputs(repo_root / meta["env_dir"]) if meta.get("env_dir") else {}
    if meta.get("env_dir") and not outs:
        print(f"ⓘ {meta['env_dir']} 의 terraform output 이 없습니다 — helm 만 "
              "업데이트합니다 (인프라 동적값 주입 생략)", file=sys.stderr)
    set_args = dynamic_set_args(cfg, outs, cfg.aws.region)

    _run(_helm_argv(meta, set_args, context=context), cwd=repo_root)

    # 2-phase: domain=none 이면 ALB DNS 를 nextauthUrl 로 (누적 안 되니 매번 주입)
    # 확인 게이트는 1차 upgrade 전에만 있다 — 2차는 같은 트랜잭션의 후속 조치
    if not cfg.domain.name:
        host = live_alb_hostname(meta, context)
        if host:
            print(f"adminUi.nextauthUrl 을 live ALB 주소({host})로 반영 — "
                  "이 2차 upgrade 는 1차에서 확인된 계획의 후속입니다", file=sys.stderr)
            _run(_helm_argv(meta, set_args, context=context,
                            extra_sets=["--set", f"adminUi.nextauthUrl=http://{host}"]),
                 cwd=repo_root)
    return {"release": meta["release"], "namespace": meta["namespace"],
            "alb_hostname": live_alb_hostname(meta, context)}
