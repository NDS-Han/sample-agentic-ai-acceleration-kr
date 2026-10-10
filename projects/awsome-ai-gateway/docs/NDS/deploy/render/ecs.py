# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""ecs backend 렌더러 — gateway.yaml → terraform.tfvars + backend.hcl.

설계 원칙은 EKS 경로와 같다: gateway.yaml 이 tfvars **만** 렌더하고, 인프라
배선은 `environments/gateway-ecs` 의 .tf 가 소유한다 (dual-render 금지 —
같은 키를 두 렌더러가 쓰면 drift 재발).

산출물 (out_dir):
  terraform.tfvars  — environments/gateway-ecs 에 -var-file 로 전달
  backend.hcl       — terraform init -backend-config 용 (계정별 bucket/table)
"""
from __future__ import annotations

import json
from pathlib import Path

from ..schema import GatewayConfig
from ..tiers import preset
from . import common


# tier 의 compute_spec → service_sizing map. cpu/memory 는 서비스 성격으로
# 고정(I/O bound — SSE 스트림 수용량은 메모리·커넥션 풀이 결정), 개수만 티어가 바꾼다.
def service_sizing(cfg: GatewayConfig) -> dict:
    spec = preset(cfg.deploy.size_tier).compute_spec
    n = int(spec.get("tasks_per_service", 1))
    gw = int(spec.get("gateway_tasks", n))
    sizing = {
        # gateway-proxy 만 SSE 장시간 커넥션 — 1vCPU/2GB + 여유 task 수
        "gateway-proxy":        {"cpu": 1024, "memory": 2048, "desired_count": gw},
        "admin-api":            {"cpu": 512,  "memory": 1024, "desired_count": n},
        "admin-ui":             {"cpu": 512,  "memory": 1024, "desired_count": n},
        "scheduler":            {"cpu": 256,  "memory": 512,  "desired_count": 1},
        # 스트림 소비자는 1개만 (COST_STREAM_CONSUMER 고정 — 늘리면 consumer name 충돌)
        "cost-recorder-worker": {"cpu": 256,  "memory": 512,  "desired_count": 1},
        "notification-worker":  {"cpu": 256,  "memory": 512,  "desired_count": 1},
        # run-task 로만 실행 — desired_count 는 쓰이지 않지만 스키마상 필요
        "migration":            {"cpu": 256,  "memory": 512,  "desired_count": 0},
    }
    # deploy.sizing 으로 서비스별 override 가능 (예: {"gateway-proxy": {"cpu": 2048}})
    for name, ov in (cfg.deploy.sizing or {}).items():
        if name in sizing and isinstance(ov, dict):
            sizing[name].update(ov)
    return sizing


def _tf_value(v) -> str:
    """python 값 → HCL 리터럴."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_tf_value(x) for x in v) + "]"
    if isinstance(v, dict):
        inner = ", ".join(f"{k} = {_tf_value(x)}" for k, x in v.items())
        return "{ " + inner + " }"
    raise TypeError(f"tfvars 변환 미지원 타입: {type(v)}")


def desired_tfvars(cfg: GatewayConfig, extra_vars: dict | None = None) -> dict:
    """gateway.yaml + tier preset → tfvars 값 map (결정적 부분).

    extra_vars: apply 후처리가 알아낸 동적 값 (domain=none 시 ALB DNS 기반
    NEXTAUTH_URL 등). tfvars 파일에 기록되므로 doctor 의 plan drift 에 걸리지 않는다."""
    t = preset(cfg.deploy.size_tier)
    n = cfg.features.notifications

    vars_: dict = {
        "project": "llm-gateway",
        "environment": cfg.env,
        "aws_region": cfg.aws.region,
        "image_tag": cfg.images.tag,
        "service_sizing": service_sizing(cfg),

        # 네트워크 — private 전환의 선행조건(VPN 경로)은 스키마가 아니라
        # docs/NDS 의 전환 런북이 강제한다
        "alb_internal": cfg.network.mode == "private",
        "allowed_cidrs": cfg.network.allowed_cidrs,
    }

    # ── DB/캐시 토폴로지 — tier spec → 모듈의 명시 모드 변수
    if t.db_mode == "rds-serverless":
        vars_["db_mode"] = "serverless"
        vars_["serverless_min_acu"] = t.db_spec.get("min_acu", 0.5)
        vars_["serverless_max_acu"] = t.db_spec.get("max_acu", 4.0)
    elif t.db_mode == "rds-provisioned":
        vars_["db_mode"] = "provisioned"
        vars_["db_instance_class"] = t.db_spec.get("instance_class", "db.r7g.large")
    vars_["db_safeguards"] = t.ha != "none"

    if t.cache_mode == "elasticache-single":
        vars_["cache_mode"] = "single"
    elif t.cache_mode == "elasticache-ha":
        vars_["cache_mode"] = "cluster" if t.cache_spec.get("cluster_mode") else "replicated"
    vars_["cache_node_type"] = t.cache_spec.get("node_type", "cache.t4g.small")
    vars_["nat_ha"] = t.ha == "multi-az"

    # ── 도메인 — cloudfront-temp 는 EKS 경로의 개념이라 ecs 에선 none 으로 수렴
    if cfg.domain.mode == "route53-acm":
        vars_["domain_name"] = cfg.domain.name
        vars_["hosted_zone_id"] = cfg.domain.zone_id
    elif cfg.domain.mode == "none" or cfg.domain.mode == "cloudfront-temp":
        vars_["domain_name"] = ""

    # ── 이미지 — 외부 registry 지정 시 ECR repo 생성 생략.
    # images.registry 의 정본 의미는 레지스트리 "호스트" (eks chart 의
    # global.imageRegistry 와 동일 — 차트가 뒤에 llm-gateway/<svc> 를 붙인다).
    # ecs 모듈은 image_registry/<svc> 로 조립하므로 bare host 이면 프로젝트
    # prefix 를 붙여 repo(host/llm-gateway/<svc>)와 일치시킨다. path 가 이미
    # 붙어 있으면 사용자가 repo prefix 까지 지정한 것으로 보고 그대로 둔다.
    if cfg.images.registry:
        reg = cfg.images.registry.rstrip("/")
        vars_["image_registry"] = reg if "/" in reg else f"{reg}/llm-gateway"

    # ── notifications
    vars_["email_sender_type"] = n.provider
    if n.sender_name:
        vars_["email_sender_name"] = n.sender_name
    if n.provider == "ses":
        vars_["email_sender_address"] = n.ses_from
        vars_["aws_ses_region"] = cfg.aws.region
    elif n.provider == "smtp":
        vars_["email_sender_address"] = n.smtp_from or "noreply@llm-gateway.local"
        vars_["smtp_host"] = n.smtp_host
        vars_["smtp_port"] = n.smtp_port

    # ── 기능 플래그
    vars_["enable_body_logging"] = cfg.features.body_logging

    # ── OIDC — ecs 경로는 Cognito 를 만들고 그 출력이 OIDC 소스가 된다.
    #    required_group 만 tfvars 로 간다 (나머지 필드는 module output).
    if cfg.oidc.required_group:
        vars_["oidc_required_group"] = cfg.oidc.required_group

    # ── 모델 프로필 — global 기본 (리전 중립). regional 은 배포 리전 프리픽스.
    if cfg.clients.models_profile == "global":
        vars_["bedrock_allowed_model_arns"] = [
            f"arn:aws:bedrock:{cfg.aws.region}::foundation-model/anthropic.*",
            "arn:aws:bedrock:::foundation-model/anthropic.*",
            "arn:aws:bedrock:*::inference-profile/global.anthropic.*",
            "arn:aws:bedrock:*:*:inference-profile/global.anthropic.*",
        ]
    else:
        vars_["bedrock_allowed_model_arns"] = [
            f"arn:aws:bedrock:{cfg.aws.region}::foundation-model/anthropic.*",
            f"arn:aws:bedrock:{cfg.aws.region}::inference-profile/*",
        ]

    vars_.update(extra_vars or {})
    return vars_


def render_tfvars(cfg: GatewayConfig, extra_vars: dict | None = None) -> str:
    lines = [
        "# generated by deploy — 직접 수정하지 말 것 (gateway.yaml 을 고치고 render)",
        f"# source: gateway.yaml env={cfg.env} tier={cfg.deploy.size_tier}",
        "",
    ]
    for k, v in desired_tfvars(cfg, extra_vars).items():
        if isinstance(v, dict) and k == "service_sizing":
            lines.append("service_sizing = {")
            for svc, s in v.items():
                lines.append(
                    f'  {svc:<22} = {{ cpu = {s["cpu"]}, memory = {s["memory"]}, desired_count = {s["desired_count"]} }}')
            lines.append("}")
            lines.append("")
            continue
        lines.append(f"{k} = {_tf_value(v)}")
    return "\n".join(lines) + "\n"


def render_backend_hcl(cfg: GatewayConfig) -> str:
    """terraform init -backend-config 용. 값이 없으면 placeholder + 안내."""
    bucket = cfg.deploy.tfstate_bucket
    table = cfg.deploy.tfstate_table
    note = ""
    if not bucket:
        if cfg.aws.account_id:
            bucket = f"llm-gateway-tfstate-{cfg.aws.account_id}"
            note = ("# account_id 규칙으로 추론한 이름 — 실제 bucket 이 다르면 "
                    "deploy.tfstate_bucket 을 gateway.yaml 에 명시하십시오.\n")
        else:
            bucket = "YOUR_TFSTATE_BUCKET"
            table = table or "YOUR_TFLOCK_TABLE"
            note = ("# ⚠️ placeholder — deploy.tfstate_bucket / deploy.tfstate_table 을 "
                    "gateway.yaml 에 채운 뒤 다시 render 하십시오.\n")
    return note + (
        f'bucket         = "{bucket}"\n'
        f'key            = "ecs/{cfg.env}/terraform.tfstate"\n'
        f'region         = "{cfg.aws.region}"\n'
        f'encrypt        = true\n'
        + (f'dynamodb_table = "{table}"\n' if table else "")
    )


def feature_notes(cfg: GatewayConfig) -> list[str]:
    """ecs backend 의 기능 플래그 실효성 — 침묵 드롭 금지 (compose 와 같은 규약)."""
    notes: list[str] = []
    if cfg.features.web_search:
        notes.append(
            "web_search: terraform 의 agentcore_gateway_url 변수가 필요합니다 — AgentCore "
            "Gateway(us-east-1 구조적 제약)를 먼저 만들고 URL 을 tfvars/변수로 넣으십시오. "
            "routing_profiles.web_search_enabled DB 행도 별도로 필요합니다.")
    if cfg.features.pricing_lambda:
        notes.append(
            "pricing_lambda: LiteLLM pricing Lambda 배포는 이 모듈 범위 밖입니다 — "
            "별도 배포 후 LITELLM_PRICING_LAMBDA 를 env 로 주입해야 합니다.")
    if cfg.features.bi_insight:
        notes.append(
            "bi_insight: agentcore_runtime 모듈 연결은 미구현입니다 — env main.tf 에 "
            "module.agentcore_runtime 추가가 필요합니다 (EKS env 의 배선 참조).")
    if cfg.domain.mode == "cloudfront-temp":
        notes.append(
            "domain.mode=cloudfront-temp: ecs 경로에 CloudFront 모듈이 없어 domain=none "
            "으로 렌더했습니다 — 임시 HTTPS 는 route53-acm 또는 ALB DNS+HTTP 를 쓰십시오.")
    if cfg.oidc.enabled:
        notes.append(
            "oidc: ecs 경로는 Cognito user pool 을 생성하고 그 출력(issuer/client_id/hosted UI)을 "
            "OIDC 소스로 씁니다 — gateway.yaml 의 issuer_url/client_id 값은 무시됩니다. "
            "외부 IdP 연동은 아직 미지원입니다.")
    return notes


def render(cfg: GatewayConfig, out_dir: Path, extra_vars: dict | None = None) -> dict:
    """산출물을 out_dir 에 생성."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tfvars = out_dir / "terraform.tfvars"
    backend = out_dir / "backend.hcl"
    tfvars.write_text(render_tfvars(cfg, extra_vars))
    backend.write_text(render_backend_hcl(cfg))
    return {
        "files": [tfvars, backend],
        "notes": feature_notes(cfg),
        "env_dir": "deployment/terraform/environments/gateway-ecs",
    }
