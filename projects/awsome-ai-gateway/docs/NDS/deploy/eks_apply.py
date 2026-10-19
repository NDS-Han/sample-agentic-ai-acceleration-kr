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
from . import websearch


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
    # install-eks.sh 계약 — output 이 없으면 차트 기본 "gateway" 로 수렴한다
    args += ["--set",
             f"database.external.name={outs.get('aurora_database_name') or 'gateway'}"]
    if outs.get("elasticache_endpoint"):
        args += ["--set", f"redis.external.host={outs['elasticache_endpoint']}",
                 "--set", "redis.external.tls=true"]

    for svc, key in (("gatewayProxy", "gateway_proxy_role_arn"),
                     ("adminApi", "admin_api_role_arn")):
        arn = outs.get(key)
        if arn:
            args += ["--set-string",
                     f"{svc}.serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn={arn}"]

    # notification-worker SES IRSA (install-eks.sh R24-S7 와 동일) — provider=ses
    # 인데 role 이 없으면 SA 에 자격이 없어 발송이 전량 실패한다.
    if cfg.features.notifications.provider == "ses":
        ses_arn = outs.get("notification_worker_ses_role_arn")
        if ses_arn:
            args += ["--set-string",
                     f"notificationWorker.serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn={ses_arn}"]
        else:
            print("⚠ notifications.provider=ses 인데 terraform output 에 "
                  "notification_worker_ses_role_arn 이 없습니다 — SES 발송이 실패합니다. "
                  "enable_notification_ses=true 로 인프라를 apply 했는지 확인하세요",
                  file=sys.stderr)

    issuer = outs.get("cognito_issuer_url")
    if issuer and not cfg.oidc.enabled:
        # yaml 에 oidc 가 없어도 terraform 이 Cognito 를 만들었으면 활성화 — 기존
        # 스크립트 계약. 단 admin-ui 까지 같은 OIDC 로 맞추지 않으면 admin-api=OIDC /
        # admin-ui=dev-login 의 비일관 배포가 된다 — terraform output 으로 양쪽을
        # 함께 배선하고, client_id 가 없으면 일관 상태를 만들 수 없으니 실패시킨다.
        client_id = outs.get("cognito_client_id")
        if not client_id:
            raise SystemExit(
                "terraform 이 Cognito 를 만들었지만 cognito_client_id output 이 "
                "없습니다 — 일관된 OIDC 상태를 만들 수 없습니다. gateway.yaml 의 "
                "oidc: issuer_url/client_id/authorize_url/token_url 을 채우세요"
            )
        hosted = outs.get("cognito_hosted_ui_domain")
        print("⚠ gateway.yaml 의 oidc 가 비어 있는데 terraform 이 Cognito 를 만들어 "
              "둔 상태입니다 — terraform output 으로 admin-api/admin-ui 양쪽에 OIDC 를 "
              "배선합니다 (의도가 아니면 gateway.yaml 에 oidc: 를 채우세요)",
              file=sys.stderr)
        args += ["--set", "adminApi.oidc.enabled=true",
                 "--set", f"adminApi.oidc.issuerUrl={issuer}",
                 "--set", f"adminApi.oidc.audience={client_id}",
                 "--set", "adminApi.oidc.providerName=oidc:cognito",
                 "--set", "adminApi.oidc.groupsClaim=cognito:groups",
                 "--set", "adminUi.env.DEV_LOGIN_ENABLED=false",
                 "--set", f"adminUi.env.OIDC_CLIENT_ID={client_id}"]
        if hosted:
            args += ["--set",
                     f"adminUi.env.OIDC_AUTHORIZE_URL=https://{hosted}/oauth2/authorize",
                     "--set",
                     f"adminUi.env.OIDC_TOKEN_URL=https://{hosted}/oauth2/token"]

    if cfg.features.body_logging:
        if outs.get("body_log_firehose_stream"):
            args += ["--set",
                     f"gatewayProxy.env.FIREHOSE_STREAM_NAME={outs['body_log_firehose_stream']}"]
        if outs.get("body_log_bucket"):
            args += ["--set",
                     f"gatewayProxy.env.BODY_LOG_S3_BUCKET={outs['body_log_bucket']}"]
    return args


def _helm_version() -> tuple[int, int]:
    r = _run_quiet(["helm", "version", "--template", "{{.Version}}"], timeout=15)
    m = re.search(r"v(\d+)(?:\.(\d+))?", r.stdout or "")
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (3, 0)


def _rollback_flag() -> str:
    major, _ = _helm_version()
    return "--rollback-on-failure" if major >= 4 else "--atomic"


def _dry_run_flag() -> str:
    # server dry-run (helm ≥3.13)은 라이브 release 와의 실제 diff/검증을 보여준다 —
    # client dry-run 은 훅·기존 리소스·클러스터 접속 문제를 놓친다.
    major, minor = _helm_version()
    return "--dry-run=server" if (major, minor) >= (3, 13) else "--dry-run"


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
        argv.append(_dry_run_flag())
    return argv


def live_alb_hostname(meta: dict, context: str = "") -> str:
    """admin-ui Ingress 의 ALB hostname — NEXTAUTH_URL 대상은 admin-ui 다.

    차트는 gateway/admin-ui/admin-api **세 개의 별도 Ingress**(group.name 미사용,
    각자 ALB)를 만들고, kubectl 리스트는 이름순(`…-admin-api` 가 먼저)이라
    "첫 hostname" 을 집으면 admin-api 나 gateway 의 ALB 가 NEXTAUTH_URL 로
    들어가 OIDC 콜백이 깨진다. 이름 끝이 `-admin-ui` 인 항목만 고른다
    (install-eks.sh 의 `{release}-admin-ui` 명명과 동일 대상 — fullnameOverride
    도 접미사는 같다).
    """
    argv = ["kubectl", "get", "ingress", "-n", meta["namespace"], "-o", "json"]
    if context:
        argv += ["--context", context]
    r = _run_quiet(argv, timeout=60)
    if r.returncode != 0:
        return ""
    try:
        for item in json.loads(r.stdout).get("items", []):
            name = (item.get("metadata") or {}).get("name") or ""
            if not name.endswith("-admin-ui"):
                continue
            lb = ((item.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
            if lb and lb[0].get("hostname"):
                return lb[0]["hostname"]
    except (json.JSONDecodeError, KeyError, IndexError):
        pass
    return ""


def _infra_targets(cfg: GatewayConfig) -> list[str]:
    """gateway.yaml 이 소유하는 terraform 대상 — body_logging 모듈 + 그 IAM 배선.
    ops 8-V 의 `terraform apply -target=module.body_logging -target=module.irsa`
    와 같은 계약."""
    modules = set()
    if cfg.features.body_logging:
        modules |= {"module.body_logging", "module.irsa"}
    if cfg.features.notifications.provider == "ses":
        modules.add("module.irsa")  # enable_notification_ses → worker IRSA
    return sorted(f"-target={m}" for m in modules)


def _infra_missing(cfg: GatewayConfig, outs: dict) -> list[str]:
    """선언은 됐는데 terraform output 이 비어 있는 기능."""
    missing = []
    if (cfg.features.body_logging
            and not (outs.get("body_log_firehose_stream") and outs.get("body_log_bucket"))):
        missing.append("body_logging(Firehose/S3/IRSA)")
    if (cfg.features.notifications.provider == "ses"
            and not outs.get("notification_worker_ses_role_arn")):
        missing.append("notification-worker SES IRSA")
    return missing


def ensure_infra(cfg: GatewayConfig, gen_dir: Path, env_dir: Path, outs: dict) -> dict:
    """선언된 기능에 필요한 인프라가 terraform output 에 없으면 targeted apply.

    관리 범위는 _infra_targets 이 정한 모듈뿐 — env 전체 apply 는 하지 않는다
    (클러스터·VPC 등 사람 소유). gen/<env>/eks/terraform.tfvars 를 -var-file 로
    뒤에 붙여 선언값을 주입한다. 반환은 최신 terraform outputs."""
    targets = _infra_targets(cfg)
    if not targets:
        return outs
    if not env_dir.is_dir():
        print("⚠ 기능 인프라가 선언됐지만 terraform env 디렉토리가 없습니다 "
              f"({env_dir}) — 인프라 없이 helm 만 진행합니다", file=sys.stderr)
        return outs
    missing = _infra_missing(cfg, outs)
    if not missing:
        return outs
    managed_tfvars = gen_dir / "terraform.tfvars"
    var_args = [f"-var-file={managed_tfvars}"] if managed_tfvars.exists() else []
    print(f"{', '.join(missing)} 가 없습니다 — {env_dir} 에서 targeted "
          "terraform apply 를 실행합니다", file=sys.stderr)
    _run(["terraform", "init", "-input=false"], cwd=env_dir)
    _run(["terraform", "apply", "-auto-approve", "-input=false",
          *var_args, *targets], cwd=env_dir)
    return tf_outputs(env_dir)


def plan(cfg: GatewayConfig, gen_dir: Path, repo_root: Path, context: str = "") -> None:
    """helm upgrade --dry-run — 무엇이 바뀌는지만 보여준다."""
    meta = load_meta(gen_dir, repo_root)
    outs = tf_outputs(repo_root / meta["env_dir"]) if meta.get("env_dir") else {}
    if meta.get("env_dir") and not outs:
        print(f"ⓘ {meta['env_dir']} 의 terraform output 이 없습니다 — "
              "동적 --set 값(DB/Redis/IRSA/cognito) 없이 dry-run 합니다", file=sys.stderr)
    missing = _infra_missing(cfg, outs)
    if missing:
        print(f"ⓘ {', '.join(missing)} 가 terraform output 에 없습니다 — apply 시 "
              f"targeted terraform apply({' '.join(_infra_targets(cfg))})가 "
              "먼저 실행됩니다", file=sys.stderr)
    if cfg.features.web_search:
        url = websearch.status_url(cfg.env, repo_root,
                                   outs.get("gateway_proxy_role_arn", ""))
        if url:
            print(f"ⓘ web_search: AgentCore Gateway {url} 를 --set 합니다",
                  file=sys.stderr)
        else:
            print("ⓘ web_search: AgentCore Gateway 가 없습니다 — apply 시 "
                  "프로비저닝(provision_agentcore_websearch.py deploy)합니다",
                  file=sys.stderr)
    set_args = dynamic_set_args(cfg, outs, cfg.aws.region)
    _run(_helm_argv(meta, set_args, dry_run=True, context=context), cwd=repo_root)


def apply(cfg: GatewayConfig, gen_dir: Path, repo_root: Path, context: str = "") -> dict:
    meta = load_meta(gen_dir, repo_root)
    env_dir = repo_root / meta["env_dir"] if meta.get("env_dir") else None
    outs = tf_outputs(env_dir) if env_dir else {}
    if env_dir and not outs:
        print(f"ⓘ {meta['env_dir']} 의 terraform output 이 없습니다 — helm 만 "
              "업데이트합니다 (인프라 동적값 주입 생략)", file=sys.stderr)
    if env_dir:
        outs = ensure_infra(cfg, gen_dir, env_dir, outs)
    set_args = dynamic_set_args(cfg, outs, cfg.aws.region)

    # install-guide §5-1/§5-2 자동화 — AgentCore Gateway 를 만들고(멱등) URL 을
    # pod env 로 주입한다. caller 는 gateway-proxy IRSA — InvokeGateway 스코프.
    if cfg.features.web_search:
        url = websearch.ensure_gateway_url(
            cfg.env, repo_root,
            caller_role_arn=outs.get("gateway_proxy_role_arn", ""))
        if url:
            set_args += [
                "--set", f"gatewayProxy.env.AGENTCORE_GATEWAY_URL={url}",
                "--set", f"gatewayProxy.env.AGENTCORE_REGION={websearch.AGENTCORE_REGION}",
                "--set", f"gatewayProxy.env.AGENTCORE_TARGET_ID={websearch.TARGET_ID}"]
        else:
            print("⚠ web_search URL 을 얻지 못했습니다 — WEB_SEARCH_ENABLED 만 "
                  "켜진 상태로 진행합니다 (라우터가 검색을 건너뜀)", file=sys.stderr)

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
