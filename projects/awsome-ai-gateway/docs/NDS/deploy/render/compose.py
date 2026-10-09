# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""compose backend 렌더러.

베이스 `docker-compose.yml`(로컬 개발용, 관측스택·mock 포함)을 읽어서
배포용 산출물을 *생성*한다 — 원본은 건드리지 않는다(generate, don't mutate).

산출물 (out_dir):
  docker-compose.yml   — 소규모 배포용 서비스 집합
  .env                 — 머지 방식(기존 시크릿 보존)
  Caddyfile            — 도메인/포트 라우팅 + IP allowlist
"""
from __future__ import annotations

import re
import yaml
from pathlib import Path

from ..schema import GatewayConfig
from . import common

REPO_ROOT = Path(__file__).resolve().parents[4]  # docs/NDS/deploy/render/compose.py → 루트

DROP_ALWAYS = {"mock-vllm"}
OBSERVABILITY = {"otel-collector", "prometheus", "loki", "tempo", "grafana"}
# 호스트에 포트를 열지 않는 서비스(Caddy 가 유일한 인입점)
APP_SERVICES = {
    "gateway-proxy", "admin-api", "admin-ui",
    "scheduler", "cost-recorder-worker", "notification-worker",
    "migration",
}
# images.registry/tag 가 있으면 build 대신 가져올 이미지 이름
IMAGE_NAMES = {
    "gateway-proxy": "gateway-proxy",
    "admin-api": "admin-api",
    "admin-ui": "admin-ui",
    "scheduler": "admin-api",
    "cost-recorder-worker": "cost-recorder-worker",
    "notification-worker": "notification-worker",
    "migration": "admin-api",
}
OTEL_ENV_RE = re.compile(r"^OTEL_")

WEB_PORTS = {"gateway-proxy": 8000, "admin-api": 8080, "admin-ui": 3000}


def _drop_env_keys(env: dict, keys) -> None:
    if isinstance(env, dict):
        for k in keys:
            env.pop(k, None)


def transform_services(base: dict, cfg: GatewayConfig) -> dict:
    services = base.get("services", {})
    drop = set(DROP_ALWAYS) | (set() if cfg.features.observability else OBSERVABILITY)
    out: dict = {}

    for name, svc in services.items():
        if name in drop:
            continue
        svc = dict(svc)

        # ① 관측스택이 빠지면 OTEL exporter env 도 같이 제거 (죽은 엔드포인트)
        if not cfg.features.observability and isinstance(svc.get("environment"), dict):
            _drop_env_keys(svc["environment"], [k for k in svc["environment"] if OTEL_ENV_RE.match(k)])

        # ② gateway 는 mock-vllm 이 없으므로 OPENMODEL_BASE_URL 제거
        if name == "gateway-proxy" and isinstance(svc.get("environment"), dict):
            svc["environment"].pop("OPENMODEL_BASE_URL", None)

        # ③ 명시 이미지 태그가 있으면 build → image 로 교체(핀 배포)
        if cfg.images.tag and cfg.images.registry and name in IMAGE_NAMES:
            svc.pop("build", None)
            svc["image"] = f"{cfg.images.registry}/{IMAGE_NAMES[name]}:{cfg.images.tag}"

        # ④ 배포용 재시작 정책 통일
        svc.setdefault("restart", "unless-stopped")

        # ⑤ 호스트 포트 제거 — 인입점은 Caddy 뿐
        if name in APP_SERVICES or name in ("postgres", "redis"):
            svc.pop("ports", None)

        # ⑤-b admin-ui 는 env_file 을 안 읽으므로 OIDC/외부 URL 을 environment 에 직접 주입
        if name == "admin-ui" and cfg.oidc.enabled:
            svc.setdefault("environment", {}).update({
                "OIDC_CLIENT_ID": cfg.oidc.client_id,
                "OIDC_AUTHORIZE_URL": cfg.oidc.authorize_url,
                "OIDC_TOKEN_URL": cfg.oidc.token_url,
                # confidential client(Cognito secret) — .env 에 보관하고 interpolate 만
                "OIDC_CLIENT_SECRET": "${OIDC_CLIENT_SECRET:-}",
            })
        if name == "admin-ui" and cfg.domain.name:
            # 도메인이 없으면 NEXTAUTH_URL 을 비워 Host 헤더로 유도 — 원격 호스트에서
            # localhost 단정은 틀린 주소를 만든다
            svc.setdefault("environment", {})["NEXTAUTH_URL"] = f"https://admin.{cfg.domain.name}"

        # ⑥ env_file 은 생성 .env 를 가리키도록
        if "env_file" in svc:
            svc["env_file"] = [str(e) for e in
                               (svc["env_file"] if isinstance(svc["env_file"], list) else [svc["env_file"]])]

        # ⑦ 상대경로 bind mount·build context → 절대경로
        #   (생성 파일은 gen/<env>/ 에 있어 ./ 기준이 다름)
        if isinstance(svc.get("volumes"), list):
            svc["volumes"] = [_absolutize_mount(v) for v in svc["volumes"]]
        if isinstance(svc.get("build"), dict) and isinstance(svc["build"].get("context"), str):
            ctx = svc["build"]["context"]
            if ctx.startswith("./"):
                svc["build"]["context"] = str(REPO_ROOT / ctx[2:])

        out[name] = svc

    out["caddy"] = _caddy_service(cfg)
    return out


def _absolutize_mount(v) -> str:
    if not isinstance(v, str):
        return v
    src, sep, rest = v.partition(":")
    if src.startswith("./") and sep:
        return f"{REPO_ROOT}/{src[2:]}:{rest}"
    return v


def _caddy_service(cfg: GatewayConfig) -> dict:
    ports = ["80:80", "443:443"]
    if cfg.domain.mode == "none":
        # 도메인이 없으면 포트별 HTTP 라우팅
        ports = [f"{p}:{p}" for p in WEB_PORTS.values()]
    return {
        "image": "caddy:2-alpine",
        "restart": "unless-stopped",
        "ports": ports,
        "volumes": [
            "./Caddyfile:/etc/caddy/Caddyfile:ro",
            "caddy_data:/data",
            "caddy_config:/config",
        ],
        "networks": ["data-plane", "control-plane", "shared"],
        # health 게이트 없이 started 만 기다리면 부팅 초기 502 가 나온다
        "depends_on": {s: {"condition": "service_healthy"} for s in WEB_PORTS},
    }


def _remote_ip_guard(cfg: GatewayConfig) -> str:
    """allowed_cidrs 가 있으면 Caddy 레벨 IP allowlist snippet."""
    if not cfg.network.allowed_cidrs:
        return ""
    cidrs = " ".join(cfg.network.allowed_cidrs)
    return f"""
\t@blocked not remote_ip {cidrs}
\trespond @blocked 403
"""


def render_caddyfile(cfg: GatewayConfig) -> str:
    """도메인 유무에 따라 host 기반 TLS 라우팅 또는 포트 기반 HTTP 라우팅."""
    if cfg.domain.mode == "none":
        blocks = []
        for svc, port in WEB_PORTS.items():
            blocks.append(f":{port} {{{_remote_ip_guard(cfg)}\n\treverse_proxy {svc}:{port}\n}}")
        return "# generated by deploy — 직접 수정하지 말 것 (deploy render 로 재생성)\n\n" + "\n\n".join(blocks) + "\n"

    base = cfg.domain.name
    host_map = {
        f"gateway.{base}": ("gateway-proxy", 8000),
        f"admin-api.{base}": ("admin-api", 8080),
        f"admin.{base}": ("admin-ui", 3000),
    }
    blocks = []
    for host, (svc, port) in host_map.items():
        blocks.append(f"{host} {{{_remote_ip_guard(cfg)}\n\treverse_proxy {svc}:{port}\n}}")
    return "# generated by deploy — 직접 수정하지 말 것 (deploy render 로 재생성)\n\n" + "\n\n".join(blocks) + "\n"


def desired_env(cfg: GatewayConfig) -> dict[str, str]:
    """gateway.yaml → .env 의 결정적 부분(시크릿 제외)."""
    n = cfg.features.notifications
    env = {
        "POSTGRES_USER": "gateway",
        "POSTGRES_DB": "gateway",
        "AWS_REGION": cfg.aws.region,
        "AWS_DEFAULT_REGION": cfg.aws.region,
        "ALLOWED_STS_REGIONS": cfg.aws.region,
        "DEV_LOGIN_ENABLED": "false" if cfg.oidc.enabled else "true",
        "EMAIL_SENDER_TYPE": n.provider,
        "EMAIL_SENDER_ADDRESS": n.ses_from or n.smtp_from or "noreply@llm-gateway.local",
        "LOG_LEVEL": "INFO",
    }
    if n.provider == "ses":
        env["AWS_SES_REGION"] = cfg.aws.region
    if n.provider == "smtp":
        env["SMTP_HOST"] = n.smtp_host
        env["SMTP_PORT"] = str(n.smtp_port)
    if cfg.oidc.enabled:
        env.update({
            "OIDC_ISSUER_URL": cfg.oidc.issuer_url,
            "OIDC_AUDIENCE": cfg.oidc.audience or cfg.oidc.client_id,
            "OIDC_PROVIDER_NAME": cfg.oidc.provider_name,
            "OIDC_GROUPS_CLAIM": "cognito:groups" if "cognito" in cfg.oidc.provider_name else "groups",
            "OIDC_CLIENT_ID": cfg.oidc.client_id,
            "OIDC_AUTHORIZE_URL": cfg.oidc.authorize_url,
            "OIDC_TOKEN_URL": cfg.oidc.token_url,
            "OIDC_REQUIRED_GROUP": cfg.oidc.required_group,
        })
        if cfg.oidc.client_secret:
            env["OIDC_CLIENT_SECRET"] = cfg.oidc.client_secret
    if cfg.features.web_search:
        # URL 은 AgentCore Gateway 수동 프로비저닝 결과물 — .env 에 추가하면 활성화됨
        env["WEB_SEARCH_ENABLED"] = "true"
    return env


def feature_notes(cfg: GatewayConfig) -> list[str]:
    """compose backend 가 이 기능 플래그로 무엇을 하는지/못 하는지 — 침묵 드롭 금지.

    render 출력에 그대로 표시한다. 'env 를 써 넣었지만 인프라가 필요' 와
    '이 backend 미지원' 을 구분해 알린다."""
    notes: list[str] = []
    if cfg.features.web_search:
        notes.append(
            "web_search: .env 에 WEB_SEARCH_ENABLED=true 를 썼습니다. 실제 동작에는 추가로 "
            "① AgentCore Gateway 프로비저닝(us-east-1) 후 .env 에 AGENTCORE_GATEWAY_URL 기입, "
            "② routing_profiles.web_search_enabled DB 행이 필요합니다 — 없으면 조용히 off 입니다.")
    if cfg.features.body_logging:
        notes.append(
            "body_logging: compose 경로는 S3/Firehose 인프라를 만들지 않아 미지원입니다 — "
            "플래그는 기록됐지만 로깅은 동작하지 않습니다.")
    if cfg.features.pricing_lambda:
        notes.append(
            "pricing_lambda: Lambda 배포는 이 렌더러 범위 밖입니다 — 배포 후 "
            ".env 에 LITELLM_PRICING_LAMBDA=<arn> 을 추가하십시오.")
    if cfg.features.observability:
        notes.append("observability: OTel/Prometheus/Loki/Tempo/Grafana 스택을 포함해 렌더했습니다.")
    if cfg.clients.models_profile == "regional":
        notes.append("models_profile=regional: 시드 alias 는 마이그레이션 관리 — "
                     "리전별 프로필 추가는 마이그레이션/관리 UI 에서 진행하십시오.")
    return notes


def render(cfg: GatewayConfig, out_dir: Path, repo_root: Path = REPO_ROOT) -> dict:
    """산출물을 out_dir 에 생성한다. 반환: {"files": [...], "filled": [...], "preserved": [...]}"""
    out_dir.mkdir(parents=True, exist_ok=True)

    base = yaml.safe_load((repo_root / "docker-compose.yml").read_text())
    services = transform_services(base, cfg)

    # 선언은 하되 아무 서비스도 안 쓰는 volume(grafana-data 등)은 싣지 않는다
    used = set()
    for svc in services.values():
        for v in svc.get("volumes") or []:
            if isinstance(v, str):
                src = v.split(":")[0]
                if not src.startswith("/") and not src.startswith("."):
                    used.add(src)
            elif isinstance(v, dict) and v.get("type") == "volume":
                used.add(v.get("source"))
    volumes = {k: v for k, v in base.get("volumes", {}).items() if k in used}
    volumes.update({"caddy_data": None, "caddy_config": None})

    composed = {
        "name": f"llm-gateway-{cfg.env}",
        "networks": base.get("networks", {}),
        "volumes": volumes,
        "services": services,
    }
    compose_path = out_dir / "docker-compose.yml"
    compose_path.write_text("# generated by deploy — 직접 수정하지 말 것\n" + yaml.safe_dump(
        composed, sort_keys=False, allow_unicode=True, width=200))

    caddy_path = out_dir / "Caddyfile"
    caddy_path.write_text(render_caddyfile(cfg))

    env_path = out_dir / ".env"
    existing = common.parse_env_file(env_path)
    merged, differing = common.merge_env(existing, desired_env(cfg))
    filled = common.fill_generated(merged)
    common.write_env_file(env_path, merged, header=(
        "# generated by deploy — 시크릿은 재렌더해도 보존된다.\n"
        "# ⚠️ 이 파일을 잃으면 발급된 Virtual Key 를 전부 다시 만들어야 한다.\n"
        "#    VIRTUAL_KEY_ENCRYPTION_KEY 는 별도 안전한 곳에 백업할 것."))
    return {"files": [compose_path, caddy_path, env_path], "filled": filled,
            "preserved": differing, "notes": feature_notes(cfg)}
