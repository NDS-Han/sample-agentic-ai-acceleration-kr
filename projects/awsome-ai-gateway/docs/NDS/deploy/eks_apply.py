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
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .schema import GatewayConfig
from . import chatagent, pricinglambda, websearch


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


def _live_deploy_env(meta: dict, deploy_suffix: str, var: str,
                     context: str = "") -> str:
    """라이브 deployment 의 env 값 — 조회 실패·미설정 시 ""."""
    argv = ["kubectl", "get", "deploy", f"{meta['release']}-{deploy_suffix}",
            "-n", meta["namespace"], "-o", "json"]
    if context:
        argv += ["--context", context]
    r = _run_quiet(argv)
    if r.returncode != 0:
        return ""
    try:
        env = json.loads(r.stdout)["spec"]["template"]["spec"][
            "containers"][0].get("env", [])
        return next((e.get("value", "") for e in env
                     if e.get("name") == var), "")
    except (json.JSONDecodeError, KeyError, IndexError):
        return ""


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


def _migration_image(cfg: GatewayConfig, meta: dict, gen_dir: Path,
                     repo_root: Path) -> str:
    """psql 이 든 migration 이미지의 풀 URI — helm values 레이어(base → overlay
    → gen, last-wins)를 그대로 병합해 실제 배포되는 이미지와 동일한 URI 를 만든다.
    레지스트리는 global.imageRegistry → images.registry → ECR 추론 순."""
    layers = [repo_root / "deployment/charts/llm-gateway/values.yaml"]
    for layer in meta.get("values_layers", []):
        lp = Path(layer)
        layers.append(lp if lp.is_absolute() else repo_root / lp)
    repo, tag, registry = "llm-gateway/migration", "", ""
    for lp in layers:
        if not lp.exists():
            continue
        v = yaml.safe_load(lp.read_text()) or {}
        mig = v.get("migration") or {}
        repo = (mig.get("image") or {}).get("repository") or repo
        tag = (mig.get("image") or {}).get("tag") or tag
        registry = (v.get("global") or {}).get("imageRegistry") or registry
    if not registry:
        registry = cfg.images.registry
    if not registry:
        acct = _aws_account_id(cfg.aws.region)
        if acct:
            registry = f"{acct}.dkr.ecr.{cfg.aws.region}.amazonaws.com"
    if not registry or not tag:
        return ""  # 프로비저너가 비밀번호 단계를 건너뛰고 경고를 낸다
    return f"{registry}/{repo}:{tag}"


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

    # RDS 관리형 master 시크릿(rds!cluster-<uuid>) — ESO 가 직접 읽게 해
    # 로테이션 드리프트를 원천 차단한다(fill-org-values.sh 와 같은 계약).
    # 비어 두면 ExternalSecret 이 /db 의 없는 master_password 를 조회해
    # SecretSyncedError → 시크릿 전체 미생성 → 신규 설치가 기동 불가(R24-H1).
    master_arn = outs.get("aurora_master_user_secret_arn")
    if master_arn:
        m = re.search(r":secret:(rds!cluster-[0-9a-f-]+)-[A-Za-z0-9]{6}$",
                      master_arn)
        if m:
            args += ["--set",
                     f"database.external.masterPasswordRemoteKey={m.group(1)}",
                     "--set",
                     "database.external.masterPasswordRemoteProperty=password"]
        else:
            print(f"⚠ aurora_master_user_secret_arn 의 형식이 예상과 다릅니다 "
                  f"({master_arn}) — masterPasswordRemoteKey 미주입, "
                  "ExternalSecret 이 /db:master_password fallback 으로 실패할 수 "
                  "있습니다", file=sys.stderr)
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
    if issuer and not cfg.oidc.enabled and not cfg.domain.name:
        # domain 없는 ALB 는 http — Cognito 콜백은 https(localhost 제외)만
        # 허용이라 hosted-UI 플로우가 물리적으로 불가. 여기에 OIDC 를 배선하면
        # hosted-UI 는 redirect_mismatch, admin-api 삼중가드가 dev 토큰까지
        # 거부해 완전 잠김 — 배선하지 않고 인증 경로를 명시한다.
        print("⚠ terraform 이 Cognito 를 만들었지만 domain.name 이 없어 "
              "OIDC 를 배선하지 않습니다 — hosted-UI 콜백은 https 전용이라 "
              "http ALB 에선 로그인이 불가합니다. admin 접근 경로를 하나 "
              "선택하세요: (a) domain 을 부착해 OIDC 사용 (권장), (b) "
              "features.dev_login=true — 단 dev 토큰은 서명이 없어 인터넷 "
              "노출 ALB 에서는 위험하니 인그레스 CIDR 제한과 함께 임시로만 "
              "사용하세요", file=sys.stderr)
    elif issuer and not cfg.oidc.enabled:
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

    if (not cfg.oidc.enabled and not cfg.domain.name
            and not cfg.features.dev_login
            and not (issuer and outs.get("cognito_client_id"))):
        # OIDC 도메인도 없고 dev-login 도 꺼진 배포 — admin-ui 는 dev-login
        # 화면을 띄우지만 admin-api 가 dev 토큰을 거부해 실질 로그인 경로가
        # 없다. 배포 자체는 유효하니 경고만 (자동 활성화는 인터넷 노출 ALB 에
        # 서명 없는 토큰을 여는 셈이라 하지 않는다).
        print("⚠ 작동하는 admin 로그인 경로가 없습니다 — oidc 미설정, "
              "domain.name 없음, features.dev_login=false. 다음 중 하나를 "
              "선택하세요: (a) domain 부착 + oidc 설정 (권장), (b) "
              "features.dev_login=true — dev 토큰은 서명이 없어 인터넷 노출 "
              "ALB 에선 인그레스 CIDR 제한과 함께 임시로만", file=sys.stderr)

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


def _alb_hostname_by_suffix(meta: dict, suffix: str, context: str = "") -> str:
    """인그레스 이름 끝이 suffix 인 항목의 ALB hostname (예: -gateway/-admin-ui)."""
    argv = ["kubectl", "get", "ingress", "-n", meta["namespace"], "-o", "json"]
    if context:
        argv += ["--context", context]
    r = _run_quiet(argv, timeout=60)
    if r.returncode != 0:
        return ""
    try:
        for item in json.loads(r.stdout).get("items", []):
            name = (item.get("metadata") or {}).get("name") or ""
            if name.endswith(suffix):
                lb = ((item.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
                if lb and lb[0].get("hostname"):
                    return lb[0]["hostname"]
    except (json.JSONDecodeError, KeyError, IndexError):
        pass
    return ""


def _find_domain_cert(domain: str, region: str) -> tuple[str, str]:
    """*.<domain>/<domain> 을 커버하는 ACM 인증서 → (arn, status). 없으면 ("","")."""
    r = _run_quiet(["aws", "acm", "list-certificates", "--region", region,
                    "--query", "CertificateSummaryList[].CertificateArn",
                    "--output", "text"], timeout=60)
    for arn in (r.stdout or "").split() if r.returncode == 0 else []:
        d = _run_quiet(["aws", "acm", "describe-certificate",
                        "--certificate-arn", arn, "--region", region,
                        "--query", "Certificate.{S:Status,SAN:SubjectAlternativeNames}",
                        "--output", "json"], timeout=30)
        try:
            c = json.loads(d.stdout or "{}")
        except json.JSONDecodeError:
            continue
        if {f"*.{domain}", domain} & set(c.get("SAN") or []):
            return arn, c.get("S", "")
    return "", ""


def ensure_domain_cert(domain: str, zone_id: str, region: str) -> str:
    """route53-acm: *.<domain> ACM 인증서를 보장하고 ARN 을 돌려준다.
    zone_id 가 있으면 DNS 검증 레코드도 자동 등록하고 발급을 기다린다.
    미발급이면 "" — 이번 apply 는 cert-arn 주입 없이 지나가고 다음에 재시도."""
    arn, status = _find_domain_cert(domain, region)
    if status == "ISSUED":
        return arn
    if not arn:
        r = _run_quiet(["aws", "acm", "request-certificate",
                        "--domain-name", f"*.{domain}",
                        "--subject-alternative-names", domain,
                        "--validation-method", "DNS", "--region", region,
                        "--query", "CertificateArn", "--output", "text"],
                       timeout=60)
        arn = (r.stdout or "").strip()
        if r.returncode != 0 or not arn:
            print(f"⚠ ACM request-certificate 실패: {r.stderr[:200]}",
                  file=sys.stderr)
            return ""
        time.sleep(3)
    if not zone_id:
        print("ⓘ domain.zone_id 가 없어 인증서 검증 CNAME 을 수동 등록해야 "
              "합니다 (등록 후 다음 apply 에 자동 부착)", file=sys.stderr)
        return ""
    d = _run_quiet(["aws", "acm", "describe-certificate",
                    "--certificate-arn", arn, "--region", region,
                    "--query", "Certificate.DomainValidationOptions[].ResourceRecord",
                    "--output", "json"], timeout=30)
    try:
        recs = [x for x in json.loads(d.stdout or "[]") if x]
    except json.JSONDecodeError:
        recs = []
    changes = [{"Action": "UPSERT",
                "ResourceRecordSet": {"Name": rec["Name"], "Type": rec["Type"],
                                      "TTL": 300,
                                      "ResourceRecords": [{"Value": rec["Value"]}]}}
               for rec in recs if rec.get("Name")]
    if changes:
        _run_quiet(["aws", "route53", "change-resource-record-sets",
                    "--hosted-zone-id", zone_id,
                    "--change-batch", json.dumps({"Changes": changes})], timeout=60)
    for _ in range(18):  # 최대 ~3분
        time.sleep(10)
        s = _run_quiet(["aws", "acm", "describe-certificate",
                        "--certificate-arn", arn, "--region", region,
                        "--query", "Certificate.Status", "--output", "text"],
                       timeout=30)
        if (s.stdout or "").strip() == "ISSUED":
            return arn
    print("ⓘ 인증서 검증이 진행 중입니다 — 다음 apply 에 자동 부착됩니다",
          file=sys.stderr)
    return ""


def ensure_domain_records(cfg: GatewayConfig, meta: dict, context: str = "") -> None:
    """route53-acm: gateway./admin./admin-api.<domain> A alias 를 live ALB 로
    upsert 한다. zone_id 없으면 수동 등록 안내만."""
    if not cfg.domain.zone_id:
        print("ⓘ domain.zone_id 없음 — host 3개의 DNS 레코드는 수동 등록입니다",
              file=sys.stderr)
        return
    suffix_to_host = {"-gateway": f"gateway.{cfg.domain.name}",
                      "-admin-ui": f"admin.{cfg.domain.name}",
                      "-admin-api": f"admin-api.{cfg.domain.name}"}
    changes = []
    for suffix, host in suffix_to_host.items():
        alb = _alb_hostname_by_suffix(meta, suffix, context)
        if not alb:
            continue
        z = _run_quiet(
            ["aws", "elbv2", "describe-load-balancers", "--region", cfg.aws.region,
             "--query", f"LoadBalancers[?DNSName=='{alb}']"
                        ".CanonicalHostedZoneId | [0]",
             "--output", "text"], timeout=30)
        zone = (z.stdout or "").strip()
        if not zone or zone == "None":
            continue
        changes.append({"Action": "UPSERT", "ResourceRecordSet": {
            "Name": host, "Type": "A",
            "AliasTarget": {"HostedZoneId": zone, "DNSName": f"dualstack.{alb}",
                            "EvaluateTargetHealth": False}}})
    if not changes:
        return
    r = _run_quiet(["aws", "route53", "change-resource-record-sets",
                    "--hosted-zone-id", cfg.domain.zone_id,
                    "--change-batch", json.dumps({"Changes": changes})],
                   timeout=60)
    if r.returncode == 0:
        print(f"  route53: {len(changes)}개 도메인 레코드 등록 "
              f"(zone {cfg.domain.zone_id})", file=sys.stderr)
    else:
        print(f"⚠ route53 레코드 등록 실패: {r.stderr[:200]}", file=sys.stderr)


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
    if cfg.domain.name:
        modules.add("module.cognito")  # gen tfvars 의 콜백/로그아웃 URL 반영
    if cfg.features.alerting.enabled:
        # SNS topic + Aurora/ElastiCache CloudWatch alarms + RDS event sub.
        # email 구독 확인은 사람 절차라 overlay 가 소유하지 않는다.
        modules.add("module.alerting")
    if cfg.features.bi_insight:
        # admin-chat-agent 인프라(ECR·exec role·staging·tool Lambdas·secret 셸)
        # + admin-api IRSA 의 InvokeAgentRuntime/lambda:InvokeFunction statement.
        modules |= {"module.agentcore_runtime", "module.irsa"}
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
    if (cfg.domain.name and outs.get("cognito_client_id")
            and outs.get("cognito_user_pool_id")):
        want = f"https://admin.{cfg.domain.name}/api/auth/callback"
        r = _run_quiet(
            ["aws", "cognito-idp", "describe-user-pool-client",
             "--user-pool-id", outs["cognito_user_pool_id"],
             "--client-id", outs["cognito_client_id"],
             "--region", cfg.aws.region,
             "--query", "UserPoolClient.CallbackURLs",
             "--output", "json"], timeout=30)
        # 조회 실패를 '없음'으로 오인하면 required apply → no-op plan →
        # still-missing SystemExit 로 건강한 배포가 영구 차단된다 — 일시적
        # API 오류는 미싱이 아니라 알 수 없음으로 건너뛴다.
        if r.returncode != 0:
            pass
        else:
            try:
                have = json.loads(r.stdout or "[]")
            except json.JSONDecodeError:
                have = []
            if want not in have:
                missing.append(f"cognito(admin.{cfg.domain.name} 콜백 등록)")
    if (cfg.features.bi_insight
            and not (outs.get("chat_agent_ecr_url")
                     and outs.get("chat_agent_execution_role_arn"))):
        missing.append("bi_insight(agentcore_runtime ECR/role)")
    if cfg.features.alerting.enabled and not outs.get("alerting_sns_topic_arn"):
        missing.append("alerting(SNS topic/alarms)")
    return missing


_ALB_DIMS_CACHE: dict = {}


def _discover_alb_dims(cfg: GatewayConfig,
                       context: str = "") -> tuple[list[str], list[dict], str]:
    """ingress 의 ALB hostname → CloudWatch LoadBalancer/TargetGroup dimension.

    ALB·TG 는 LB controller 가 배포 시 부여한 id 를 dimension 으로 쓰므로
    plan-time 에 알 수 없다 — 라이브에서 발견한 것만 tfvars 로 내려 알람을
    만든다. 반환 셋째는 실패 사유(빈 결과일 때 경고 문구용). 결과는 한
    프로세스 안에서 (namespace, region, context) 단위로 캐시 — apply 의
    ensure_infra 와 managed plan 이 각각 부르는 중복 AWS 호출을 막는다."""
    if not cfg.features.alerting.enabled:
        return [], [], ""
    key = (cfg.deploy.namespace, cfg.deploy.release, cfg.aws.region, context)
    if key in _ALB_DIMS_CACHE:
        return _ALB_DIMS_CACHE[key]

    def done(lb_dims, tg_pairs, note=""):
        _ALB_DIMS_CACHE[key] = (lb_dims, tg_pairs, note)
        return _ALB_DIMS_CACHE[key]

    # 우리 차트의 ingress 만 — app.kubernetes.io/instance 라벨이 release 와
    # 일치하는 것만. 접미사만 보면 같은 ns 의 외부 *-gateway ingress 도
    # 채택된다. 형제 함수들과 같은 -o json 파싱.
    argv = ["kubectl", "get", "ingress", "-n", cfg.deploy.namespace,
            "-o", "json"]
    if context:
        argv += ["--context", context]
    r = _run_quiet(argv, timeout=30)
    if r.returncode != 0:
        return done([], [], f"kubectl rc={r.returncode}")
    try:
        items = json.loads(r.stdout or "{}").get("items") or []
    except json.JSONDecodeError:
        return done([], [], "kubectl ingress 응답 파싱 실패")
    hostnames = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        labels = ((it.get("metadata") or {}).get("labels") or {})
        if labels.get("app.kubernetes.io/instance") != cfg.deploy.release:
            continue
        for ent in ((it.get("status") or {}).get("loadBalancer") or {}
                    ).get("ingress") or []:
            h = ent.get("hostname") if isinstance(ent, dict) else None
            if h:
                hostnames.add(h)
    if not hostnames:
        return done([], [], "ingress hostname 없음 (ALB 미생성?)")
    r = _run_quiet(["aws", "elbv2", "describe-load-balancers",
                    "--region", cfg.aws.region, "--output", "json"], timeout=60)
    if r.returncode != 0:
        return done([], [],
                    f"describe-load-balancers rc={r.returncode}")
    try:
        lbs = json.loads(r.stdout or "{}").get("LoadBalancers") or []
    except json.JSONDecodeError:
        return done([], [], "describe-load-balancers 응답 파싱 실패")
    lb_dims: list[str] = []
    tg_pairs: list[dict] = []
    matched: set[str] = set()
    for lb in lbs:
        if not isinstance(lb, dict):
            continue
        if lb.get("DNSName") not in hostnames or lb.get("Type") != "application":
            continue
        arn = lb.get("LoadBalancerArn", "")
        if ":loadbalancer/" not in arn:
            continue
        ldim = arn.split(":loadbalancer/")[-1]  # app/<name>/<id>
        lb_dims.append(ldim)
        matched.add(lb["DNSName"])
        r2 = _run_quiet(
            ["aws", "elbv2", "describe-target-groups",
             "--load-balancer-arn", arn,
             "--region", cfg.aws.region, "--output", "json"], timeout=60)
        # TG 조회 실패 = 불완전한 발견 — 이 상태로 쓰면 해당 LB 의 TG 알람이
        # destroy-plan 에 잡힌다. 전체를 실패로 돌려 stale 재사용 경로로.
        if r2.returncode != 0:
            return done([], [],
                        f"describe-target-groups rc={r2.returncode}")
        try:
            tgs = json.loads(r2.stdout).get("TargetGroups") or []
        except json.JSONDecodeError:
            return done([], [], "describe-target-groups 응답 파싱 실패")
        if not tgs:
            # ALB 는 있는데 TG 0 — controller sync 전 불완전 상태. fresh 로
            # 취급하면 기존 unhealthy 알람이 지워진다 → 전체 실패로 돌려
            # stale 재사용/중단 경로로 보낸다.
            return done([], [], "LB 에 TargetGroup 없음 (controller 미동기)")
        for tg in tgs:
            if not isinstance(tg, dict):
                continue
            tg_arn = tg.get("TargetGroupArn", "")
            if ":targetgroup/" not in tg_arn:
                continue
            # CW TargetGroup dimension 은 'targetgroup/<name>/<id>' 형식.
            # TG 가 두 LB 에 붙어 있으면 같은 tg 키가 두 번 들어가
            # alb_unhealthy for_each 가 중복 키로 plan 실패 — 첫 pair 유지
            tg_dim = "targetgroup/" + tg_arn.split(":targetgroup/")[-1]
            if any(p["tg"] == tg_dim for p in tg_pairs):
                continue
            tg_pairs.append({"lb": ldim, "tg": tg_dim})
    if not lb_dims:
        return done([], [], "hostname 과 일치하는 application LB 없음")
    if hostnames - matched:
        # 일부 ingress 의 ALB 가 describe 에 아직 없음 — 부분 dims 로 쓰면
        # 그 LB 의 알람이 delete 된다 (eventual-consistency flap).
        return done([], [], "일부 ingress hostname 의 ALB 미조회 (동기화 대기)")
    if not tg_pairs:
        return done([], [], "유효한 TargetGroup dimension 없음")
    return done(lb_dims, tg_pairs)


def _alerting_var_args(cfg: GatewayConfig, gen_dir: Path,
                       context: str = "") -> tuple[list[str], bool]:
    """ALB dimension 을 별도 gen var-file 로 — render 는 오프라인 순수 함수라
    AWS 호출을 못 하므로 apply/plan 경로(여기)에서 라이브 발견을 주입한다.

    반환 둘째(alb_fresh)는 이번 실행에 라이브 발견이 성공해 파일을 새로 쓴
    경우만 True — 이때만 plan 의 ALB 알람 delete 가 "실측된 교체"로 간주돼
    파괴 가드 화이트리스트가 허용된다. 발견 실패 시에는 False:

    - 이전 발견 파일이 있으면 재사용 (일시적 kubectl/AWS 오류로 dims=[]
      평가돼 기존 알람이 삭제되는 것을 막음 — 단 이때 plan 에 delete 가
      떠도 허용하지 않는다: 실측 없이 지우는 것은 위험)
    - 파일이 없으면(첫 배포 또는 gen 삭제) var-file 없이 진행 — 기존 알람이
      있으면 destroy-plan 이 뜨고 화이트리스트가 닫혀 있어 apply 가
      중단된다 (신규 머신/CI 의 일시적 발견 실패로 알람을 잃지 않기 위해)."""
    if not cfg.features.alerting.enabled:
        return [], False
    lb_dims, tg_pairs, note = _discover_alb_dims(cfg, context)
    p = gen_dir / "alerts-dims.tfvars"
    if not lb_dims:
        if p.exists():
            _warn_once(
                f"alb-stale-{p}",
                f"⚠ alerting: ALB 발견 실패({note}) — 이전 발견 파일 "
                f"{p.name} 을 재사용합니다. stale 일 수 있으니 발견이 "
                "복구되면 자동으로 최신화됩니다")
            return [f"-var-file={p.resolve()}"], False
        _warn_once(
            f"alb-miss-{note}",
            f"⚠ alerting: ALB dimension 을 발견하지 못했습니다({note}) — "
            "ALB 5xx/UnHealthyHost 알람은 아직 생성되지 않습니다. "
            "ingress ALB 가 뜬 뒤 `deploy apply` 를 한 번 더 실행해야 "
            "알람이 만들어집니다. (기존 알람이 있는 상태에서 이 경고가 "
            "뜨면 destroy-plan 이 차단될 수 있습니다 — kubectl/AWS 접근을 "
            "복구하고 재실행하세요)")
        return [], False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # 고유 tmp + os.replace — 동시 apply 가 같은 .tmp 이름으로
        # 교차 쓰기하며 반쪽 tfvars 를 발행하는 창을 없앤다
        fd, tmp_s = tempfile.mkstemp(
            dir=p.parent, prefix=f"{p.name}.", suffix=".tmp")
        tmp = Path(tmp_s)
        try:
            os.close(fd)
            tmp.write_text(
                "# generated by deploy — 라이브 발견 ALB dimension "
                "(apply 마다 재생성)\n"
                f"# ns={cfg.deploy.namespace} region={cfg.aws.region} "
                # 개행 제거 — context 의 \n 이 tfvars 라인을 오염시키는 것 방지
                f"context={(context or 'default').replace(chr(10), ' ')}\n"
                f"alb_lb_dimensions = {json.dumps(sorted(lb_dims))}\n"
                f"alb_target_groups = {json.dumps(tg_pairs, sort_keys=True)}\n")
            tmp.replace(p)
        except BaseException:
            tmp.unlink(missing_ok=True)   # 잔여 tmp 는 다음 run 의 쓰레기
            raise
    except OSError as e:
        print(f"⚠ alerting: {p} 기록 실패({e}) — ALB 알람 주입을 건너뜁니다",
              file=sys.stderr)
        return [], False
    return [f"-var-file={p.resolve()}"], True


_WARNED: set = set()


def _warn_once(key: str, msg: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(msg, file=sys.stderr)


# module.alerting 은 count 모듈 — plan 주소는 module.alerting[0].... 형태라
# 리터럴 prefix 매칭으로는 잡히지 않는다 (회귀: 테스트 주소에 [0] 필수).
_ALB_ALARM_ADDR = re.compile(
    r"^module\.alerting(\[\d+\])?\.aws_cloudwatch_metric_alarm\."
    r"alb_(?:target_5xx|unhealthy)\[")
# email 구독은 yaml 의 선언 리스트 — 제거는 선언된 의도라 허용.
_EMAIL_SUB_ADDR = re.compile(
    r"^module\.alerting(\[\d+\])?\.aws_sns_topic_subscription\.email\[")
# aurora/cache 알람은 상위 모듈 output(member id)이 for_each 키 — 사람이
# env terraform 으로 토폴로지를 줄인 결과를 추종하는 delete 라 허용.
_MEMBER_ALARM_ADDR = re.compile(
    r"^module\.alerting(\[\d+\])?\.aws_cloudwatch_metric_alarm\."
    r"(?:aurora_cpu|cache_cpu|cache_memory|cache_connections)\[")


def _managed_infra_plan(cfg: GatewayConfig, gen_dir: Path,
                        env_dir: Path, context: str = "") -> subprocess.CompletedProcess | None:
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
         "-lock=false", f"-var-file={managed_tfvars}",
         *_alerting_var_args(cfg, gen_dir, context)[0], *targets],
        cwd=env_dir, timeout=300)


def _target_modules(targets: list[str]) -> list[str]:
    return [t.removeprefix("-target=") for t in targets]


def _inspect_managed_plan(env_dir: Path, plan_path: Path,
                          modules: list[str],
                          allow_alb_destroy: bool = False
                          ) -> tuple[list[str], list[str], list[str]]:
    """저장된 plan 을 terraform show -json 으로 검사한다.

    반환은 (bad, changes, blocked) 3-튜플 — bad 는 범위 밖/비인가 파괴 변경
    (무조건 중단), blocked 는 실측 없는 ALB 알람 delete (soft 차단: required
    면 중단, drift 면 경고 후 apply 만 건너뜀), changes 는 허용된 변경.
    -target 은 대상 모듈의 *의존성*
    (module.eks·module.cognito 같은 사람 소유 리소스) 변경까지 plan 에
    포함시키므로, 적용 전에 (a) 주소가 target 모듈 안이고 (b) destroy/replace
    계열 액션이 없는지를 검증해 자동 수렴의 폭발 반경을 yaml 소유 모듈로
    한정한다."""
    r = _run_quiet(["terraform", "show", "-json", str(plan_path)],
                   cwd=env_dir, timeout=120)
    if r.returncode != 0:
        return ([f"terraform show -json 실패(rc={r.returncode}) — plan 검증 "
                 "불가"], [], [])
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return ["terraform show -json 출력 파싱 실패 — plan 검증 불가"], [], []
    bad: list[str] = []
    blocked: list[str] = []  # 실측 없는 ALB 알람 delete — soft 차단 대상
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
            # 선언·실측 리스트 기반 리소스의 축소만 허용한다:
            # - ALB 알람: 발견 dimension 이 for_each 키 — 단 이번 실행에
            #   라이브 발견이 성공해 dims 파일을 새로 쓴 경우(allow_alb_destroy)
            #   만. 발견 실패/파일 부재 상태의 delete 는 차단 — 일시 오류로
            #   알람을 잃지 않기 위해.
            # - email 구독: yaml emails 리스트 축소는 선언된 의도.
            # - aurora/cache 알람: 상위 모듈 output(member id) 축소를 추종.
            if _ALB_ALARM_ADDR.match(addr):
                if allow_alb_destroy:
                    changes.append(f"{addr} ({'+'.join(acts)})")
                else:
                    blocked.append(
                        f"{addr} ({'+'.join(acts)}) — ALB 발견 실패 상태의 "
                        "삭제라 차단 (kubectl/AWS 복구 후 재실행)")
                continue
            if _EMAIL_SUB_ADDR.match(addr) or _MEMBER_ALARM_ADDR.match(addr):
                changes.append(f"{addr} ({'+'.join(acts)})")
                continue
            bad.append(f"{addr} — {'+'.join(acts)} (파괴적 변경은 자동 적용 불가)")
        else:
            changes.append(f"{addr} ({'+'.join(acts)})")
    return bad, changes, blocked


def _apply_managed(env_dir: Path, targets: list[str], var_args: list[str],
                   reason: str, required: bool,
                   allow_alb_destroy: bool = False) -> bool:
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
        bad, changes, blocked = _inspect_managed_plan(
            env_dir, plan_path, _target_modules(targets), allow_alb_destroy)
        if bad:
            raise SystemExit(
                "managed plan 이 NDS 소유 범위를 벗어나거나 파괴적 변경을 "
                "포함합니다 — 자동 apply 를 중단합니다:\n  - "
                + "\n  - ".join(bad)
                + "\n원인 후보: target 모듈 의존 리소스의 사람 소유 변경, "
                "선언을 거둔 기능의 잔여 인프라(끄려면 env terraform 으로 수동)."
                "\nenv terraform 을 사람이 검토/적용하십시오.")
        if blocked:
            msg = ("ALB 발견 실패 상태에서 기존 알람 삭제가 plan 에 있습니다 — "
                   "삭제를 막기 위해 managed terraform 수렴을 건너뜁니다 "
                   "(알람은 보존됩니다):\n  - " + "\n  - ".join(blocked)
                   + "\nkubectl/AWS 접근을 복구하고 재실행하면 실측 dims 로 "
                   "수렴됩니다.")
            if required:
                raise SystemExit(msg)
            # 드리프트 수렴 경로 — 적용만 건너뛰고 helm 은 계속 진행.
            print("⚠ " + msg, file=sys.stderr)
            return False
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


def ensure_infra(cfg: GatewayConfig, gen_dir: Path, env_dir: Path,
                 outs: dict, context: str = "") -> dict:
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
    var_args = ([f"-var-file={managed_tfvars}"] if managed_tfvars.exists() else [])
    alb_args, alb_fresh = _alerting_var_args(cfg, gen_dir, context)
    var_args += alb_args
    missing = _infra_missing(cfg, outs)
    if missing:
        print(f"ⓘ {', '.join(missing)} 가 terraform output 에 없습니다 — "
              "targeted apply 로 프로비저닝합니다", file=sys.stderr)
        _apply_managed(env_dir, targets, var_args,
                       ", ".join(missing), required=True,
                       allow_alb_destroy=alb_fresh)
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
    p = _managed_infra_plan(cfg, gen_dir, env_dir, context)
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
                             "managed overlay 선언 수렴", required=False,
                             allow_alb_destroy=alb_fresh)
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
    if cfg.features.bi_insight:
        # archive_file data source 가 build/ 트리를 zip 한다 — 없으면 tf
        # plan/apply 모두 'missing directory' 로 실패. 어떤 tf 명령보다 먼저.
        chatagent.ensure_lambda_build(repo_root)
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
        p = _managed_infra_plan(cfg, gen_dir, repo_root / meta["env_dir"],
                                context)
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
    if cfg.features.bi_insight:
        arn = chatagent.status_arn(repo_root, outs, env_name=cfg.env,
                                   region=cfg.aws.region)
        if arn:
            print(f"ⓘ bi_insight: AgentCore Runtime {arn} 을 --set 합니다",
                  file=sys.stderr)
        else:
            print("ⓘ bi_insight: AgentCore Runtime 이 없습니다 — apply 시 "
                  "프로비저닝(provision_agentcore_chatagent.py deploy)합니다",
                  file=sys.stderr)
    if cfg.features.pricing_lambda:
        st = pricinglambda.status(
            cfg.aws.region, cfg.env,
            (outs.get("admin_api_role_arn") or "").rsplit("/", 1)[-1])
        print(f"ⓘ pricing_lambda: function={st['function']} role={st['role']} "
              f"invoke={st['invoke']} — apply 시 없는 것만 보장합니다",
              file=sys.stderr)
    set_args = dynamic_set_args(cfg, outs, cfg.aws.region)
    # plan diff 도 apply 와 같은 --set 을 써야 한다 — status 조회만 하고
    # --set 을 안 넣으면 dry-run manifest 가 env 미주입으로 렌더돼
    # "ARN 이 지워진다" 는 거짓 wipe diff 를 보인다 (실제 apply 는 주입함).
    if cfg.features.web_search and url:
        set_args += [
            "--set", f"gatewayProxy.env.AGENTCORE_GATEWAY_URL={url}",
            "--set", f"gatewayProxy.env.AGENTCORE_REGION={websearch.AGENTCORE_REGION}",
            "--set", f"gatewayProxy.env.AGENTCORE_TARGET_ID={websearch.TARGET_ID}"]
    if cfg.features.bi_insight and arn:
        set_args += [
            "--set", f"adminApi.env.AGENTCORE_RUNTIME_ARN={arn}",
            "--set",
            f"adminApi.env.AGENTCORE_REGION={chatagent.resolved_region(cfg.aws.region)}",
            "--set", "adminUi.env.CHAT_ENABLED=true"]
        for key, out_key in (
                ("LAMBDA_QUERY_DB", "chat_agent_query_db_function"),
                ("CHAT_STAGING_BUCKET", "chat_agent_staging_bucket")):
            if outs.get(out_key):
                set_args += ["--set", f"adminApi.env.{key}={outs[out_key]}"]
    _plan_diff(_helm_argv(meta, set_args, dry_run=True, context=context),
               meta, repo_root, context)


def apply(cfg: GatewayConfig, gen_dir: Path, repo_root: Path, context: str = "") -> dict:
    meta = load_meta(gen_dir, repo_root)
    env_dir = repo_root / meta["env_dir"] if meta.get("env_dir") else None
    outs = tf_outputs(env_dir) if env_dir else {}
    if env_dir and not outs:
        print(f"⚠ {meta['env_dir']} 의 terraform output 이 없습니다 — DB/Redis/"
              "IRSA/Cognito 동적 --set 이 전부 빠집니다. env values 가 실측 "
              "엔드포인트를 자체 보유한 경우(외부 인프라)만 유효하고, 그렇지 "
              "않으면 placeholder 로 배포돼 파드가 뜨지 않습니다. terraform "
              "env apply 여부를 확인하세요 — helm 은 계속 진행합니다",
              file=sys.stderr)
    if cfg.features.bi_insight:
        # archive_file data source 가 build/ 트리를 zip 한다 — 없으면
        # managed overlay 의 plan/apply 가 'missing directory' 로 실패.
        chatagent.ensure_lambda_build(repo_root)
    if env_dir:
        outs = ensure_infra(cfg, gen_dir, env_dir, outs, context)
        # helm/kubectl 전에 클러스터를 kubeconfig 에 등록 — 컨텍스트 부재
        # 실패·다른 클러스터 오배포 방지 (멱등, context 지정 시 --alias)
        if outs.get("cluster_name"):
            from .bootstrap import update_kubeconfig
            update_kubeconfig(outs, cfg.aws.region, context)
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

    # bi_insight — AgentCore Runtime 을 만들고(멱등) ARN/연결 env 를 admin-api
    # pod env 로 주입한다. 인프라(ECR·role·staging·Lambdas)는 managed overlay
    # (module.agentcore_runtime)가 먼저 보장한다 — outputs 가 없으면
    # _infra_missing 이 먼저 targeted apply 를 돌렸다.
    # 프로비저너는 chat_reader 비밀번호도 같이 동기화한다 — SM 비밀번호를 채우고
    # kubectl pod(migration 이미지)으로 ALTER ROLE 을 실행한다.
    # 실패해도 배포를 중단하지 않는다 — env 미주입으로 라우터가 503 graceful-off.
    if cfg.features.bi_insight:
        arn = chatagent.ensure_runtime_arn(
            repo_root, outs, meta=meta,
            migration_image=_migration_image(cfg, meta, gen_dir, repo_root),
            context=context, env_name=cfg.env, region=cfg.aws.region)
        if not arn:
            # 프로비저너 실패인데 라이브에 이미 ARN 이 주입돼 있으면 그대로
            # 유지한다 — --set 누락으로 helm 이 values.yaml 기본 '' 으로
            # 덮어 멀쩡한 채팅 설정을 지우는 사고 방지 (리전 오조회로 실제
            # 발생). Runtime 이 실제로 삭제됐다면 재주입해도 503 — wipe 와
            # 같은 최종 상태라 graceful-off 계약은 유지된다.
            prev = _live_deploy_env(meta, "admin-api",
                                    "AGENTCORE_RUNTIME_ARN", context)
            if prev:
                arn = prev
                print("⚠ bi_insight: 프로비저너 실패 — 라이브 ARN 을 보존해 "
                      "재주입합니다 (Runtime 삭제 여부는 doctor 로 확인)",
                      file=sys.stderr)
        if arn:
            set_args += [
                "--set", f"adminApi.env.AGENTCORE_RUNTIME_ARN={arn}",
                "--set",
                f"adminApi.env.AGENTCORE_REGION={chatagent.resolved_region(cfg.aws.region)}",
                # admin-ui 서버사이드 게이트 — /chat 페이지·사이드바 메뉴가
                # 이 env 로 표시 여부를 판정한다 (admin-ui/src/app/chat/page.tsx)
                "--set", "adminUi.env.CHAT_ENABLED=true"]
            for key, out_key in (
                    ("LAMBDA_QUERY_DB", "chat_agent_query_db_function"),
                    ("CHAT_STAGING_BUCKET", "chat_agent_staging_bucket")):
                if outs.get(out_key):
                    set_args += ["--set", f"adminApi.env.{key}={outs[out_key]}"]
        else:
            print("⚠ bi_insight: Runtime ARN 을 얻지 못했습니다 — /chat 은 "
                  "503 (미구성) 상태로 진행합니다", file=sys.stderr)
    elif outs.get("chat_agent_name"):
        # 선언 off 인데 모듈이 아직 살아 있다 = 켰던 적이 있는 첫 off apply.
        # 프로비저너 소유 Runtime 도 정리 — terraform 밖 리소스라 destroy 에 안
        # 잡힌다. outputs 가 없으면(한 번도 안 켬·이미 정리) API 호출 자체를
        # 건너뛴다 — agentcore 권한 없는 배포에서 매번 경고가 나오는 것 방지.
        chatagent.teardown(repo_root, cfg.env, region=cfg.aws.region)

    # domain.route53-acm — ACM 인증서를 보장하고 cert-arn 을 --set 주입한다.
    # 미발급이면 주입 없이 진행하고 다음 apply 에 자동 부착(멱등).
    if cfg.domain.mode == "route53-acm" and cfg.domain.name:
        arn = ensure_domain_cert(cfg.domain.name, cfg.domain.zone_id,
                                 cfg.aws.region)
        if arn:
            for key in ("gateway", "adminUi", "adminApi"):
                set_args += ["--set-string",
                             f"ingress.{key}.annotations."
                             "alb\\.ingress\\.kubernetes\\.io/"
                             "certificate-arn=" + arn]
        else:
            print("ⓘ ACM 인증서가 아직 ISSUED 가 아닙니다 — 이번 apply 는 "
                  "cert 없이 진행하고 다음 apply 에 자동 부착합니다",
                  file=sys.stderr)

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
    # domain.route53-acm — ALB 가 생긴 뒤 host 3개의 A alias 를 upsert 한다
    if cfg.domain.mode == "route53-acm" and cfg.domain.name:
        ensure_domain_records(cfg, meta, context)
    return {"release": meta["release"], "namespace": meta["namespace"],
            "alb_hostname": live_alb_hostname(meta, context)}
