# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""eks backend 렌더러 — gateway.yaml → helm values 오버레이 + deploy 메타.

설계: 기존 배포(install-eks.sh + values-eks-fargate-<env>.yaml)를 **이어받는다**.
차트의 기본 values 와 env overlay 는 그대로 쓰고, 이 렌더러는 gateway.yaml 이
소유하는 키만 담은 마지막 오버레이를 만든다 — helm 의 -f 는 나중 파일이 이기므로
  base values.yaml → values-eks-fargate-<env>.yaml → gen/<env>/eks/values.yaml
순으로 쌓인다. generate, don't mutate — env values 파일 자체는 건드리지 않는다.

산출물 (out_dir):
  values.yaml  — helm -f 로 전달할 오버레이 (관리 키만)
  deploy.yaml  — apply/doctor 메타 (release/namespace/env-dir/레이어 순서)
"""
from __future__ import annotations

import yaml
from pathlib import Path

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
    """기존 배포가 쓰던 env overlay 를 찾는다 — 없으면 None (차트 기본값만)."""
    for cand in (
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
        },
        "migration": {"enabled": True},
    }
    if cfg.images.registry:
        v["global"]["imageRegistry"] = cfg.images.registry
    if cfg.images.tag:
        for svc in TAGGED_SERVICES:
            v.setdefault(svc, {})["image"] = {"tag": cfg.images.tag}

    # ── ingress — 도구는 CIDR 과 TLS 게이트를 소유. hosts 는 domain.name 이
    #    명시된 경우에만 우리 네이밍으로 쓴다(기존 gateway-dev.* 네이밍과 다를
    #    수 있어 침묵 변경 금지 — notes 에 경고를 남긴다)
    ann: dict = {}
    if cfg.network.allowed_cidrs:
        ann["alb.ingress.kubernetes.io/inbound-cidrs"] = ",".join(cfg.network.allowed_cidrs)
    if cfg.network.mode == "private":
        ann["alb.ingress.kubernetes.io/scheme"] = "internal"
    if ann or cfg.domain.name:
        v["ingress"] = {"enabled": True, "className": "alb", "annotations": ann}
    if cfg.domain.name:
        # TLS 는 ALB 의 certificate-arn 어노테이션이 담당 — tls.secretName 은
        # 비워두면 빈 문자열 블록이 생기므로 여기선 host 만 관리한다
        for key, host in (("gateway", f"gateway.{cfg.domain.name}"),
                          ("adminUi", f"admin.{cfg.domain.name}"),
                          ("adminApi", f"admin-api.{cfg.domain.name}")):
            v["ingress"][key] = {"host": host, "path": "/", "pathType": "Prefix"}

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
    email: dict = {"provider": n.provider if n.provider in ("ses", "internal_api") else
                   ("internal_api" if n.provider == "smtp" else n.provider)}
    if n.provider == "ses":
        email["ses"] = {"region": cfg.aws.region, "fromAddress": n.ses_from,
                        "fromName": n.sender_name or "LLM Gateway"}
    elif n.provider == "smtp":
        email["internalApi"] = {"url": "", "fromAddress": n.smtp_from,
                                "fromName": n.sender_name or "LLM Gateway"}
    v["notificationWorker"] = dict(v.get("notificationWorker", {}), email=email)

    # ── OIDC — adminApi/gatewayProxy/adminUi 세 곳에 동일 계약 ───────────────
    if cfg.oidc.enabled:
        claim = "cognito:groups" if "cognito" in cfg.oidc.provider_name else "groups"
        o = {"enabled": True, "issuerUrl": cfg.oidc.issuer_url,
             "providerName": cfg.oidc.provider_name, "groupsClaim": claim,
             "requiredGroup": cfg.oidc.required_group}
        v.setdefault("adminApi", {})["oidc"] = dict(o)
        v.setdefault("gatewayProxy", {})["oidc"] = dict(o)
        ui_env = {"DEV_LOGIN_ENABLED": "false",
                  "OIDC_CLIENT_ID": cfg.oidc.client_id}
        if cfg.oidc.authorize_url:
            ui_env["OIDC_AUTHORIZE_URL"] = cfg.oidc.authorize_url
        if cfg.oidc.token_url:
            ui_env["OIDC_TOKEN_URL"] = cfg.oidc.token_url
        v.setdefault("adminUi", {}).setdefault("env", {}).update(ui_env)
    else:
        v.setdefault("adminUi", {}).setdefault("env", {})["DEV_LOGIN_ENABLED"] = "true"

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
        notes.append("images.tag 가 서비스 전체에 적용됩니다 — 기존 배포가 서비스별 "
                     "태그를 달리 쓰고 있었다면 첫 apply 에서 전부 이 태그로 통일됩니다.")
    if cfg.domain.name:
        notes.append(f"ingress host 를 gateway./admin./admin-api.{cfg.domain.name} "
                     "네이밍으로 씁니다 — 기존 host(gateway-dev.* 등)와 다르면 "
                     "URL 이 바뀌므로 클라이언트 재안내가 필요합니다.")
    if not cfg.domain.name and cfg.domain.mode != "none":
        notes.append("domain.name 이 없어 ingress host 는 기존 values 를 유지합니다.")
    if cfg.features.notifications.provider == "smtp":
        notes.append("notifications.provider=smtp → chart 의 internal_api 로 매핑했습니다"
                     " — internalApi.url 은 env values 파일에서 관리하세요.")
    if cfg.features.body_logging:
        notes.append("body_logging: FIREHOSE_STREAM_NAME/BODY_LOG_S3_BUCKET 은 "
                     "terraform output 에서 apply 시 주입됩니다.")
    if cfg.features.web_search:
        notes.append("web_search: AGENTCORE_GATEWAY_URL 은 기존 env values 에 있어야 "
                     "합니다 — 없으면 플래그만 켜지고 라우터가 건너뜁니다.")
    return notes


def render(cfg: GatewayConfig, out_dir: Path, repo_root: Path = REPO_ROOT) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    env_vals = env_values_file(cfg, repo_root)

    values_path = out_dir / "values.yaml"
    values_path.write_text(
        "# generated by deploy — gateway.yaml 이 소유하는 키만. 직접 수정하지 말 것.\n"
        "# 레이어 순서: chart values.yaml → env overlay → 이 파일(마지막이 이긴다)\n"
        + yaml.safe_dump(desired_values(cfg), sort_keys=False, allow_unicode=True))

    meta = deploy_meta(cfg, env_vals, repo_root)
    meta["values_layers"].append(str(values_path))
    deploy_path = out_dir / "deploy.yaml"
    deploy_path.write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True))

    notes = feature_notes(cfg)
    if not env_vals:
        notes.append(f"env overlay(values-eks-fargate-{cfg.env}.yaml)를 못 찾았습니다 — "
                     "차트 기본값 + 이 오버레이만으로 배포됩니다. 기존 배포를 업데이트하는 "
                     "거라면 env 값이 빠져 의도와 달라질 수 있습니다. 특히 시크릿은 "
                     "이 오버레이가 공급하지 않습니다 — chart 는 externalSecrets.enabled(ESO) "
                     "또는 .Values.secrets 중 하나가 반드시 필요하며, 둘 다 없으면 "
                     "migration Job과 전 pod가 Secret 참조 실패로 뜨지 못합니다.")
    return {"files": [values_path, deploy_path], "notes": notes,
            "env_dir": meta["env_dir"]}
