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

import difflib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from .schema import GatewayConfig
from . import pricinglambda, websearch


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
                     f"gatewayProxy.env.BODY_LOG_S3_BUCKET={outs['body_log_bucket']}",
                     # 본문 뷰어 — 읽기는 admin-api 가 수행하므로 버킷 이름을 같이 준다
                     "--set",
                     f"adminApi.env.BODY_LOG_S3_BUCKET={outs['body_log_bucket']}"]
    return args


def ensure_selfsigned_cert(env: str, alb_host: str, region: str) -> str:
    """domain=selfsigned-temp 용 self-signed 인증서를 ACM 에 보장하고 ARN 을
    돌려준다. CN 에 env 를 넣어 재실행 때 기존 인증서를 찾아 재사용(멱등)한다.
    ALB 가 요구하는 것은 리스너 인증서뿐이라 신뢰 체인은 무관 — 브라우저
    경고는 사용자 수용 조건이다. 실패 시 빈 문자열."""
    cn = f"llm-gateway-{env}-selfsigned"
    r = _run_quiet(["aws", "acm", "list-certificates", "--region", region,
                    "--query", f"CertificateSummaryList[?DomainName=='{cn}']"
                               ".CertificateArn | [0]",
                    "--output", "text"], timeout=30)
    arn = (r.stdout or "").strip()
    if r.returncode == 0 and arn and arn != "None":
        return arn
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        key, crt = f"{td}/key.pem", f"{td}/cert.pem"
        r = _run_quiet(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-days", "3650", "-keyout", key, "-out", crt,
                        "-subj", f"/CN={cn}",
                        "-addext", f"subjectAltName=DNS:{alb_host}"], timeout=30)
        if r.returncode != 0:
            return ""
        r = _run_quiet(["aws", "acm", "import-certificate",
                        "--certificate", f"fileb://{crt}",
                        "--private-key", f"fileb://{key}",
                        "--region", region,
                        "--query", "CertificateArn", "--output", "text"],
                       timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""


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


def live_ingress_rule_host(meta: dict, context: str = "") -> str:
    """admin-ui Ingress 의 spec.rules[].host — 차트 NEXTAUTH_URL 파생 대상.

    domain=none 인데 env values 가 실제 host 를 공급하는 채택 배포에서는 차트가
    이미 https://<host> 를 NEXTAUTH_URL 로 파생한다 — 이 경우 아래 2-phase 의
    ALB 주입이 오히려 정상 값을 raw ALB DNS 로 덮어 OIDC 콜백을 깨뜨린다.
    host 가 비어 있는 fresh install 에서만 2-phase 가 필요하다.
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
            for rule in (item.get("spec") or {}).get("rules") or []:
                if rule.get("host"):
                    return rule["host"]
    except (json.JSONDecodeError, KeyError, IndexError):
        pass
    return ""


def strip_es_owned_secrets(meta: dict, context: str = "") -> None:
    """ES 교체 시 ownerRef 에 끌려 삭제되는 Secret 보존 (rebuild-image.sh R24-N6r2).

    구 ES(creationPolicy: Owner)가 만든 Secret 은 metadata.ownerReferences 에
    ExternalSecret 이 남는다 — helm upgrade 가 ES 를 지우면 GC 가 Secret 도
    같이 삭제해 ESO 재생성까지 pod 가 전부 CreateContainerConfigError 로 멈춘다.
    upgrade 전에 stale ownerRef 만 떼어둔다(데이터·키는 그대로). 멱등 — 이미
    Orphan 이거나 ownerRef 가 없으면 아무것도 안 한다.
    """
    ns = meta["namespace"]
    base = ["kubectl", "-n", ns]
    if context:
        base += ["--context", context]
    for suffix in ("app", "db", "redis"):
        sec = f"{meta['release']}-{suffix}"
        r = _run_quiet([*base, "get", "secret", sec,
                        "-o", "jsonpath={.metadata.ownerReferences[0].kind}"],
                       timeout=30)
        if r.returncode != 0 or (r.stdout or "").strip() != "ExternalSecret":
            continue
        p = _run_quiet([*base, "patch", "secret", sec, "--type=json",
                        "-p", '[{"op":"remove","path":"/metadata/ownerReferences"}]'],
                       timeout=30)
        if p.returncode == 0:
            print(f"  stale ownerRef 제거: secret/{sec}", file=sys.stderr)


def _infra_targets(cfg: GatewayConfig) -> list[str]:
    """gateway.yaml 이 소유하는 terraform 대상 — 선언 키가 먹는 모듈로 좁힌다.
    ops 8-V 의 `terraform apply -target=module.body_logging -target=module.irsa`
    와 같은 계약 — env 전체 apply 는 하지 않는다(클러스터·VPC 는 사람 소유)."""
    modules = set()
    if cfg.features.body_logging:
        modules |= {"module.body_logging", "module.irsa"}
    if cfg.features.notifications.provider == "ses":
        modules.add("module.irsa")  # enable_notification_ses → worker IRSA
    if cfg.infra.bedrock_allowed_model_arns or cfg.infra.mantle_regions:
        # managed overlay 소유 키가 IRSA 정책(bedrock/mantle statement)을 만든다.
        # 같은 ARN 이 module.agentcore_runtime 입력에도 쓰이지만 그 모듈은
        # web_search 프로비저닝 경로가 별도로 관리하므로 여기선 대상에 넣지 않는다.
        modules.add("module.irsa")
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


def _managed_infra_plan(cfg: GatewayConfig, gen_dir: Path,
                        env_dir: Path) -> subprocess.CompletedProcess | None:
    """managed overlay 소유 모듈로 좁힌 terraform plan — rc 0=수렴, 2=drift.

    env 전체 plan 은 사람 소유 리소스(EKS 자동 업그레이드 같은 외부 변경)의
    잡음이 섞이므로 _infra_targets 가 정한 yaml 소유 모듈만 본다. 미리보기
    전용이라 -lock=false (상태 잠금과 무관하게 읽기만 한다)다."""
    managed_tfvars = gen_dir / "terraform.tfvars"
    targets = _infra_targets(cfg)
    if not targets or not managed_tfvars.exists() or not env_dir.is_dir():
        return None
    _run_quiet(["terraform", "init", "-input=false"], cwd=env_dir, timeout=300)
    return _run_quiet(
        ["terraform", "plan", "-detailed-exitcode", "-input=false", "-no-color",
         "-lock=false", f"-var-file={managed_tfvars}", *targets],
        cwd=env_dir, timeout=300)


def _target_modules(targets: list[str]) -> list[str]:
    return [t.removeprefix("-target=") for t in targets]


def _inspect_managed_plan(env_dir: Path, plan_path: Path,
                          modules: list[str]) -> tuple[list[str], list[str]]:
    """저장된 plan 을 terraform show -json 으로 검사한다.

    반환은 (거부 사유 목록, 허용된 변경 목록). -target 은 대상 모듈의 *의존성*
    (module.eks·module.cognito 같은 사람 소유 리소스) 변경까지 plan 에
    포함시키므로, 적용 전에 (a) 주소가 target 모듈 안이고 (b) destroy/replace
    계열 액션이 없는지를 검증해 자동 수렴의 폭발 반경을 yaml 소유 모듈로
    한정한다."""
    r = _run_quiet(["terraform", "show", "-json", str(plan_path)],
                   cwd=env_dir, timeout=120)
    if r.returncode != 0:
        return [f"terraform show -json 실패(rc={r.returncode}) — plan 검증 불가"], []
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return ["terraform show -json 출력 파싱 실패 — plan 검증 불가"], []
    bad: list[str] = []
    changes: list[str] = []
    for rc_ in data.get("resource_changes") or []:
        if rc_.get("mode") != "managed":
            continue
        addr = str(rc_.get("address", ""))
        acts = (rc_.get("change") or {}).get("actions") or []
        # 의존성 리소스는 변경 없이 no-op/read 로 plan 에 나타난다 — 범위 검사
        # 전에 걸러야 한다(걸리면 모든 수렴이 "범위 밖" 오진으로 중단된다).
        if not acts or acts == ["no-op"] or acts == ["read"]:
            continue
        in_scope = any(addr == m or addr.startswith(m + ".")
                       or addr.startswith(m + "[") for m in modules)
        if not in_scope:
            bad.append(f"{addr} — target 모듈({', '.join(modules)}) 밖")
        elif any(a in ("delete", "replace", "forget") for a in acts):
            bad.append(f"{addr} — {'+'.join(acts)} (파괴적 변경은 자동 적용 불가)")
        else:
            changes.append(f"{addr} ({'+'.join(acts)})")
    return bad, changes


def _apply_managed(env_dir: Path, targets: list[str], var_args: list[str],
                   reason: str, required: bool) -> bool:
    """plan -out → show -json 범위 검증 → 저장 plan apply.

    재계획 없이 검증된 그 plan 파일을 그대로 적용해 preview↔apply 갭을 없앤다.
    required(출력이 비어 필수)일 때 plan 자체가 실패하면 배포를 중단하고,
    아니면 경고만 남기고 helm 을 계속 진행한다."""
    init = _run_quiet(["terraform", "init", "-input=false"], cwd=env_dir,
                      timeout=300)
    if init.returncode != 0:
        if required:
            print((init.stdout or "") + (init.stderr or ""), file=sys.stderr)
            raise SystemExit(
                f"terraform init 실패(rc={init.returncode}) — {reason}")
        print(f"⚠ terraform init 실패(rc={init.returncode}) — managed 수렴을 "
              "건너뜁니다 (helm 은 계속 진행)", file=sys.stderr)
        return False
    with tempfile.TemporaryDirectory(prefix="nds-plan-") as td:
        plan_path = Path(td) / "managed.tfplan"
        argv = ["terraform", "plan", "-no-color", "-input=false",
                "-out", str(plan_path), *var_args, *targets]
        r = _run_quiet(argv, cwd=env_dir, timeout=600)
        if r.returncode != 0:
            if required:
                print((r.stdout or "") + (r.stderr or ""), file=sys.stderr)
                raise SystemExit(
                    f"managed 인프라 plan 실패(rc={r.returncode}) — {reason} "
                    "인프라 없이는 배포할 수 없습니다")
            print(f"⚠ managed 인프라 plan 실패(rc={r.returncode}) — terraform "
                  "수렴을 건너뜁니다 (helm 은 계속 진행)", file=sys.stderr)
            return False
        bad, changes = _inspect_managed_plan(
            env_dir, plan_path, _target_modules(targets))
        if bad:
            raise SystemExit(
                "managed plan 이 NDS 소유 범위를 벗어나거나 파괴적 변경을 "
                "포함합니다 — 자동 apply 를 중단합니다:\n  - "
                + "\n  - ".join(bad)
                + "\n원인 후보: target 모듈 의존 리소스의 사람 소유 변경, "
                "선언을 거둔 기능의 잔여 인프라(끄려면 env terraform 으로 수동)."
                "\nenv terraform 을 사람이 검토/적용하십시오.")
        if not changes:
            return False
        print(f"ⓘ {reason} — managed terraform 변경 {len(changes)}건을 "
              "적용합니다 (target 모듈 안의 사람 변경이 섞여 있을 수 있습니다):",
              file=sys.stderr)
        for c in changes:
            print(f"    {c}", file=sys.stderr)
        _run(["terraform", "apply", "-input=false", str(plan_path)],
             cwd=env_dir)
        return True


def ensure_infra(cfg: GatewayConfig, gen_dir: Path, env_dir: Path, outs: dict) -> dict:
    """선언된 기능에 필요한 인프라가 terraform output 에 없으면 targeted apply.

    관리 범위는 _infra_targets 이 정한 모듈뿐 — env 전체 apply 는 하지 않는다
    (클러스터·VPC 등 사람 소유). gen/<env>/eks/terraform.tfvars 를 -var-file 로
    뒤에 붙여 선언값을 주입한다. 반환은 최신 terraform outputs.

    output 이 이미 있어도 managed overlay 선언 키(ARN·mantle·기능 플래그)는
    live 와 어긋날 수 있다 — yaml 소유 모듈로 좁힌 plan 을 만들어 변경이
    있으면 같은 저장 plan 으로 수렴한다. 이게 yaml → ./deploy apply 한 경로로
    인프라 키를 관리하는 수렴 경로다."""
    targets = _infra_targets(cfg)
    if not targets:
        return outs
    if not env_dir.is_dir():
        print("⚠ 기능 인프라가 선언됐지만 terraform env 디렉토리가 없습니다 "
              f"({env_dir}) — 인프라 없이 helm 만 진행합니다", file=sys.stderr)
        return outs
    managed_tfvars = gen_dir / "terraform.tfvars"
    var_args = [f"-var-file={managed_tfvars}"] if managed_tfvars.exists() else []
    missing = _infra_missing(cfg, outs)
    if missing:
        print(f"ⓘ {', '.join(missing)} 가 terraform output 에 없습니다 — "
              "targeted apply 로 프로비저닝합니다", file=sys.stderr)
        _apply_managed(env_dir, targets, var_args,
                       ", ".join(missing), required=True)
        fresh = tf_outputs(env_dir)
        # 적용 후에도 출력이 비면(overlay 미렌더·env tfvars false 등 no-op plan)
        # helm 에 빈 env 가 들어간다 — 조용히 진행하지 않는다.
        still = _infra_missing(cfg, fresh)
        if still:
            raise SystemExit(
                f"{', '.join(still)} — terraform 적용 후에도 output 이 "
                "비어 있습니다. managed overlay(render)와 env tfvars 선언을 "
                "확인하십시오")
        return fresh
    if not var_args:
        return outs
    # 출력은 있지만 선언 키(ARN·mantle·기능 플래그)가 live 와 어긋날 수 있다 —
    # 표시용 plan 이 수렴(rc=0)이면 여기서 끝, drift(rc=2)면 저장 plan 을 검증해
    # 그대로 적용한다(재계획 갭 없이).
    p = _managed_infra_plan(cfg, gen_dir, env_dir)
    if p is None or p.returncode == 0:
        return outs
    if p.returncode != 2:
        print(f"⚠ managed 인프라 plan 실패(rc={p.returncode}) — terraform "
              "수렴을 건너뜁니다 (helm 은 계속 진행)", file=sys.stderr)
        return outs
    print("managed overlay 와 live 인프라가 어긋납니다 — 수렴할 diff:",
          file=sys.stderr)
    print(p.stdout)
    applied = _apply_managed(env_dir, targets, var_args,
                             "managed overlay 선언 수렴", required=False)
    return tf_outputs(env_dir) if applied else outs


def _manifest_docs(text: str) -> dict[str, dict]:
    """helm 출력(훅 + 매니페스트 덤프, get manifest 결과)를 (Kind/name) → 리소스로."""
    # dry-run 출력의 NOTES 섹션은 자유 텍스트라 마지막 매니페스트 청크를
    # 오염시켜 파싱 실패 → 허위 삭제로 보인다. YAML 이 아닌 꼬리는 잘라낸다.
    text = re.split(r"^NOTES:\s*$", text, flags=re.M)[0]
    # HOOKS:/MANIFEST:/HOOKS SUCCEEDED: 같은 대문자 섹션 마커는 YAML 키로
    # 흡수돼 허위 diff 를 만든다 — k8s 매니페스트에 all-caps 최상위 키는 없다.
    marker = re.compile(r"^[A-Z][A-Z0-9 _-]+:\s*$")
    docs: dict[str, dict] = {}
    for chunk in re.split(r"^---\s*$", text, flags=re.M):
        body = "\n".join(
            l for l in chunk.splitlines()
            if not l.lstrip().startswith("#") and not marker.match(l)
        ).strip()
        if not body:
            continue
        try:
            obj = yaml.safe_load(body)
        except yaml.YAMLError:
            continue
        if not isinstance(obj, dict):
            continue
        kind, name = obj.get("kind"), (obj.get("metadata") or {}).get("name")
        if kind and name:
            docs[f"{kind}/{name}"] = obj
    return docs


def _canon(obj: dict) -> str:
    """정규화된 YAML — 맵 순서/공백 차이가 diff 로 잡히지 않게 한다."""
    return yaml.dump(obj, sort_keys=True, allow_unicode=True)


def _plan_diff(argv_dry: list[str], meta: dict, repo_root: Path, context: str) -> None:
    """dry-run 렌더 결과를 live release 매니페스트와 비교해 변경분만 출력한다."""
    print(f"$ {' '.join(argv_dry)}", file=sys.stderr)
    r = _run_quiet(argv_dry, cwd=repo_root, timeout=300)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        raise SystemExit(f"helm dry-run 실패 ({r.returncode})")

    get_argv = ["helm", "get", "manifest", meta["release"],
                "-n", meta["namespace"]]
    if context:
        get_argv += ["--kube-context", context]
    g = _run_quiet(get_argv, timeout=60)
    if g.returncode != 0:
        # live release 를 못 읽으면 비교 불가 — 종전처럼 전체 덤프로 폴백.
        print("ⓘ live release 매니페스트를 읽지 못해 전체 dry-run 출력을 보여줍니다",
              file=sys.stderr)
        print(r.stdout)
        return

    new_docs = _manifest_docs(r.stdout)
    live_docs = _manifest_docs(g.stdout)
    added = sorted(k for k in new_docs if k not in live_docs)
    removed = sorted(k for k in live_docs if k not in new_docs)
    changed = sorted(
        k for k in new_docs
        if k in live_docs and _canon(new_docs[k]) != _canon(live_docs[k])
    )

    if not added and not removed and not changed:
        print("\n✓ 변경 없음 — live release 와 렌더 결과가 동일합니다")
        return

    if not live_docs:
        print(f"\n신규 설치 — {len(added)} 개 리소스가 생성됩니다:")
    else:
        print(f"\n변경 요약: {len(changed)} 변경 / {len(added)} 추가 / {len(removed)} 삭제")
    for k in added:
        print(f"  + {k}")
    for k in removed:
        print(f"  - {k}")
    for k in changed:
        print(f"\n  ~ {k}")
        diff = difflib.unified_diff(
            _canon(live_docs[k]).splitlines(),
            _canon(new_docs[k]).splitlines(),
            fromfile="live", tofile="rendered", n=3, lineterm="",
        )
        for line in diff:
            print(f"    {line}")


def plan(cfg: GatewayConfig, gen_dir: Path, repo_root: Path, context: str = "") -> None:
    """helm upgrade --dry-run — 무엇이 바뀌는지만 보여준다.

    dry-run 덤프는 전체 매니페스트라 읽기 어렵다 — 렌더 결과를 live release
    매니페스트와 정규화 비교해 변경/추가/삭제된 리소스와 unified diff 만
    보여준다. live 를 읽지 못하면 종전처럼 전체 덤프로 폴백한다."""
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
    elif meta.get("env_dir"):
        # output 은 있지만 선언 키(ARN·mantle·기능 플래그)가 live 와 어긋난 경우 —
        # 확인 게이트 전에 terraform diff 도 보여준다.
        p = _managed_infra_plan(cfg, gen_dir, repo_root / meta["env_dir"])
        if p is not None and p.returncode == 2:
            print(f"ⓘ managed 인프라 overlay 와 live 가 어긋납니다 — apply 시 "
                  f"검증된 plan 으로 수렴합니다({' '.join(_infra_targets(cfg))})",
                  file=sys.stderr)
            print(p.stdout)
        elif p is not None and p.returncode != 0:
            print(f"⚠ managed 인프라 plan 실패(rc={p.returncode}) — "
                  f"{(p.stderr or '').strip()[:200]}", file=sys.stderr)
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
    if cfg.features.pricing_lambda:
        st = pricinglambda.status(
            cfg.aws.region, cfg.env,
            (outs.get("admin_api_role_arn") or "").rsplit("/", 1)[-1])
        print(f"ⓘ pricing_lambda: function={st['function']} role={st['role']} "
              f"invoke={st['invoke']} — apply 시 없는 것만 보장합니다",
              file=sys.stderr)
    set_args = dynamic_set_args(cfg, outs, cfg.aws.region)
    _plan_diff(_helm_argv(meta, set_args, dry_run=True, context=context),
               meta, repo_root, context)


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

    # domain.selfsigned-temp — admin-ui 만 임시 HTTPS. ALB hostname 은 live
    # ingress 에서 읽으므로 첫 설치(아직 ALB 없음)는 인증서 없이 지나가고 다음
    # apply 에 붙는다. cert-arn 은 values 에 못 박는 런타임값이라 --set 주입.
    if cfg.domain.mode == "selfsigned-temp":
        ss_host = live_alb_hostname(meta, context)
        if ss_host:
            arn = ensure_selfsigned_cert(cfg.env, ss_host, cfg.aws.region)
            if arn:
                set_args += ["--set-string",
                             "ingress.adminUi.annotations."
                             "alb\\.ingress\\.kubernetes\\.io/"
                             "certificate-arn=" + arn]
            else:
                print("⚠ self-signed 인증서 프로비저닝 실패 — 443 리스너 없이 "
                      "진행합니다 (openssl/aws acm 권한 확인)", file=sys.stderr)
        else:
            print("ⓘ admin-ui ALB 가 아직 없습니다 — self-signed 인증서는 "
                  "ALB 생성 후 다음 apply 에 붙습니다", file=sys.stderr)

    # ops/NDS-01 자동화 — role+함수+invoke 정책을 보장하고(멱등) 함수 이름을
    # adminApi.env 로 주입한다. 비면 litellm 소스는 비활성(graceful-off).
    if cfg.features.pricing_lambda:
        fn = pricinglambda.ensure(
            cfg, repo_root, outs.get("admin_api_role_arn", ""))
        if fn:
            set_args += ["--set", f"adminApi.env.LITELLM_PRICING_LAMBDA={fn}"]

    # helm upgrade 가 구 ES 를 지우는 순간 ownerRef 달린 Secret 이 GC 로 같이
    # 지워지지 않게 미리 정리 (이거 없으면 전 pod CreateContainerConfigError)
    strip_es_owned_secrets(meta, context)
    _run(_helm_argv(meta, set_args, context=context), cwd=repo_root)

    # 2-phase: domain=none 이면 ALB DNS 를 nextauthUrl 로 (누적 안 되니 매번 주입)
    # 단 env values 가 실제 host 를 공급하는 채택 배포는 차트가 이미
    # https://<host> 를 파생하므로 ALB 주입을 하면 되려 콜백이 깨진다 —
    # live ingress 에 rule host 가 있으면 생략한다.
    # 확인 게이트는 1차 upgrade 전에만 있다 — 2차는 같은 트랜잭션의 후속 조치
    if not cfg.domain.name and not live_ingress_rule_host(meta, context):
        host = live_alb_hostname(meta, context)
        if host:
            scheme = "https" if cfg.domain.mode == "selfsigned-temp" else "http"
            print(f"adminUi.nextauthUrl 을 live ALB 주소({host})로 반영 — "
                  "이 2차 upgrade 는 1차에서 확인된 계획의 후속입니다", file=sys.stderr)
            _run(_helm_argv(meta, set_args, context=context,
                            extra_sets=["--set",
                                        f"adminUi.nextauthUrl={scheme}://{host}"]),
                 cwd=repo_root)
    return {"release": meta["release"], "namespace": meta["namespace"],
            "alb_hostname": live_alb_hostname(meta, context)}
