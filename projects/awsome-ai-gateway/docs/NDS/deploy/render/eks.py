# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""eks backend 렌더러 — gateway.yaml → helm values 오버레이 + deploy 메타.

설계: 기존 배포(install-eks.sh + values-eks-fargate-<env>.yaml)를 **이어받는다**.
차트의 기본 values 와 env overlay 는 그대로 쓰고, 이 렌더러는 gateway.yaml 이
소유하는 키만 담은 마지막 오버레이를 만든다 — helm 의 -f 는 나중 파일이 이기므로
  base values.yaml → values-eks-fargate-<env>[.local].yaml → gen/<env>/eks/values.yaml
순으로 쌓인다. *.local.yaml(git 미추적, 머신별 라이브 값)이 있으면 우선 —
추적된 템플릿을 배포값으로 더럽히지 않아 브랜치 전환을 막지 않는다.
generate, don't mutate — env values 파일 자체는 건드리지 않는다.

산출물 (out_dir):
  values.yaml  — helm -f 로 전달할 오버레이 (관리 키만)
  deploy.yaml  — apply/doctor 메타 (release/namespace/env-dir/레이어 순서)
"""
from __future__ import annotations

import json
import re

import yaml
from pathlib import Path

from .. import schema
from ..schema import GatewayConfig

REPO_ROOT = Path(__file__).resolve().parents[4]
CHART_DIR = Path("deployment/charts/llm-gateway")

# 이 도구가 태그를 소유하는 서비스 — 단일 images.tag 로 통일한다.
# (기존 eks 배포는 서비스별 태그가 다른 경우가 있음 — capture notes 참조)
TAGGED_SERVICES = (
    "gatewayProxy", "adminApi", "adminUi", "scheduler",
    "notificationWorker", "costRecorderWorker",
)


def env_values_file(cfg: GatewayConfig, repo_root: Path = REPO_ROOT) -> Path | None:
    """기존 배포가 쓰던 env overlay 를 찾는다 — 없으면 None (차트 기본값만).

    우선순위: *.local.yaml(머신별 라이브 값, git 미추적 — 브랜치 전환을 막지 않음)
    → 추적된 env 파일(템플릿). 라이브 계정·엔드포인트·태그 같은 실측값은
    반드시 .local.yaml 쪽에 둔다.
    """
    for cand in (
        repo_root / CHART_DIR / f"values-eks-fargate-{cfg.env}.local.yaml",
        repo_root / CHART_DIR / f"values-eks-{cfg.env}.local.yaml",
        repo_root / CHART_DIR / f"values-eks-fargate-{cfg.env}.yaml",
        repo_root / CHART_DIR / f"values-eks-{cfg.env}.yaml",
    ):
        if cand.exists():
            return cand
    return None


def desired_values(cfg: GatewayConfig) -> dict:
    """gateway.yaml → helm 오버레이(관리 키만). 동적 값(IRSA·엔드포인트)은
    apply 가 terraform output 에서 --set 으로 주입 — 여기엔 넣지 않는다."""
    v: dict = {
        "global": {
            "environment": cfg.env,
            "deploymentMode": "eks-fargate",
        },
        "aws": {
            "region": cfg.aws.region,
            "allowedStsRegions": [cfg.aws.region],
            # VK 발급 허용 계정 — role 이름 검사는 외부 계정의 동명 role/IAM user 로
            # 우회되므로 계정 경계가 필요. gateway.yaml 의 aws.account_id 사용.
            **({"allowedAwsAccounts": [cfg.aws.account_id]} if cfg.aws.account_id else {}),
        },
        "migration": {"enabled": True},
    }
    if cfg.images.registry:
        v["global"]["imageRegistry"] = cfg.images.registry
    if cfg.images.tag:
        for svc in TAGGED_SERVICES:
            v.setdefault(svc, {})["image"] = {"tag": cfg.images.tag}
    # 서비스별 핀 — US-19 같은 멀티태그 릴리스. tag 기본값 위에 개별 키를 덮고
    # migration(TAGGED_SERVICES 에 없음)도 여기서만 핀할 수 있다.
    for svc, tag in (cfg.images.tags or {}).items():
        v.setdefault(svc, {})["image"] = {"tag": tag}

    # ── ingress — 도구는 CIDR 과 TLS 게이트를 소유. hosts 는 domain.name 이
    #    명시된 경우에만 우리 네이밍으로 쓴다(기존 gateway-dev.* 네이밍과 다를
    #    수 있어 침묵 변경 금지 — notes 에 경고를 남긴다)
    ann: dict = {}
    if cfg.network.allowed_cidrs:
        ann["alb.ingress.kubernetes.io/inbound-cidrs"] = ",".join(cfg.network.allowed_cidrs)
    if cfg.network.mode == "private":
        # ⚠️ 공통 annotations — 3개 Ingress 전부 internal 로 내린다(gateway 포함).
        # gateway public + admin internal 조합은 아래 admin_internal 이 담당.
        ann["alb.ingress.kubernetes.io/scheme"] = "internal"
    if ann or cfg.domain.name or cfg.network.admin_internal:
        v["ingress"] = {"enabled": True, "className": "alb", "annotations": ann}
    if cfg.network.admin_internal:
        # ops 8-I — admin 콘솔만 internal ALB (S2S VPN 전제). per-ingress
        # 어노테이션은 차트의 전용 맵이며 공통값을 덮어쓴다.
        for key in ("adminUi", "adminApi"):
            v["ingress"].setdefault(key, {}).setdefault("annotations", {})[
                "alb.ingress.kubernetes.io/scheme"] = "internal"
    if cfg.domain.name:
        # TLS 는 ALB 의 certificate-arn 어노테이션이 담당 — tls.secretName 은
        # 비워두면 빈 문자열 블록이 생기므로 여기선 host 만 관리한다
        for key, host in (("gateway", f"gateway.{cfg.domain.name}"),
                          ("adminUi", f"admin.{cfg.domain.name}"),
                          ("adminApi", f"admin-api.{cfg.domain.name}")):
            v["ingress"][key] = {"host": host, "path": "/", "pathType": "Prefix"}
        # domain 도입 시 NEXTAUTH_URL 도 새 주소로 — 비워두면 이전 값(ALB DNS
        # 등)이 남아 Cognito callback allowlist 와 어긋나 SSO 가 깨진다
        v.setdefault("adminUi", {})["nextauthUrl"] = f"https://admin.{cfg.domain.name}"

    # ── features → gatewayProxy.env / notificationWorker.email ──────────────
    genv: dict = {}
    if cfg.features.web_search:
        genv["WEB_SEARCH_ENABLED"] = "true"
    else:
        genv["WEB_SEARCH_ENABLED"] = "false"
    if genv:
        v.setdefault("gatewayProxy", {})["env"] = genv
    # body_logging 의 FIREHOSE_STREAM_NAME/BODY_LOG_S3_BUCKET 는 terraform
    # output 값이라 apply 에서 --set — 플래그만 여기서 기록하지 않는다

    n = cfg.features.notifications
    # 차트가 provider 별 블록을 네이티브 지원 — smtp 를 internal_api 로 매핑하면
    # EMAIL_API_URL="" 로 crashloop 되므로 각각 자기 블록으로 보낸다.
    email: dict = {"provider": n.provider}
    if n.provider == "ses":
        email["ses"] = {"region": cfg.aws.region, "fromAddress": n.ses_from,
                        "fromName": n.sender_name or "LLM Gateway"}
    elif n.provider == "smtp":
        email["smtp"] = {"host": n.smtp_host, "port": n.smtp_port,
                         "fromAddress": n.smtp_from,
                         **({"credentialsSecretName": n.smtp_credentials_secret}
                            if n.smtp_credentials_secret else {})}
    elif n.provider == "internal_api":
        email["internalApi"] = {"url": n.internal_api_url,
                                "fromAddress": n.internal_api_from
                                or "no-reply@llm-gateway.local",
                                "fromName": n.sender_name or "LLM Gateway"}
    v["notificationWorker"] = dict(v.get("notificationWorker", {}), email=email)

    # ── OIDC — adminApi/gatewayProxy/adminUi 세 곳에 동일 계약 ───────────────
    if cfg.oidc.enabled:
        claim = "cognito:groups" if "cognito" in cfg.oidc.provider_name else "groups"
        # ⚠️ audience 미설정이면 verifier 가 aud 검증을 끈다 — 같은 user pool 의
        #    다른 app client 용 id_token 도 issuer 만 맞으면 통과한다. Cognito 의
        #    aud 는 app client_id 라 명시가 없으면 client_id 로 채운다.
        o = {"enabled": True, "issuerUrl": cfg.oidc.issuer_url,
             "audience": cfg.oidc.audience or cfg.oidc.client_id,
             "providerName": cfg.oidc.provider_name, "groupsClaim": claim,
             "requiredGroup": cfg.oidc.required_group}
        v.setdefault("adminApi", {})["oidc"] = dict(o)
        v.setdefault("gatewayProxy", {})["oidc"] = dict(o)
        ui_env = {"DEV_LOGIN_ENABLED": "true" if cfg.features.dev_login else "false",
                  "OIDC_CLIENT_ID": cfg.oidc.client_id}
        if cfg.oidc.authorize_url:
            ui_env["OIDC_AUTHORIZE_URL"] = cfg.oidc.authorize_url
        if cfg.oidc.token_url:
            ui_env["OIDC_TOKEN_URL"] = cfg.oidc.token_url
        v.setdefault("adminUi", {}).setdefault("env", {}).update(ui_env)
    else:
        v.setdefault("adminUi", {}).setdefault("env", {})["DEV_LOGIN_ENABLED"] = "true"

    # dev login 은 admin-api 가 dev. 토큰을 받아야 완결된다 — adminUi 만 켜면
    # UI 로그인은 되는데 API 호출이 401 로 떨어진다. 명시적 opt-in 일 때만 양쪽 배선.
    if cfg.features.dev_login:
        v.setdefault("adminApi", {}).setdefault("env", {})["DEV_LOGIN_ENABLED"] = "true"

    # VK TTL — adminApi.env:OIDC_VK_TTL_HOURS 로 넣으면 oidcEnv 뒤에 같은 이름의
    # env 가 2개 생기는 함정이 있다(ops 8-Z) — 정식 키 adminApi.oidc.vkTtlHours.
    if cfg.oidc.vk_ttl_hours:
        v.setdefault("adminApi", {}).setdefault("oidc", {})[
            "vkTtlHours"] = cfg.oidc.vk_ttl_hours

    return v


def deploy_meta(cfg: GatewayConfig, env_values: Path | None,
                repo_root: Path = REPO_ROOT) -> dict:
    env_dir = cfg.deploy.tf_env_dir or \
        f"deployment/terraform/environments/llm-gateway-{cfg.env}"
    layers = []
    if env_values:
        layers.append(str(env_values.relative_to(repo_root)))
    return {
        "release": cfg.deploy.release,
        "namespace": cfg.deploy.namespace,
        "chart": str(CHART_DIR),
        "env_dir": env_dir,
        # values.yaml 은 apply 가 out_dir 에서 찾아 마지막 레이어로 붙인다
        "values_layers": layers,
    }


def feature_notes(cfg: GatewayConfig) -> list[str]:
    notes: list[str] = []
    if cfg.images.tag:
        if cfg.images.tags:
            uncovered = sorted(set(TAGGED_SERVICES) - set(cfg.images.tags))
            if uncovered:
                notes.append(
                    f"images.tags 에 없는 {uncovered} 는 images.tag"
                    f"({cfg.images.tag})를 따릅니다 — 기존 태그와 다르면 바뀝니다.")
        else:
            notes.append("images.tag 가 서비스 전체에 적용됩니다 — 기존 배포가 서비스별 "
                         "태그를 달리 쓰고 있었다면 첫 apply 에서 전부 이 태그로 통일됩니다.")
    if cfg.domain.name:
        notes.append(f"ingress host 를 gateway./admin./admin-api.{cfg.domain.name} "
                     "네이밍으로 씁니다 — 기존 host(gateway-dev.* 등)와 다르면 "
                     "URL 이 바뀌므로 클라이언트 재안내가 필요합니다.")
    if not cfg.domain.name and cfg.domain.mode != "none":
        notes.append("domain.name 이 없어 ingress host 는 기존 values 를 유지합니다.")
    if cfg.features.notifications.provider == "internal_api":
        notes.append("notifications.provider=internal_api — internalApi.url 에 "
                     f"{cfg.features.notifications.internal_api_url} 를 렌더했습니다.")
    if cfg.network.admin_internal:
        notes.append("admin_internal: admin-ui/admin-api Ingress 만 scheme=internal 로 "
                     "내립니다 — S2S VPN 등 관리자망→VPC 경로가 선행돼야 합니다.")
    if cfg.features.web_search:
        notes.append("web_search: apply 시 AgentCore Gateway(us-east-1)를 멱등 "
                     "프로비저닝하고 AGENTCORE_GATEWAY_URL 을 --set 합니다 — "
                     "boto3 와 us-east-1 agentcore/IAM 권한이 필요합니다.")
    if cfg.oidc.enabled and cfg.oidc.client_secret:
        # 차트 adminUi env 범위는 value: 만 렌더하고 secretKeyRef 를 못 만든다 —
        # confidential client 는 템플릿 추가가 필요 (chart values.yaml 주석 §OIDC 참조)
        notes.append("oidc.client_secret: eks 경로에서는 렌더되지 않습니다 — 차트 권장은 "
                     "secret 없는 public client + PKCE 입니다. confidential client 가 "
                     "필요하면 env values 의 시크릿 경로로 주입하세요.")
    return notes


def env_dir_path(cfg: GatewayConfig, repo_root: Path = REPO_ROOT) -> Path:
    return repo_root / (cfg.deploy.tf_env_dir
                        or f"deployment/terraform/environments/llm-gateway-{cfg.env}")


def _env_vpc_cidr(env_dir: Path | None) -> str:
    """env 의 terraform.tfvars 에 선언된 vpc_cidr — 없으면 env 기본값."""
    if env_dir:
        tfvars = env_dir / "terraform.tfvars"
        if tfvars.exists():
            m = re.search(r'vpc_cidr\s*=\s*"([^"]+)"', tfvars.read_text())
            if m:
                return m.group(1)
    return "10.30.0.0/16"   # llm-gateway-* env variables.tf 기본값


def render_tfvars(cfg: GatewayConfig, env_dir: Path | None = None) -> str:
    """eks env(terraform) 의 managed 오버레이 — gateway.yaml 이 소유하는 인프라
    키만. eks env tfvars 는 사람이 소유하므로 이 파일을 -var-file 로 뒤에
    붙인다 (나중 -var-file 가 이긴다)."""
    lines = [
        "# generated by deploy — gateway.yaml 이 소유하는 terraform 키만.",
        "# 적용: terraform -var-file=<이 파일> apply (env 디렉토리에서)",
        f"enable_body_logging = {'true' if cfg.features.body_logging else 'false'}",
        "enable_notification_ses = "
        f"{'true' if cfg.features.notifications.provider == 'ses' else 'false'}",
    ]
    inf = cfg.infra
    if inf.vpc_cidr:
        lines.append(f'vpc_cidr = "{inf.vpc_cidr}"')
    if inf.azs:
        lines.append(f"azs = {json.dumps(inf.azs)}")
    if inf.vpc_cidr or inf.azs:
        # azs 개수와 서브넷 cidr 리스트 길이는 같아야 한다 — 선언되면 둘 다 생성
        for k, v in schema.subnet_cidrs(
                inf.vpc_cidr or _env_vpc_cidr(env_dir), len(inf.azs) or 2).items():
            lines.append(f"{k} = {json.dumps(v)}")
    if inf.cognito_domain_suffix:
        lines.append(f'cognito_domain_suffix = "{inf.cognito_domain_suffix}"')
    if inf.cognito_groups:
        groups = list(dict.fromkeys([*inf.cognito_groups, "ClaudeAdmin"]))
        lines.append(f"cognito_groups = {json.dumps(groups)}")
    if inf.bedrock_allowed_model_arns:
        lines.append(
            f"bedrock_allowed_model_arns = "
            f"{json.dumps(inf.bedrock_allowed_model_arns, indent=2)}")
    return "\n".join(lines) + "\n"


def render(cfg: GatewayConfig, out_dir: Path, repo_root: Path = REPO_ROOT) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    env_vals = env_values_file(cfg, repo_root)

    values_path = out_dir / "values.yaml"
    values_path.write_text(
        "# generated by deploy — gateway.yaml 이 소유하는 키만. 직접 수정하지 말 것.\n"
        "# 레이어 순서: chart values.yaml → env overlay → 이 파일(마지막이 이긴다)\n"
        + yaml.safe_dump(desired_values(cfg), sort_keys=False, allow_unicode=True))

    tfvars_path = out_dir / "terraform.tfvars"
    tfvars_path.write_text(render_tfvars(cfg, env_dir=env_dir_path(cfg, repo_root)))

    meta = deploy_meta(cfg, env_vals, repo_root)
    meta["values_layers"].append(str(values_path))
    deploy_path = out_dir / "deploy.yaml"
    deploy_path.write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True))

    notes = feature_notes(cfg)
    if cfg.features.body_logging:
        notes.append(
            "body_logging: 인프라(Firehose/S3/IRSA)는 terraform 소유입니다 — "
            "`deploy apply` 가 output 누락을 감지하면 "
            f"`terraform apply -var-file={tfvars_path} -target=module.body_logging "
            "-target=module.irsa` 를 자동 실행하고 output 을 pod env 로 주입합니다.")
    if cfg.features.bi_insight:
        notes.append(
            "bi_insight: NDS 배포 대상이 아닙니다 — enable_chat_agent 는 env "
            "tfvars(사람 소유)로 켭니다 (scaffold 는 false 로 생성). "
            "ops/NDS-02-bi-insight.md 절차를 따르십시오.")
    if not env_vals:
        notes.append(f"env overlay(values-eks-fargate-{cfg.env}.yaml)를 못 찾았습니다 — "
                     "차트 기본값 + 이 오버레이만으로 배포됩니다. 기존 배포를 업데이트하는 "
                     "거라면 env 값이 빠져 의도와 달라질 수 있습니다. 특히 시크릿은 "
                     "이 오버레이가 공급하지 않습니다 — chart 는 externalSecrets.enabled(ESO) "
                     "또는 .Values.secrets 중 하나가 반드시 필요하며, 둘 다 없으면 "
                     "migration Job과 전 pod가 Secret 참조 실패로 뜨지 못합니다.")
    return {"files": [values_path, deploy_path, tfvars_path], "notes": notes,
            "env_dir": meta["env_dir"]}
