# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""gateway.yaml 스키마 — 로드·검증·직렬화. 외부 의존 없이 dataclass + yaml 만 쓴다."""
from __future__ import annotations

import ipaddress
import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

DEPLOY_TARGETS = ("compose", "ecs", "eks")
IMPLEMENTED_TARGETS = ("compose", "ecs", "eks")   # 렌더러가 있는 backend
SIZE_TIERS = ("t0", "t1", "t2", "t3")
NETWORK_MODES = ("public", "private")
DOMAIN_MODES = ("none", "cloudfront-temp", "route53-acm")
NOTIFICATION_PROVIDERS = ("mock", "smtp", "ses")
MODEL_PROFILES = ("global", "regional")

_ENV_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d+$")


class SchemaError(ValueError):
    """gateway.yaml 검증 실패. errors에 사람이 읽을 메시지를 모은다."""


@dataclass
class AwsConfig:
    region: str = "ap-northeast-2"
    account_id: str = ""


@dataclass
class DeployConfig:
    target: str = "compose"
    size_tier: str = "t1"
    sizing: dict[str, Any] = field(default_factory=dict)
    # terraform state backend (ecs/eks 경로). 비우면 account_id 규칙으로 추론하거나
    # render 가 placeholder 를 둔다.
    tfstate_bucket: str = ""
    tfstate_table: str = ""
    # eks 경로 — helm release/namespace(기본 llm-gateway) 와 인프라를 소유한
    # terraform env 디렉토리(기본 environments/llm-gateway-<env>)
    release: str = "llm-gateway"
    namespace: str = "llm-gateway"
    tf_env_dir: str = ""


@dataclass
class NetworkConfig:
    mode: str = "public"
    allowed_cidrs: list[str] = field(default_factory=list)


@dataclass
class DomainConfig:
    mode: str = "none"
    name: str = ""
    zone_id: str = ""


@dataclass
class NotificationConfig:
    provider: str = "mock"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = ""
    ses_from: str = ""
    sender_name: str = ""          # 비우면 "LLM Gateway"


@dataclass
class FeaturesConfig:
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    bi_insight: bool = False
    pricing_lambda: bool = False
    web_search: bool = False
    body_logging: bool = False
    observability: bool = False


@dataclass
class ImagesConfig:
    registry: str = ""
    tag: str = ""


@dataclass
class ClientsConfig:
    models_profile: str = "global"


@dataclass
class OidcConfig:
    """admin-api(JWT 검증) + admin-ui(브라우저 SSO) 공용 OIDC 설정.

    비워두면 OIDC 비활성 — admin-ui 는 로그인 경로가 없어지므로 dev-login 으로만
    들어갈 수 있다. 채우면 Cognito/Keycloak 등 issuer 기반으로 동작."""
    issuer_url: str = ""
    audience: str = ""          # admin-api JWT aud (Cognito: app client id)
    client_id: str = ""         # admin-ui SSO client id
    client_secret: str = ""     # Cognito confidential client — 있으면 HTTP Basic 토큰 교환
    authorize_url: str = ""     # hosted-ui authorize (issuer 가 아님)
    token_url: str = ""
    provider_name: str = "oidc:cognito"
    required_group: str = ""    # 비우면 인증된 모든 사용자 통과

    @property
    def enabled(self) -> bool:
        return bool(self.issuer_url)


@dataclass
class GatewayConfig:
    env: str = ""
    aws: AwsConfig = field(default_factory=AwsConfig)
    deploy: DeployConfig = field(default_factory=DeployConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    domain: DomainConfig = field(default_factory=DomainConfig)
    features: FeaturesConfig = field(default_factory=FeaturesConfig)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    clients: ClientsConfig = field(default_factory=ClientsConfig)
    oidc: OidcConfig = field(default_factory=OidcConfig)

    # ------------------------------------------------------------------ #
    def warnings(self) -> list[str]:
        """검증은 통과하지만 운영상 주의가 필요한 조합."""
        w: list[str] = []
        if self.network.mode == "public" and not self.network.allowed_cidrs:
            w.append(
                "network.allowed_cidrs 가 비어 있어 게이트웨이가 인터넷 전체에 열립니다. "
                "사용자 PC의 공인 IP 대역을 지정하십시오."
            )
        if self.deploy.size_tier in ("t0", "t1"):
            w.append(
                f"size_tier={self.deploy.size_tier} 는 단일 노드/백업-기반 구성입니다 — "
                "HA·자동 장애조치가 없습니다(복원 = 스냅샷/백업)."
            )
        if self.domain.mode == "none":
            w.append(
                "domain.mode=none 이면 TLS 없이 HTTP 로 시작합니다 — "
                "Cowork 는 https 가 필수라 해당 클라이언트는 동작하지 않습니다."
            )
        if self.features.web_search and self.deploy.target == "compose":
            w.append("web_search 는 AgentCore(us-east-1) 의존 — compose 경로에서는 수동 프로비저닝이 필요합니다.")
        if self.deploy.target == "eks":
            w.append("size_tier 는 eks 경로에서 실제 리소스를 바꾸지 않습니다 — "
                     "컴퓨트 크기는 차트 values/HPA/requests 가 결정합니다 (기존 인프라 이어받기 모델).")
        if self.deploy.target == "eks" and not self.images.registry:
            w.append("images.registry 비어 있음 — apply 시 계정 기본 ECR 로 추론합니다.")
        if self.deploy.target not in IMPLEMENTED_TARGETS:
            w.append(f"deploy.target={self.deploy.target} 의 렌더러는 아직 구현 전입니다 (validate 만 지원).")
        return w

    def validate(self) -> list[str]:
        e: list[str] = []
        if not self.env:
            e.append("env 는 필수입니다 (예: acme-small).")
        elif not _ENV_RE.match(self.env):
            e.append(f"env '{self.env}' 는 소문자/숫자/하이픈 2~31자여야 합니다.")
        if not _REGION_RE.match(self.aws.region):
            e.append(f"aws.region '{self.aws.region}' 이 리전 형식이 아닙니다 (예: ap-northeast-2).")
        if self.deploy.target not in DEPLOY_TARGETS:
            e.append(f"deploy.target 은 {DEPLOY_TARGETS} 중 하나여야 합니다: {self.deploy.target}")
        if self.deploy.size_tier not in SIZE_TIERS:
            e.append(f"deploy.size_tier 는 {SIZE_TIERS} 중 하나여야 합니다: {self.deploy.size_tier}")
        if self.deploy.target == "compose" and self.deploy.size_tier != "t0":
            e.append("compose backend 는 size_tier=t0 만 지원합니다 (단일 노드).")
        if self.deploy.target == "ecs" and self.deploy.size_tier not in ("t1", "t2"):
            e.append("ecs backend 는 size_tier=t1|t2 를 지원합니다 (t3 는 eks 경로).")
        if self.deploy.target == "eks" and self.deploy.size_tier not in ("t2", "t3"):
            e.append("eks backend 는 size_tier=t2|t3 입니다.")
        if self.deploy.size_tier == "t0" and self.deploy.target != "compose":
            e.append("size_tier=t0 는 compose backend 전용입니다.")
        if self.deploy.target in ("ecs", "eks") and not self.images.tag:
            e.append(f"deploy.target={self.deploy.target} 는 images.tag 명시가 필수입니다.")
        # registry 비우면 ecs 는 모듈이 ECR repo 를 만들고, eks 는 apply 가
        # <account>.dkr.ecr.<region> 으로 추론한다 (install-eks.sh 와 동일)
        if self.network.mode not in NETWORK_MODES:
            e.append(f"network.mode 는 {NETWORK_MODES} 중 하나여야 합니다.")
        if self.domain.mode not in DOMAIN_MODES:
            e.append(f"domain.mode 은 {DOMAIN_MODES} 중 하나여야 합니다.")
        if self.domain.mode == "route53-acm" and not self.domain.name:
            e.append("domain.mode=route53-acm 이면 domain.name 이 필수입니다.")
        if (self.domain.mode == "route53-acm"
                and self.deploy.target == "ecs"
                and not self.domain.zone_id):
            e.append("ecs 의 route53-acm 은 domain.zone_id(hosted zone)가 필수입니다 — "
                     "ACM 검증·레코드 생성에 필요. eks 는 env values 의 기존 인증서를 씁니다.")
        if self.features.notifications.provider not in NOTIFICATION_PROVIDERS:
            e.append(f"notifications.provider 는 {NOTIFICATION_PROVIDERS} 중 하나여야 합니다.")
        if self.features.notifications.provider == "ses" and not self.features.notifications.ses_from:
            e.append("notifications.provider=ses 이면 notifications.ses_from 이 필요합니다.")
        if self.features.notifications.provider == "smtp" and not self.features.notifications.smtp_host:
            e.append("notifications.provider=smtp 이면 notifications.smtp_host 가 필요합니다.")
        if self.features.bi_insight and self.deploy.target == "compose":
            e.append("bi_insight(AgentCore Runtime)는 compose backend에서 지원하지 않습니다.")
        if self.domain.mode == "cloudfront-temp" and self.deploy.target == "compose":
            e.append("domain.mode=cloudfront-temp 는 compose 에서 지원하지 않습니다 — "
                     "none 또는 route53-acm 을 쓰십시오 (ecs 는 none 으로 수렴).")
        for c in self.network.allowed_cidrs:
            if not isinstance(c, str):
                e.append(f"allowed_cidrs 항목은 문자열이어야 합니다: {c!r}")
                continue
            try:
                ipaddress.ip_network(c, strict=False)
            except ValueError:
                e.append(f"allowed_cidrs '{c}' 는 유효한 CIDR 이 아닙니다 (예: 1.2.3.4/32)")
        if self.clients.models_profile not in MODEL_PROFILES:
            e.append(f"clients.models_profile 은 {MODEL_PROFILES} 중 하나여야 합니다.")
        if self.oidc.enabled:
            if not self.oidc.client_id:
                e.append("oidc.issuer_url 을 채웠으면 admin-ui SSO 용 oidc.client_id 도 필요합니다.")
            if not (self.oidc.authorize_url and self.oidc.token_url):
                e.append("oidc 를 쓰려면 authorize_url / token_url 도 필요합니다 (hosted-ui 엔드포인트).")
        return e


def _safe_int(v: Any, errors: list[str], path: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        errors.append(f"{path} 는 정수여야 합니다: {v!r}")
        return 0


def _require_mapping(node: Any, path: str, errors: list[str]) -> dict:
    if node is None:
        return {}
    if not isinstance(node, dict):
        errors.append(f"{path} 는 mapping 이어야 합니다.")
        return {}
    return node


def from_dict(raw: dict) -> GatewayConfig:
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise SchemaError("gateway.yaml 최상위는 mapping 이어야 합니다.")
    version = raw.get("version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        errors.append(f"version 은 {SCHEMA_VERSION} 이어야 합니다 (got {version!r}).")

    aws = _require_mapping(raw.get("aws"), "aws", errors)
    deploy = _require_mapping(raw.get("deploy"), "deploy", errors)
    network = _require_mapping(raw.get("network"), "network", errors)
    domain = _require_mapping(raw.get("domain"), "domain", errors)
    features = _require_mapping(raw.get("features"), "features", errors)
    notif = _require_mapping(features.get("notifications"), "features.notifications", errors)
    images = _require_mapping(raw.get("images"), "images", errors)
    clients = _require_mapping(raw.get("clients"), "clients", errors)
    oidc = _require_mapping(raw.get("oidc"), "oidc", errors)

    cfg = GatewayConfig(
        env=str(raw.get("env", "")),
        aws=AwsConfig(
            region=str(aws.get("region", "ap-northeast-2")),
            account_id=str(aws.get("account_id", "")),
        ),
        deploy=DeployConfig(
            target=str(deploy.get("target", "compose")),
            size_tier=str(deploy.get("size_tier", "t1")),
            sizing=dict(deploy.get("sizing") or {}),
            tfstate_bucket=str(deploy.get("tfstate_bucket", "")),
            tfstate_table=str(deploy.get("tfstate_table", "")),
            release=str(deploy.get("release", "llm-gateway")),
            namespace=str(deploy.get("namespace", "llm-gateway")),
            tf_env_dir=str(deploy.get("tf_env_dir", "")),
        ),
        network=NetworkConfig(
            mode=str(network.get("mode", "public")),
            # 문자열을 넣으면 list() 가 글자 단위로 분해하므로 단일 항목으로 감싼다
            allowed_cidrs=([network["allowed_cidrs"]]
                           if isinstance(network.get("allowed_cidrs"), str)
                           else list(network.get("allowed_cidrs") or [])),
        ),
        domain=DomainConfig(
            mode=str(domain.get("mode", "none")),
            name=str(domain.get("name", "")),
            zone_id=str(domain.get("zone_id", "")),
        ),
        features=FeaturesConfig(
            notifications=NotificationConfig(
                provider=str(notif.get("provider", "mock")),
                smtp_host=str(notif.get("smtp_host", "")),
                smtp_port=_safe_int(notif.get("smtp_port", 587), errors,
                                    "features.notifications.smtp_port"),
                smtp_from=str(notif.get("smtp_from", "")),
                ses_from=str(notif.get("ses_from", "")),
                sender_name=str(notif.get("sender_name", "")),
            ),
            bi_insight=bool(features.get("bi_insight", False)),
            pricing_lambda=bool(features.get("pricing_lambda", False)),
            web_search=bool(features.get("web_search", False)),
            body_logging=bool(features.get("body_logging", False)),
            observability=bool(features.get("observability", False)),
        ),
        images=ImagesConfig(
            registry=str(images.get("registry", "")),
            tag=str(images.get("tag", "")),
        ),
        clients=ClientsConfig(
            models_profile=str(clients.get("models_profile", "global")),
        ),
        oidc=OidcConfig(
            issuer_url=str(oidc.get("issuer_url", "")),
            audience=str(oidc.get("audience", "")),
            client_id=str(oidc.get("client_id", "")),
            authorize_url=str(oidc.get("authorize_url", "")),
            token_url=str(oidc.get("token_url", "")),
            client_secret=str(oidc.get("client_secret", "")),
            provider_name=str(oidc.get("provider_name", "oidc:cognito")),
            required_group=str(oidc.get("required_group", "")),
        ),
    )
    errors.extend(cfg.validate())
    if errors:
        raise SchemaError("\n".join(f"  - {m}" for m in errors))
    return cfg


def load(path: Path) -> GatewayConfig:
    try:
        text = path.read_text()
    except FileNotFoundError as exc:
        raise SchemaError(
            f"{path} 가 없습니다 — 새 배포는 `./deploy init`, "
            f"기존 배포 온보딩은 `./deploy doctor --capture` 로 생성하십시오.") from exc
    except UnicodeDecodeError as exc:
        raise SchemaError(f"{path} 가 UTF-8 텍스트가 아닙니다: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SchemaError(f"gateway.yaml 파싱 실패: {exc}") from exc
    return from_dict(raw or {})
