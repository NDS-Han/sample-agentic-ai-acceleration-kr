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
DOMAIN_MODES = ("none", "cloudfront-temp", "selfsigned-temp", "route53-acm")
NOTIFICATION_PROVIDERS = ("mock", "smtp", "ses", "internal_api")
MODEL_PROFILES = ("global", "regional")

# admin-ui 브랜드명 — admin-ui/src/lib/branding.ts 의 DEFAULT_BRAND_NAME /
# MAX_BRAND_NAME_LENGTH 와 같은 값이어야 한다.
DEFAULT_BRAND_NAME = "AWSome AI Gateway"
MAX_BRAND_NAME_LENGTH = 40

_ENV_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


class SchemaError(ValueError):
    """gateway.yaml 검증 실패. errors에 사람이 읽을 메시지를 모은다."""


@dataclass
class AwsConfig:
    region: str = "ap-northeast-2"
    account_id: str = ""
    # CLI(STS 경로) VK 발급의 IAM role 허용 목록 — helm 의 aws.allowedIamRoles 와
    # 같은 의미. 비어 있으면 compose 배포에서 CLI 로그인이 fail-closed 로 전량 403.
    allowed_roles: list[str] = field(default_factory=list)


@dataclass
class DeployConfig:
    target: str = "compose"
    size_tier: str = "t1"
    sizing: dict[str, Any] = field(default_factory=dict)
    # eks 전용 용량 밴드 — tiers.EKS_CAPACITY_TIERS 키("500"~"6000+").
    # 프리셋이 바닥이고 sizing 의 명시 키가 개별 오버라이드.
    capacity_tier: str = ""
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
class PortsConfig:
    """compose 전용 — Caddy 가 바인드하는 호스트 포트(유일한 인입점이라
    이것만 바꾸면 된다). 컨테이너 측 포트는 Caddyfile 과 고정이라 불변."""
    http: int = 80         # domain 부착 시 HTTP
    https: int = 443       # domain 부착 시 HTTPS
    gateway: int = 8000    # domain mode=none — gateway-proxy
    admin_api: int = 8080  # domain mode=none — admin-api
    admin_ui: int = 3000   # domain mode=none — admin-ui


@dataclass
class NetworkConfig:
    mode: str = "public"
    allowed_cidrs: list[str] = field(default_factory=list)
    # eks 전용 — admin-ui/admin-api Ingress 만 scheme=internal 로 내린다
    # (gateway 데이터플레인은 public 유지 — ops 8-I 목표형). mode=private 은
    # 3개 Ingress 전부를 internal 로 만들어 게이트웨이까지 끊기므로 다른 의미다.
    admin_internal: bool = False
    ports: PortsConfig = field(default_factory=PortsConfig)


@dataclass
class DomainConfig:
    mode: str = "none"
    name: str = ""
    zone_id: str = ""
    # 실제 호스트 오버라이드 (gateway/admin_ui/admin_api) — 비우면
    # gateway./admin./admin-api.{name} 으로 파생한다. 기존 배포 채택(capture)처럼
    # gateway-dev.* 같은 다른 규칙을 쓸 때 URL 보존용.
    hosts: dict[str, str] = field(default_factory=dict)


@dataclass
class NotificationConfig:
    provider: str = "mock"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = ""
    ses_from: str = ""
    sender_name: str = ""          # 비우면 "LLM Gateway"
    internal_api_url: str = ""     # provider=internal_api — 사내 메일 API URL (필수)
    internal_api_from: str = ""    # 발신 주소 (비우면 차트 기본)
    smtp_credentials_secret: str = ""  # eks: SMTP 인증 Secret 이름 (차트 credentialsSecretName)


@dataclass
class AlertingConfig:
    """인프라 헬스 알림 — eks/ecs 전용. managed overlay(module.alerting)가
    SNS topic + Aurora/ElastiCache CloudWatch alarms + RDS event subscription
    을 만든다. email 구독은 SNS 특성상 수신자가 메일로 confirm 해야 발송
    시작된다 — 콘솔/CLI 확인 절차는 docs/NDS/observability.md.

    enabled 는 topic·alarm·emails 게이트다 — emails 는 enabled 필요(검증 에러).
    log_shipping 은 독립 축이라 enabled 없이 단독으로도 켤 수 있다."""
    enabled: bool = False
    # SNS email 구독 — apply 후 각 주소로 confirmation 메일이 간다.
    emails: list[str] = field(default_factory=list)
    # eks 전용 — Fargate 컨테이너 stdout/stderr → CloudWatch Logs 라우터
    # (aws-observability namespace + aws-logging ConfigMap, helm 템플릿).
    # 비용·보관 기준은 docs/NDS/observability.md.
    log_shipping: bool = False


@dataclass
class BusinessAlertsConfig:
    """비용 정확성 비즈니스 알림(notification-worker) — 3타겟 공통 모델.

    전부 기본 꺼짐. enabled 는 마스터(폴링 태스크 자체), 개별 check 스위치는
    그 아래에서 켠다. slack=true 면 전달 경로를 배선한다:
    - eks: notification-worker 가 k8s Secret `alertmanager-slack-<env>`(key api_url,
      Alertmanager 와 같은 env 별 공유 Secret)을 optional secretKeyRef 로 읽는다 —
      Secret 이 없어도 워커는 정상 기동하고 알림만 비활성.
    - ecs: app Secrets Manager secret 의 json key `slack_webhook_url` 을 valueFrom 으로
      배선한다. 활성화 전에 그 key 를 SM 에 만들어 두어야 한다(없으면 태스크 기동 실패 —
      사전 수동 단계, 런타임 graceful 아님).
    - compose: .env 의 SLACK_WEBHOOK_URL 을 그대로 통과시킨다(없으면 워커가 알림만 끈다).
    임계값·폴링 주기는 서비스 기본값(helm values / worker config)을 쓰고, DB
    notification_configs 행이 env 보다 우선한다(기존 계약).
    """
    enabled: bool = False
    reconciliation_mismatch: bool = False
    dead_letter_growth: bool = False
    pricing_missing: bool = False
    slack: bool = False


@dataclass
class FeaturesConfig:
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    alerting: AlertingConfig = field(default_factory=AlertingConfig)
    bi_insight: bool = False
    pricing_lambda: bool = False
    web_search: bool = False
    # None = yaml 미선언 → managed overlay 가 enable_body_logging 키를 쓰지 않아
    # env tfvars(사람 소유) 값이 유지된다. 명시 false 도 비활성화를 apply 하지
    # 않는다 — overlay 가 false 를 쓰면 기존 Firehose/S3 가 destroy 계획에
    # 들어가므로, 비활성화는 env terraform 수동 절차다 (warnings 도 참고).
    body_logging: bool | None = None
    observability: bool = False
    # oidc 와 공존하는 개발자 로그인(DEV_LOGIN_ENABLED) — false 이면 oidc 설정 시
    # render 가 adminUi DEV_LOGIN_ENABLED 를 강제로 끈다(기존 동작). true 이면
    # adminUi/adminApi 양쪽에 true 를 박아 수동 --set 없이도 유지된다.
    dev_login: bool = False
    # 서비스 계정 장기 VK 발급 — 기본 꺼짐(opt-in). true 면 발급 허용, false/미선언
    # 면 admin-api 가 표지·발급 엔드포인트를 403 거부하고 GET /admin/service-accounts
    # 가 service_keys_enabled=false 를 내려 UI 가 발급 버튼을 숨긴다.
    # 기존 키는 폐기 전까지 유효하다(발급만 차단). admin-api 앱 기본도 false 라
    # fail-closed 가 이중으로 보장되지만, 렌더러는 값을 항상 명시적으로
    # 내려보내 env 오버레이의 stale true 를 덮는다.
    service_keys: bool = False
    business_alerts: BusinessAlertsConfig = field(default_factory=BusinessAlertsConfig)


# eks helm 오버레이에서 이미지 태그를 가질 수 있는 키. migration 도 태그가
# 있지만 자체 버전 라인(1.0.5x 계열)이라 단일 tag 기본값에는 포함하지 않고
# images.tags.migration 으로만 핀한다.
EKS_TAGGABLE_SERVICES = frozenset((
    "gatewayProxy", "adminApi", "adminUi", "scheduler",
    "notificationWorker", "costRecorderWorker", "migration",
))

# deploy.sizing 이 다루는 ecs 서비스 (render/ecs.py 의 service_sizing 키와 동일)
ECS_SERVICES = frozenset((
    "gateway-proxy", "admin-api", "admin-ui", "scheduler",
    "notification-worker", "cost-recorder-worker", "migration",
))

# deploy.sizing 의 eks 소비 키 (render/eks.py desired_values 와 동일).
# size_tier 는 eks 에서 문서용이라 실제 크기는 여기서 정한다.
# db_* 키는 차트의 서비스별 db: 확장점으로 렌더된다 — snake_case sizing 키가
# 차트 camelCase db.{poolSize,maxOverflow,poolTimeout,poolRecycle} 로 매핑되어
# 공통 ConfigMap(envFrom) 값을 per-service env 로 override 한다.
_EKS_DB_POOL_SIZING = frozenset((
    "db_pool_size", "db_max_overflow", "db_pool_timeout", "db_pool_recycle",
))
# db_* 값의 허용 범위 (프로세스당, 양끝 포함). 차트 helper 는 형식만 보고 크기는
# 모른다 — gateway.yaml 오타 하나가 pods × WORKERS 배로 곱해져 RDS Proxy 백엔드를
# 고갈시키지 않도록 여기서 절대 상한을 건다 (rv-swe P1-1).
# - db_pool_size ≥ 1: SQLAlchemy pool_size=0 은 "무제한" 이다.
# - db_max_overflow ≥ 0: 0 은 pool_size 를 정확한 상한으로 쓰는 하드캡(P2-1),
#   -1 은 무제한 overflow 라 거부한다.
# - db_pool_timeout ≤ 10: wave-1b AC4 — 풀 고갈 시 요청이 오래 매달리지 않게.
# - db_pool_recycle < 1800: RDS Proxy idle_client_timeout(1800s,
#   modules/aurora-postgresql/variables.tf) 보다 먼저 재생성.
EKS_DB_POOL_BOUNDS: dict[str, tuple[int, int]] = {
    "db_pool_size": (1, 50),
    "db_max_overflow": (0, 50),
    "db_pool_timeout": (1, 10),
    "db_pool_recycle": (60, 1799),
}
EKS_SIZING = {
    "gateway-proxy": frozenset((
        "min_replicas", "max_replicas", "bedrock_thread_pool_size",
        *_EKS_DB_POOL_SIZING,
    )),
    "admin-api": _EKS_DB_POOL_SIZING,
    "cost-recorder-worker": _EKS_DB_POOL_SIZING,
    "notification-worker": _EKS_DB_POOL_SIZING,
    "scheduler": _EKS_DB_POOL_SIZING,
    "redis": frozenset(("pool_size",)),
}
_ECS_SIZING_KEYS = frozenset((
    "cpu", "memory", "desired_count", "min_count", "max_count",
))


@dataclass
class ImagesConfig:
    # registry 는 "레지스트리 호스트" 의미 (예: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com).
    # eks 는 차트가 host 뒤에 llm-gateway/<svc> 를 붙이고, ecs 렌더도 같은 규칙으로
    # /llm-gateway 를 붙인다 — 두 backend 가 동일 값을 같은 의미로 쓴다.
    registry: str = ""
    tag: str = ""
    # 서비스별 태그 (eks 전용) — US-19 처럼 릴리스가 서비스별 태그로 나올 때
    # 단일 tag 로는 표현 불가. tag 를 기본값으로 두고 tags 가 개별 키를 덮는다.
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class ClientsConfig:
    models_profile: str = "global"


@dataclass
class BrandingConfig:
    # admin-ui 사이드바·탭 제목의 서비스 이름. 비우면 DEFAULT_BRAND_NAME.
    # compose/ecs/eks 모두 admin-ui 런타임 env ADMIN_BRAND_NAME 으로 내려간다.
    name: str = ""
    # Cognito Managed Login(v2) + 브랜딩(modules/cognito/managed-login). ecs/eks
    # 전용 — terraform cognito 모듈이 도메인 버전·Essentials 요금제·브랜딩 스타일을
    # 소유한다. 켜기만 managed overlay 로 발산한다(끄기는 브랜딩 스타일 삭제라
    # 파괴 변경 — env terraform 수동 절차).
    managed_login: bool = False

    @property
    def effective_name(self) -> str:
        return self.name or DEFAULT_BRAND_NAME


@dataclass
class InfraDbConfig:
    """ecs 경로 DB 세부 override — 빈 값이면 size_tier 프리셋을 쓴다.
    mode 전환(serverless↔provisioned)은 리소스 재생성급 변경이다."""
    mode: str = ""                  # serverless | provisioned
    min_acu: float | None = None    # serverless 전용 — 0.5 단위
    max_acu: float | None = None
    instance_class: str = ""        # provisioned 전용 (비우면 db.r7g.large)
    # 기존 클러스터의 실측 메이저 핀(예 "16", "16.13") — parameter group
    # family 를 그쪽에 맞춘다. 비우면 AWS 기본 버전의 메이저(신규 생성용).
    # 기존 env 는 미선언 시 다음 plan 부터 family drift 가 뜨니 반드시 핀.
    engine_version: str = ""


@dataclass
class InfraCacheConfig:
    """ecs 경로 ElastiCache 세부 override — 빈 값이면 size_tier 프리셋.
    토폴로지 키는 mode 와 맞아야 한다: num_clusters 는 non-cluster 노드 수,
    num_node_groups/replicas_per_node_group 은 cluster 전용 — 안 맞으면 무시된다."""
    mode: str = ""                             # single | replicated | cluster
    node_type: str = ""
    num_clusters: int | None = None            # non-cluster 노드 수 (1~6, replicated 면 >=2)
    num_node_groups: int | None = None         # cluster 샤드 수 (1~90)
    replicas_per_node_group: int | None = None # cluster 샤드당 replica (1~5)


@dataclass
class InfraConfig:
    """신규 설치 인프라 입력 (bootstrap — ecs/eks).

    빈 값이면 backend 기본을 쓴다 (eks env 의 variables.tf 기본값 / ecs 모듈
    기본값). 값을 넣으면 managed overlay/generated tfvars 로 내려간다 —
    기존 배포 온보딩(capture) 경로에서는 채우지 않는다."""
    azs: list[str] = field(default_factory=list)
    vpc_cidr: str = ""
    cognito_domain_suffix: str = ""   # 비우면 <region-code>-auth-<account>
    # Claude_ 접두 필수 — 접두 없는 그룹은 OIDC 로그인 거부(REJECT_UNMATCHED_GROUPS).
    # ClaudeAdmin 은 항상 자동 포함이라 목록에 쓰지 않아도 된다.
    cognito_groups: list[str] = field(default_factory=list)
    # 비우면 backend 기본: eks=variables.tf 최신 목록, ecs=models_profile 매핑.
    bedrock_allowed_model_arns: list[str] = field(default_factory=list)
    # eks 전용 — in-account Bedrock Mantle 호출 허용 리전. 비우면 env
    # variables.tf 기본값. 선언하면 managed overlay 가 module.irsa 정책을 소유.
    mantle_regions: list[str] = field(default_factory=list)
    # eks 전용 — 선언된 EKS 목표 버전 ("1.34" 형식). 일반 apply 는 이 값으로
    # 클러스터를 바꾸지 않는다 — live 와 다르면 경고만 하고, 실제 업그레이드는
    # `deploy upgrade eks` 가 홉 단위 절차로 수행한다. 비우면 버전을 관리
    # 하지 않는다는 뜻 (캡처는 라이브 버전을 역생성한다).
    eks_version: str = ""
    db: InfraDbConfig = field(default_factory=InfraDbConfig)       # ecs 전용
    cache: InfraCacheConfig = field(default_factory=InfraCacheConfig)  # ecs 전용


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
    groups_claim: str = ""      # 비우면 provider_name 에서 유추 (cognito → cognito:groups)
    group_prefix: str = ""      # 그룹→부서/팀 파싱 prefix — 비우면 차트 기본 "Claude_"
    reject_unmatched_groups: bool | None = None  # None 이면 차트 기본 true
    admin_groups: list[str] = field(default_factory=list)  # ADMIN_GROUPS 정확일치
    admin_emails: list[str] = field(default_factory=list)  # ADMIN_EMAILS
    vk_ttl_hours: int = 0       # VK 수명 — 0 이면 차트 기본(1시간). ops 8-Z 의
                              # adminApi.oidc.vkTtlHours 와 같은 키로 렌더된다.

    @property
    def enabled(self) -> bool:
        return bool(self.issuer_url)


@dataclass
class ObservabilityAlertingConfig:
    """클러스터 내 Alertmanager(kube-prometheus-stack) — eks 전용, 기본 꺼짐.

    features.alerting(terraform SNS/CloudWatch)과는 다른 축이다. render 가
    deployment/gen/<env>/eks/observability.env 에 쓰고 observability install.sh 가 읽는다.
    꺼짐(기본): Alertmanager 파드·전달 리소스 없음 — 게이트웨이 PrometheusRule 만 설치.
    켜짐: Alertmanager 1 파드 + env 별 Slack Secret(있을 때만) 전달."""
    enabled: bool = False
    # Slack incoming-webhook URL 을 담은 Secret(ns observability, key api_url) 이름.
    # 비우면 alertmanager-slack-<env>. URL 자체는 gateway.yaml 에 두지 않는다.
    slack_secret_name: str = ""


@dataclass
class ObservabilityConfig:
    alerting: ObservabilityAlertingConfig = field(default_factory=ObservabilityAlertingConfig)


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
    branding: BrandingConfig = field(default_factory=BrandingConfig)
    oidc: OidcConfig = field(default_factory=OidcConfig)
    infra: InfraConfig = field(default_factory=InfraConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    # ------------------------------------------------------------------ #
    def warnings(self) -> list[str]:
        """검증은 통과하지만 운영상 주의가 필요한 조합."""
        w: list[str] = []
        if self.deploy.target != "compose" and self.network.ports != PortsConfig():
            w.append("network.ports 는 compose 전용입니다 — eks/ecs 배포에서는 무시됩니다")
        elif self.deploy.target == "compose":
            p = self.network.ports
            if self.domain.mode == "none":
                if p.http != 80 or p.https != 443:
                    w.append("domain 없는 compose 에서는 ports.http/https 가 적용되지 "
                             "않습니다 — gateway/admin_api/admin_ui 를 사용하세요")
            else:
                if (p.gateway, p.admin_api, p.admin_ui) != (8000, 8080, 3000):
                    w.append("domain 부착 compose 에서는 ports.gateway/admin_api/admin_ui "
                             "가 적용되지 않습니다 — http/https 를 사용하세요")
                if p.https != 443:
                    w.append("ports.https ≠ 443 — Caddy 자동 HTTPS 의 인증서 발급과 "
                             "HTTP→HTTPS 리다이렉트는 공개 443 을 기대합니다. "
                             "프록시/NAT 앞단이 아니면 인증서 발급·리다이렉트가 깨집니다")
            # observability 스택은 127.0.0.1 호스트 바인드를 유지한다 — caddy 의
            # 0.0.0.0 바인드와 겹치면 compose up 이 실패
            if self.features.observability:
                obs_ports = {4317, 4318, 8889, 9090, 3100, 3200, 3001}
                used = ({p.gateway, p.admin_api, p.admin_ui}
                        if self.domain.mode == "none" else {p.http, p.https})
                hit = sorted(obs_ports & used)
                if hit:
                    w.append(f"network.ports {hit} — observability 스택이 같은 포트를 "
                             "127.0.0.1 에 바인드해 caddy 와 충돌합니다")
        open_cidrs = {"0.0.0.0/0", "::/0"}
        if self.network.mode == "public" and not self.network.allowed_cidrs:
            if self.deploy.target == "ecs":
                # ecs 모듈의 ALB SG 는 cidrs × ports setproduct — 빈 리스트면
                # 인그레스 규칙 0개라 '전체 개방'이 아니라 '아무도 못 접속' 이다
                w.append(
                    "ecs 에서 network.allowed_cidrs 가 비어 있으면 ALB 인그레스 "
                    "규칙이 하나도 생기지 않아 배포해도 접속이 불가합니다. "
                    "사용자 PC의 공인 IP 대역을 지정하십시오."
                )
            else:
                w.append(
                    "network.allowed_cidrs 가 비어 있어 게이트웨이가 인터넷 전체에 열립니다. "
                    "사용자 PC의 공인 IP 대역을 지정하십시오."
                )
        elif (self.network.mode == "public"
              and open_cidrs & set(self.network.allowed_cidrs)):
            # 빈 리스트와 같은 효과인데 '규칙을 넣었다'고 믿기 쉽다 — 캡처된 실제
            # 배포(dev) 에도 0.0.0.0/0 이 있었다
            w.append(
                "allowed_cidrs 에 0.0.0.0/0(또는 ::/0)이 포함되어 사실상 인터넷 전체에 열립니다 — "
                "실제 사용자 IP 대역으로 좁히십시오."
            )
        if self.deploy.size_tier in ("t0", "t1"):
            w.append(
                f"size_tier={self.deploy.size_tier} 는 단일 노드/백업-기반 구성입니다 — "
                "HA·자동 장애조치가 없습니다(복원 = 스냅샷/백업)."
            )
        if self.branding.managed_login and self.deploy.target == "compose":
            w.append("branding.managed_login 은 ecs/eks 전용입니다 — compose 는 "
                     "Cognito 를 만들지 않아 무시됩니다(외부 IdP 로그인 화면은 그쪽에서 설정).")
        if self.domain.hosts and not self.domain.name:
            w.append("domain.hosts 를 선언했지만 domain.name 이 없어 render 가 "
                     "무시합니다 — 파생 네이밍의 앵커가 되는 name 을 함께 채우세요.")
        if self.domain.name and self.domain.hosts:
            # 일부만 선언하면 나머지는 조용히 파생값으로 바뀐다 — admin 파생값이
            # env 의 cognito_callback_urls 와 어긋나 SSO 가 깨질 수 있으니 명시.
            derived = {"gateway": "gateway", "admin_ui": "admin",
                       "admin_api": "admin-api"}
            missing_hosts = [f"{k}({p}.{self.domain.name})"
                             for k, p in derived.items()
                             if k not in self.domain.hosts]
            if missing_hosts:
                w.append("domain.hosts 가 일부만 선언됐습니다 — 나머지는 파생 "
                         f"호스트를 씁니다: {', '.join(missing_hosts)}. 의도가 "
                         "아니면 세 키를 모두 명시하십시오.")
            # 선언 호스트가 domain.name 범위를 벗어나면 route53-acm 경로의
            # 와일드카드 인증서(*.{name})·레코드가 커버하지 못한다.
            outside = [f"{k}={h}" for k, h in self.domain.hosts.items()
                       if not h.endswith(f".{self.domain.name}")]
            if outside and self.domain.mode == "route53-acm":
                w.append("domain.hosts 가 domain.name 범위 밖입니다 — "
                         f"{', '.join(outside)}. route53-acm 경로의 인증서·"
                         "DNS 레코드가 이 호스트들을 커버하는지 확인하십시오.")
        if self.domain.mode == "none":
            w.append(
                "domain.mode=none 이면 TLS 없이 HTTP 로 시작합니다 — "
                "Cowork 는 https 가 필수라 해당 클라이언트는 동작하지 않습니다."
            )
        if self.domain.name and self.domain.mode != "route53-acm":
            w.append(
                f"domain.name='{self.domain.name}' 이 선언됐지만 "
                f"domain.mode={self.domain.mode} — 인증서·DNS 레코드 자동화는 "
                "route53-acm 에서만 동작합니다. 이 호스트의 DNS 는 수동으로 "
                "만들어야 하고"
                + (" mode=none 이면 HTTP 만 서빙되므로 Cognito SSO 콜백(https "
                   "필수)은 도메인 주소를 쓸 수 없습니다." if self.domain.mode == "none"
                   else " selfsigned-temp 의 인증서는 이 도메인을 커버하지 않아 "
                        "브라우저 경고가 뜹니다."))
        if self.network.mode == "private" and self.deploy.target == "compose":
            # compose 렌더러는 mode 를 읽지 않는다 — caddy 가 호스트 포트를
            # 그대로 연다. 선언과 다른 외부 노출이 되므로 경고한다.
            w.append(
                "network.mode=private 는 compose 에서 적용되지 않습니다 — "
                "Caddy 가 호스트 포트를 그대로 개방합니다. 호스트 방화벽/보안그룹으로 "
                "막거나 allowed_cidrs 를 사용하십시오."
            )
        if self.deploy.target == "compose" and (
                self.infra.azs or self.infra.cognito_groups
                or self.infra.bedrock_allowed_model_arns
                or self.infra.mantle_regions):
            w.append("infra.* 는 ecs/eks 신규 설치용입니다 — "
                     "deploy.target=compose 에서는 무시됩니다.")
        if self.deploy.target == "ecs" and self.infra.mantle_regions:
            w.append("infra.mantle_regions 는 eks 전용입니다 — "
                     "deploy.target=ecs 에서는 무시됩니다.")
        if self.deploy.target != "eks" and self.infra.eks_version:
            w.append("infra.eks_version 는 eks 전용입니다 — "
                     f"deploy.target={self.deploy.target} 에서는 무시됩니다.")
        if self.deploy.target != "eks" and self.domain.hosts:
            w.append("domain.hosts 는 eks render 전용입니다 — "
                     f"deploy.target={self.deploy.target} 에서는 무시됩니다.")
        # managed overlay 가 키를 발산하는 건 eks 뿐 — 그 경로에서만 "끄기는
        # 수동" 안내가 의미가 있다 (ecs 는 tfvars 에 false 로 실제 적용된다).
        if self.features.body_logging is False and self.deploy.target == "eks":
            w.append("features.body_logging=false — managed overlay 는 비활성화를 "
                     "적용하지 않습니다(기존 인프라 destroy 방지). env terraform "
                     "으로 수동 비활성화하십시오.")
        if self.network.admin_internal:
            if self.deploy.target == "eks" and self.network.mode == "private":
                w.append(
                    "network.mode=private 이면 admin 포함 3개 Ingress 전부 internal "
                    "입니다 — admin_internal 은 중복 선언입니다."
                )
            elif self.deploy.target != "eks":
                w.append(
                    f"network.admin_internal 은 eks 전용입니다 — "
                    f"deploy.target={self.deploy.target} 에서는 무시됩니다."
                )
        if (self.features.observability and self.deploy.target != "compose"):
            w.append(
                f"features.observability 는 compose 전용입니다 — "
                f"deploy.target={self.deploy.target} 에서는 무시됩니다."
            )
        al = self.features.alerting
        if al.enabled and not al.emails:
            w.append("features.alerting.enabled=true 이지만 emails 가 비어 있습니다 — "
                     "topic·alarm 은 생기지만 수신자가 없어 콘솔에서만 보입니다. "
                     "구독은 콘솔/CLI 로 언제든 추가 가능합니다.")
        if al.emails:
            w.append("SNS email 구독은 apply 후 각 주소로 confirmation 메일이 가며, "
                     "수신자가 confirm 해야 발송이 시작됩니다 (자동화 불가).")
        if self.features.dev_login:
            w.append(
                "features.dev_login=true — 인증 없는 개발자 로그인 경로가 열립니다. "
                "인터넷 노출 배포라면 network.allowed_cidrs 로 접근을 반드시 제한하십시오."
            )
        if (self.clients.models_profile == "regional"
                and self.deploy.target == "eks"):
            w.append(
                "clients.models_profile 은 eks 경로에서 무시됩니다 — eks 의 Bedrock "
                "허용 ARN은 env tfvars 의 bedrock_allowed_model_arns + 차트 env "
                "values 의 modelsProfile 이 소유합니다 (infra.bedrock_allowed_model_arns "
                "로 managed overlay 를 쓸 수는 있습니다)."
            )
        if self.features.web_search:
            w.append("web_search 는 AgentCore(us-east-1) 의존 — apply 가 자동 "
                     "프로비저닝합니다(boto3 + us-east-1 agentcore/IAM 권한 필요). "
                     "과금은 ~$7/1,000 queries, idle ≈ $0 입니다.")
        if self.features.bi_insight and self.deploy.target in ("eks", "ecs"):
            w.append("bi_insight 는 AgentCore Runtime(ap-northeast-2 기본, "
                     "CHAT_AGENT_REGION 으로 변경) 의존 — apply 가 "
                     "module.agentcore_runtime(+irsa/task-role)을 만들고 "
                     "provision_agentcore_chatagent.py 로 Runtime 을 멱등 "
                     "생성합니다(boto3 agentcore 권한 + ECR 이미지 필요 — "
                     "없으면 docker/finch 빌드를 시도합니다). chat_reader DB "
                     "비밀번호도 프로비저너가 동기화하고, 스키마 임베딩 시드만 "
                     "수동 단계입니다 (docs/NDS/chat-agent.md).")
        if self.deploy.target == "eks":
            w.append("size_tier 는 eks 경로에서 문서용입니다 — 컴퓨트 크기는 "
                     "deploy.capacity_tier(밴드)와 deploy.sizing(개별 키)이 "
                     "정하고, 미선언 시 env values 가 그대로 유지됩니다.")
        if self.deploy.target == "eks" and not self.images.registry:
            w.append("images.registry 비어 있음 — apply 시 계정 기본 ECR 로 추론합니다.")
        if self.deploy.target == "eks" and self.deploy.sizing:
            # Redis 커넥션 예산 — pool_size 는 워커 프로세스당이다. WORKERS
            # 기본 2. 노드 maxclients 는 스키마가 모르므로 산식만 경고한다.
            # 실효값 = capacity_tier 밴드 + sizing 명시 키 병합 (render 와 동일).
            from .tiers import eks_capacity_sizing
            eff = eks_capacity_sizing(self)
            gp = eff.get("gateway-proxy") or {}
            rd = eff.get("redis") or {}
            pool = rd.get("pool_size")
            mx = gp.get("max_replicas")
            if pool and mx and pool * 2 * mx > 30_000:
                w.append(f"deploy.sizing: redis.pool_size({pool}) × WORKERS(2) × "
                         f"max_replicas({mx}) = {pool * 2 * mx} — 노드 maxclients "
                         f"예산을 확인하세요.")
        if self.deploy.target == "eks":
            uncovered = sorted(EKS_TAGGABLE_SERVICES - {"migration"} - set(self.images.tags))
            if self.images.tags and not self.images.tag and uncovered:
                w.append(f"images.tags 가 {uncovered} 를 덮지 않습니다 — env overlay 의 태그를 그대로 씁니다.")
        if self.deploy.target in ("eks", "compose") and not self.oidc.enabled:
            if self.features.dev_login:
                w.append("oidc 비활성 + features.dev_login=true — admin-ui 가 "
                         "서명 없는 dev 토큰으로 열립니다. 인터넷 노출 ALB 라면 "
                         "network.allowed_cidrs 로 접근을 제한하고 임시로만 쓰십시오.")
            else:
                w.append("oidc 비활성 + features.dev_login 미선언 — render 가 "
                         "DEV_LOGIN_ENABLED=false 로 강제해 작동하는 admin 로그인 "
                         "경로가 없습니다. domain 부착 + oidc(권장) 또는 "
                         "features.dev_login=true 를 선언하십시오.")
        elif (self.deploy.target == "eks" and self.oidc.enabled
              and not self.domain.name):
            # OIDC 는 배선되지만 Cognito hosted UI 는 https 콜백만 허용해
            # domain 없는 http ALB 로는 돌아올 수 없고, dev_login 도 oidc
            # 선언으로 강제 off — 어느 쪽으로도 관리자 로그인이 안 된다.
            w.append("oidc 선언 + domain.name 없음 — hosted UI 콜백은 https 만 "
                     "허용해 로그인 redirect 가 거절되고 dev_login 도 꺼집니다. "
                     "작동하는 admin 경로가 없으니 domain 을 선언하거나 oidc 를 "
                     "거두세요.")
        if (self.deploy.target == "eks" and len(self.env) > 18
                and (self.features.bi_insight or self.features.web_search)):
            # 프로비저너 명명에 env 가 박힌다 — llm-gateway-{env}-admin-chat-agent
            # 는 AgentCore 47자 제한(29+len(env)), websearch role 은 IAM 64자.
            w.append(f"env '{self.env}'({len(self.env)}자)가 길어 provisioner 이름이 "
                     "한계를 초과할 수 있습니다 — AgentCore Runtime 은 env ≤18자, "
                     "IAM role 은 env ≤24자까지 안전합니다 (env 를 짧게 하세요).")
        if self.deploy.target == "compose" and bool(self.images.tag) != bool(self.images.registry):
            w.append("compose render 는 images.registry + images.tag 둘 다 있어야 "
                     "사전 빌드 이미지를 씁니다 — 한쪽만 있으면 해당 선언이 무시되고 "
                     "소스에서 빌드합니다.")
        if self.deploy.target == "ecs" and not self.deploy.tfstate_table:
            w.append("deploy.tfstate_table 이 비어 있습니다 — terraform state 가 "
                     "locking 없이 저장됩니다 (동시 apply 가 state 를 깨뜨릴 수 있음). "
                     "DynamoDB lock table 을 지정하십시오.")
        if self.deploy.target == "ecs":
            # 정상 수요가 min_acu 바닥을 넘지만 스케일 헤드룸은 있는 경우 —
            # 저부하/cold-start 큐잉 가능성이라 경고 (천장 초과는 validate 에러)
            from .tiers import ecs_db_budget as _budget
            _b = _budget(self)
            if (_b and _b["floor_capacity"] < _b["ceiling_capacity"]
                    and _b["steady"] > _b["floor_capacity"]):
                w.append(
                    f"정상 pool 수요 {_b['steady']}개가 Aurora 최소 용량"
                    f"(min_acu ≈ {_b['floor_capacity']}conn)을 넘습니다 — "
                    "저부하/cold-start 구간에서 pool_timeout 큐잉이 생길 수 "
                    "있습니다. infra.db.min_acu 를 올리세요.")
        if self.deploy.target not in IMPLEMENTED_TARGETS:
            w.append(f"deploy.target={self.deploy.target} 의 렌더러는 아직 구현 전입니다 (validate 만 지원).")
        return w

    def validate(self) -> list[str]:
        e: list[str] = []
        if not self.env:
            e.append("env 는 필수입니다 (예: acme-small).")
        elif not _ENV_RE.fullmatch(self.env):
            e.append(f"env '{self.env}' 는 소문자/숫자/하이픈 2~31자여야 합니다.")
        if not _REGION_RE.fullmatch(self.aws.region):
            e.append(f"aws.region '{self.aws.region}' 이 리전 형식이 아닙니다 (예: ap-northeast-2).")
        if self.deploy.target not in DEPLOY_TARGETS:
            e.append(f"deploy.target 은 {DEPLOY_TARGETS} 중 하나여야 합니다: {self.deploy.target}")
        # 잘못된 tier 로 preset() 을 호출하면 KeyError 가 validate 를 빠져나가
        # 모아둔 오류 목록이 유실된다 — tier 의존 검사는 전부 이 플래그로 게이트.
        _tier_ok = self.deploy.size_tier in SIZE_TIERS
        if not _tier_ok:
            e.append(f"deploy.size_tier 는 {SIZE_TIERS} 중 하나여야 합니다: {self.deploy.size_tier}")
        if self.deploy.target == "compose" and self.deploy.size_tier != "t0":
            e.append(f"compose 형태는 최저 티어(t0)로만 배포 가능합니다 — "
                     f"테스트·평가용 단일 호스트입니다 "
                     f"(size_tier={self.deploy.size_tier!r} 지정됨).")
        if self.deploy.target == "eks":
            # 빈 release 면 ALB 발견의 instance 라벨 필터(!= release)가 전부
            # 통과해 같은 ns 의 외부 ingress ALB 에까지 알람을 단다.
            if not self.deploy.release.strip():
                e.append("deploy.release 가 비어 있습니다 — eks 의 ingress/ALB "
                         "발견이 release 라벨로 우리 차트만 골라내는 키라 "
                         "비어 있으면 안 됩니다.")
            if not self.deploy.namespace.strip():
                e.append("deploy.namespace 가 비어 있습니다 — eks 배포의 "
                         "kubectl/helm 범위를 정하는 키입니다.")
        # 이 값들은 tfvars/backend.hcl/helm args 에 f-string 으로 직접 박힌다 —
        # 따옴표·개행이 섞인 문자열은 var-file 주입(environment= 덮어쓰기)이나
        # HCL 파괴로 번져 다른 env 의 state/plan 에 손이 닿는다.
        for key, val in (("deploy.namespace", self.deploy.namespace),
                         ("deploy.release", self.deploy.release)):
            if val and not _K8S_NAME_RE.fullmatch(val):
                e.append(f"{key} '{val}' 은 소문자/숫자/하이픈 DNS-1123 이름이어야 "
                         "합니다 — tfvars/helm 인자로 그대로 쓰입니다.")
        if (self.deploy.tfstate_bucket
                and not _S3_BUCKET_RE.fullmatch(self.deploy.tfstate_bucket)):
            e.append(f"deploy.tfstate_bucket '{self.deploy.tfstate_bucket}' 이 "
                     "S3 버킷명 형식(소문자/숫자/.-, 3~63자)이 아닙니다.")
        if (self.deploy.tfstate_table
                and not _DDB_TABLE_RE.fullmatch(self.deploy.tfstate_table)):
            e.append(f"deploy.tfstate_table '{self.deploy.tfstate_table}' 이 "
                     "DynamoDB 테이블명 형식(3~255자)이 아닙니다.")
        al = self.features.alerting
        if self.deploy.target == "compose" and (al.enabled or al.emails or al.log_shipping):
            e.append("features.alerting 은 compose 전용이 아닙니다 — CloudWatch/SNS 는 "
                     "ecs|eks 배포에서만 생성됩니다.")
        if al.emails and not al.enabled:
            e.append("features.alerting.emails 는 enabled=true 일 때만 유효합니다 — "
                     "email 구독은 SNS topic 이 있어야 붙습니다.")
        for em in al.emails:
            if not _EMAIL_RE.fullmatch(em):
                e.append(f"features.alerting.emails '{em}' — 이메일 형식이 아닙니다 "
                         "(오타가 있으면 apply 중간에 SNS 구독 생성이 실패합니다).")
        # case-variant/중복 주소는 toset(for_each) 에 별개 구독으로 남아
        # 같은 메일박스에 알림·confirm 이 중복 도착한다 — 정규화+dedupe.
        al.emails = list(dict.fromkeys(em.lower() for em in al.emails))
        if al.log_shipping and self.deploy.target != "eks":
            e.append("features.alerting.log_shipping 은 eks(Fargate) 전용입니다 — "
                     "ecs 는 task definition 의 awslogs 드라이버 경로를 씁니다.")
        if self.deploy.target == "ecs" and self.deploy.size_tier not in ("t1", "t2"):
            e.append("ecs backend 는 size_tier=t1|t2 를 지원합니다 (t3 는 eks 경로).")
        if self.deploy.target == "eks" and self.deploy.size_tier not in ("t2", "t3"):
            e.append("eks backend 는 size_tier=t2|t3 입니다.")
        if self.deploy.size_tier == "t0" and self.deploy.target != "compose":
            e.append("size_tier=t0 는 compose backend 전용입니다.")
        if self.deploy.target in ("ecs", "eks") and not (self.images.tag or self.images.tags):
            e.append(f"deploy.target={self.deploy.target} 는 images.tag 또는 images.tags 명시가 필수입니다.")
        if self.images.tags:
            if self.deploy.target != "eks":
                e.append("images.tags(서비스별 태그)는 eks 경로만 지원합니다 — compose/ecs 는 단일 images.tag 입니다.")
            else:
                bad = sorted(set(self.images.tags) - EKS_TAGGABLE_SERVICES)
                if bad:
                    e.append(f"images.tags 에 허용되지 않은 서비스 키: {bad} "
                             f"(허용: {sorted(EKS_TAGGABLE_SERVICES)})")
        # registry 비우면 ecs 는 모듈이 ECR repo 를 만들고, eks 는 apply 가
        # <account>.dkr.ecr.<region> 으로 추론한다 (install-eks.sh 와 동일)
        if len(self.infra.azs) > 45:
            # 파생 서브넷의 세 번째 옥텟이 255 를 넘어 무효 CIDR 이 된다 —
            # elasticache 가 211+i 로 가장 빡빡하다(211+44=255).
            e.append("infra.azs 는 45개를 초과할 수 없습니다 — "
                     "파생 서브넷 규약이 /24 옥텟을 초과합니다")
        if self.infra.azs and len(self.infra.azs) < 2:
            e.append("infra.azs 는 2개 이상이어야 합니다 — "
                     "DB/ElastiCache subnet group 이 AZ 2개를 요구합니다.")
        for az in self.infra.azs:
            if not az.startswith(self.aws.region):
                e.append(f"infra.azs '{az}' 가 aws.region '{self.aws.region}' 의 "
                         "AZ 가 아닙니다 (로컬존은 지원하지 않습니다).")
        for g in self.infra.cognito_groups:
            if g != "ClaudeAdmin" and not g.startswith("Claude_"):
                e.append(f"infra.cognito_groups '{g}' — 'Claude_' 접두가 필수입니다 "
                         "(접두 없는 그룹은 OIDC 로그인이 거부됩니다).")
        # infra.db / infra.cache — ecs 전용 세부 override (tier 기본값 위에 얹는다)
        db, cache = self.infra.db, self.infra.cache
        if db.mode and db.mode not in ("serverless", "provisioned"):
            e.append(f"infra.db.mode 는 serverless|provisioned 여야 합니다: {db.mode!r}")
        if db.min_acu is not None and not (0 <= db.min_acu <= 256):
            e.append(f"infra.db.min_acu 범위는 0~256: {db.min_acu}")
        if db.max_acu is not None and not (0.5 <= db.max_acu <= 256):
            e.append(f"infra.db.max_acu 범위는 0.5~256: {db.max_acu}")
        if (db.min_acu is not None and db.max_acu is not None
                and db.min_acu > db.max_acu):
            e.append(f"infra.db.min_acu({db.min_acu}) > max_acu({db.max_acu}) 입니다.")
        if cache.mode and cache.mode not in ("single", "replicated", "cluster"):
            e.append(f"infra.cache.mode 는 single|replicated|cluster 여야 합니다: {cache.mode!r}")
        if cache.num_clusters is not None and not (1 <= cache.num_clusters <= 6):
            e.append(f"infra.cache.num_clusters 범위는 1~6: {cache.num_clusters}")
        if cache.num_node_groups is not None and not (1 <= cache.num_node_groups <= 90):
            e.append(f"infra.cache.num_node_groups 범위는 1~90: {cache.num_node_groups}")
        if cache.replicas_per_node_group is not None and not (1 <= cache.replicas_per_node_group <= 5):
            e.append(f"infra.cache.replicas_per_node_group 범위는 1~5: {cache.replicas_per_node_group}")
        # 토폴로지 키가 mode 와 어긋나면 terraform 변수가 조용히 무시된다 — 경고로 노출.
        # tier 가 잘못되면 preset() 자체가 KeyError 라 검사 자체를 건너뛴다.
        from .tiers import preset as _preset
        _t = _preset(self.deploy.size_tier) if _tier_ok else None
        if _t is not None:
            _tmode = ("single" if _t.cache_mode == "elasticache-single"
                      else "cluster" if _t.cache_spec.get("cluster_mode")
                      else "replicated")
            _eff_mode = cache.mode or _tmode
            if _eff_mode == "cluster":
                if cache.num_clusters is not None:
                    e.append("infra.cache.num_clusters 는 cluster mode 에서 무시됩니다 "
                             "— num_node_groups/replicas_per_node_group 을 쓰세요.")
            elif cache.num_node_groups is not None or cache.replicas_per_node_group is not None:
                e.append(f"infra.cache.num_node_groups/replicas_per_node_group 은 cluster "
                         f"mode 전용입니다 (effective mode={_eff_mode!r}).")
            # replicated 에서 num_clusters=1 은 모듈이 max(1,2) 로 조용히 올린다 —
            # 선언과 실제가 갈리는 것을 차단
            if _eff_mode == "replicated" and cache.num_clusters == 1:
                e.append("infra.cache.num_clusters=1 은 replicated 와 맞지 않습니다 "
                         "(모듈이 최소 2 로 강제합니다) — 2 이상이나 mode=single 을 쓰세요.")
        if (db.mode or db.min_acu is not None or db.max_acu is not None
                or db.instance_class or db.engine_version
                or cache.mode or cache.node_type
                or cache.num_clusters is not None or cache.num_node_groups is not None
                or cache.replicas_per_node_group is not None):
            if self.deploy.target != "ecs":
                e.append(f"infra.db/infra.cache 는 ecs 경로에서만 적용됩니다 "
                         f"(deploy.target={self.deploy.target}) — eks 는 env tfvars 소유.")
        if self.network.mode not in NETWORK_MODES:
            e.append(f"network.mode 는 {NETWORK_MODES} 중 하나여야 합니다.")
        if self.domain.mode not in DOMAIN_MODES:
            e.append(f"domain.mode 은 {DOMAIN_MODES} 중 하나여야 합니다.")
        if self.domain.mode == "route53-acm" and not self.domain.name:
            e.append("domain.mode=route53-acm 이면 domain.name 이 필수입니다.")
        # 캡처/수작업 입력의 쓰레기 값이 ingress host·ACM·Route53 으로 번지는 걸
        # 선언 단계에서 차단 — Caddyfile 파생 이름(후행 콤마 등)이 대표 사례.
        if self.domain.name and not _HOSTNAME_RE.fullmatch(self.domain.name):
            e.append(f"domain.name '{self.domain.name}' 이 hostname 형식이 아닙니다.")
        if self.domain.zone_id and not re.fullmatch(r"Z[0-9A-Z]+", self.domain.zone_id):
            e.append(f"domain.zone_id '{self.domain.zone_id}' 이 hosted zone ID "
                     "형식(Z...)이 아닙니다.")
        if (self.aws.account_id
                and not re.fullmatch(r"\d{12}", self.aws.account_id)):
            e.append(f"aws.account_id '{self.aws.account_id}' 는 12자리 숫자여야 합니다.")
        if (self.domain.mode == "route53-acm"
                and self.deploy.target == "ecs"
                and not self.domain.zone_id):
            e.append("ecs 의 route53-acm 은 domain.zone_id(hosted zone)가 필수입니다 — "
                     "ACM 검증·레코드 생성에 필요. eks 는 env values 의 기존 인증서를 씁니다.")
        # 비-int sizing 값은 위에서 오류로 잡히지만 dict 에 남아 있다 —
        # 병합 검사/DB 예산이 그 값으로 int()/비교를 하면 TypeError/ValueError 가
        # validate 를 빠져나가므로 아래 계산형 검사는 이 플래그로 게이트한다.
        _sizing_ints_ok = True
        if self.deploy.sizing:
            is_eks = self.deploy.target == "eks"
            allowed_svc = EKS_SIZING if is_eks else {s: _ECS_SIZING_KEYS for s in ECS_SERVICES}
            # 비-문자열 키({5: ...})는 sorted 가 TypeError 로 죽는다 —
            # errors 에 담기지 않고 traceback 으로 나간다. key=str 로 정렬.
            unknown = sorted(set(self.deploy.sizing) - set(allowed_svc),
                             key=str)
            if unknown:
                e.append(f"deploy.sizing 에 알 수 없는 서비스: {unknown} "
                         f"(허용: {sorted(allowed_svc)})")
                _sizing_ints_ok = False
            for svc, ov in self.deploy.sizing.items():
                if svc not in allowed_svc:
                    # 위의 "알 수 없는 서비스" 오류로 충분 — 키 검사까지 돌면
                    # "(허용: [])" 같은 두 번째 오류가 붙어 혼란스럽다(P2-6).
                    continue
                if not isinstance(ov, dict):
                    e.append(f"deploy.sizing.{svc} 는 mapping 이어야 합니다: {ov!r}")
                    _sizing_ints_ok = False
                    continue
                keys = allowed_svc.get(svc, frozenset())
                bad = sorted(set(ov) - keys, key=str)
                if bad:
                    e.append(f"deploy.sizing.{svc} 에 알 수 없는 키: {bad} "
                             f"(허용: {sorted(keys)})")
                    _sizing_ints_ok = False
                    continue
                for k, val in ov.items():
                    is_int = isinstance(val, int) and not isinstance(val, bool)
                    bounds = EKS_DB_POOL_BOUNDS.get(k) if is_eks else None
                    if bounds:
                        lo, hi = bounds
                        if not is_int or not lo <= val <= hi:
                            e.append(f"deploy.sizing.{svc}.{k} 는 {lo}..{hi} 범위의 "
                                     f"정수여야 합니다: {val!r}")
                            _sizing_ints_ok = False
                    elif not is_int or val <= 0:
                        e.append(f"deploy.sizing.{svc}.{k} 는 양의 정수여야 합니다: {val!r}")
                        _sizing_ints_ok = False
            if self.deploy.target not in ("ecs", "eks"):
                e.append(f"deploy.sizing 은 ecs/eks 경로에서만 적용됩니다 "
                         f"(deploy.target={self.deploy.target}).")
            elif is_eks:
                # Redis 커넥션 예산은 차단이 아니라 권고 — warnings() 쪽에서 계산
                pass
        # 병합 결과의 min ≤ max — sizing 에 min 만 있고 max 는 env overlay 값을
        # 물려받는 조합도 잡는다 (render/eks.py 도 최종 병합 후 한 번 더 검사).
        from .tiers import EKS_CAPACITY_TIERS as _BANDS
        _bad_tier = (self.deploy.capacity_tier
                     and self.deploy.capacity_tier not in _BANDS)
        if (self.deploy.target == "eks" and _tier_ok and not _bad_tier
                and _sizing_ints_ok
                and (self.deploy.sizing or self.deploy.capacity_tier)):
            from .tiers import eks_capacity_sizing
            _gp = eks_capacity_sizing(self).get("gateway-proxy") or {}
            _mn, _mx = _gp.get("min_replicas"), _gp.get("max_replicas")
            if _mn is not None and _mx is not None and _mn > _mx:
                e.append(f"deploy.sizing/capacity_tier 병합 결과 "
                         f"min_replicas({_mn}) > max_replicas({_mx}) — "
                         f"함께 선언하세요 (env overlay 기본값과의 합성은 "
                         f"render 에서 최종 검증됩니다)")
        if (self.deploy.target == "ecs" and _tier_ok and not _bad_tier
                and _sizing_ints_ok
                and (self.deploy.sizing or self.deploy.capacity_tier)):
            from .tiers import ecs_service_sizing
            for _svc, _s in ecs_service_sizing(self).items():
                _mn = int(_s.get("min_count") or 0)
                _mx = int(_s.get("max_count") or 0)
                _d = int(_s.get("desired_count") or 0)
                if _mx and _mn > _mx:
                    e.append(f"deploy.sizing.{_svc}: min_count({_mn}) > "
                             f"max_count({_mx})")
                elif _mx and _d > _mx:
                    e.append(f"deploy.sizing.{_svc}: desired_count({_d}) > "
                             f"max_count({_mx}) — 오토스케일 상한 밖의 desired "
                             f"는 스케일링이 되돌립니다")
        if self.deploy.capacity_tier:
            # 밴드 전개는 target 마다: eks → pod min/max, ecs → task desired/max.
            # compose 는 단일호스트 — 밴드를 적용할 복제본 축이 없어 거부한다.
            if self.deploy.target not in ("eks", "ecs"):
                e.append(f"deploy.capacity_tier 는 적용되지 않습니다 — "
                         f"compose 형태는 최저 티어(t0)로만 배포 가능합니다 "
                         f"(테스트·평가용 단일 호스트).")
            else:
                from .tiers import EKS_CAPACITY_TIERS
                if self.deploy.capacity_tier not in EKS_CAPACITY_TIERS:
                    e.append(f"deploy.capacity_tier 가 목록에 없습니다: "
                             f"{self.deploy.capacity_tier!r} "
                             f"(허용: {sorted(EKS_CAPACITY_TIERS)})")
        if self.deploy.target == "ecs" and _tier_ok and _sizing_ints_ok:
            # 오토스케일(max_count)을 포함한 worst-case pool 수요가 Aurora 천장을
            # 넘으면 스케일아웃이 DB 를 고갈시킨다 — 선언 단계에서 막는다.
            from .tiers import ecs_db_budget
            b = ecs_db_budget(self)
            if b and b["worst"] > int(b["ceiling_capacity"] * 0.8):
                e.append(
                    f"DB 커넥션 예산 초과: worst-case 수요 {b['worst']}개 "
                    f"(pool 10+overflow 20 × 프로세스 × desired/max_count 합산)가 "
                    f"Aurora 최대 용량 {b['ceiling_capacity']}개의 80%를 넘습니다. "
                    f"deploy.sizing 의 max_count/desired_count 를 낮추거나 "
                    f"infra.db.max_acu(또는 tier) 를 올리세요.")
            elif (b and b["steady"] > b["floor_capacity"]
                    and b["floor_capacity"] >= b["ceiling_capacity"]):
                # floor == ceiling(provisioned 또는 min_acu=max_acu)이면 바닥
                # 초과가 곧 천장 초과 — 경고가 아니라 에러. 그 외의 바닥 초과는
                # warnings() 에서 경고한다 (w 는 이 메서드에 없다).
                e.append(
                    f"정상 pool 수요 {b['steady']}개(pool_size 기준)가 Aurora 용량"
                    f"(≈ {b['floor_capacity']}conn)을 넘습니다 — "
                    f"min_acu/instance_class 를 올리거나 task 수를 줄이세요.")
        if self.features.notifications.provider not in NOTIFICATION_PROVIDERS:
            e.append(f"notifications.provider 는 {NOTIFICATION_PROVIDERS} 중 하나여야 합니다.")
        if self.features.notifications.provider == "ses" and not self.features.notifications.ses_from:
            e.append("notifications.provider=ses 이면 notifications.ses_from 이 필요합니다.")
        if self.features.notifications.provider == "smtp" and not self.features.notifications.smtp_host:
            e.append("notifications.provider=smtp 이면 notifications.smtp_host 가 필요합니다.")
        if (self.features.notifications.provider == "internal_api"
                and not self.features.notifications.internal_api_url):
            e.append("notifications.provider=internal_api 이면 "
                     "notifications.internal_api_url 이 필요합니다 — 비우면 worker 가 "
                     "EMAIL_API_URL 부재로 기동 실패(crashloop)합니다.")
        if self.features.bi_insight and self.deploy.target == "compose":
            e.append("bi_insight(AgentCore Runtime)는 compose backend에서 지원하지 않습니다.")
        if self.domain.mode == "selfsigned-temp" and self.deploy.target != "eks":
            e.append("domain.mode=selfsigned-temp 는 EKS(ALB ingress certificate-arn) "
                     "전용입니다 — ecs/compose 에서는 지원하지 않습니다")
        if self.domain.mode == "cloudfront-temp" and self.deploy.target in (
                "compose", "eks"):
            e.append("domain.mode=cloudfront-temp 는 아직 배포 경로가 구현되지 "
                     "않았습니다 — none 또는 route53-acm (eks 는 selfsigned-temp "
                     "도 가능)을 쓰십시오.")
        for c in self.network.allowed_cidrs:
            if not isinstance(c, str):
                e.append(f"allowed_cidrs 항목은 문자열이어야 합니다: {c!r}")
                continue
            try:
                ipaddress.ip_network(c, strict=False)
            except ValueError:
                e.append(f"allowed_cidrs 항목이 유효한 CIDR 가 아닙니다: {c!r}")
        if self.infra.vpc_cidr:
            try:
                net = ipaddress.ip_network(self.infra.vpc_cidr, strict=False)
                # 서브넷 파생 규약은 IPv4 a.b.x.y/24 전용 — IPv6 는
                # subnet_cidrs 의 subnet_of 비교가 TypeError 로 죽는다.
                if net.version != 4:
                    e.append(f"infra.vpc_cidr 는 IPv4 여야 합니다: "
                             f"{self.infra.vpc_cidr!r}")
            except ValueError:
                e.append(f"infra.vpc_cidr 가 유효한 CIDR 가 아닙니다: "
                         f"{self.infra.vpc_cidr!r} (예: 10.0.0.0/16)")
        sfx = self.infra.cognito_domain_suffix
        if sfx and not re.fullmatch(r"[a-z0-9-]+", sfx):
            e.append(f"infra.cognito_domain_suffix 는 소문자/숫자/하이픈만 "
                     f"가능합니다: {sfx!r}")
        ev = self.infra.eks_version
        if ev and not isinstance(ev, str):
            # eks_version: 1.30 을 따옴표 없이 쓰면 yaml 이 float 1.3 으로 읽어
            # minor 30 → 3 으로 왜곡된다 — 문자열 강제로 차단한다.
            e.append(f'infra.eks_version 은 문자열이어야 합니다 — '
                     f'eks_version: "{ev}" 처럼 따옴표로 감싸세요')
        elif ev and not re.fullmatch(r"1\.\d+", ev):
            e.append(f"infra.eks_version 은 '1.<minor>' 형식이어야 합니다: {ev!r}")
        if self.clients.models_profile not in MODEL_PROFILES:
            e.append(f"clients.models_profile 은 {MODEL_PROFILES} 중 하나여야 합니다.")
        bn = self.branding.name
        if len(bn) > MAX_BRAND_NAME_LENGTH:
            e.append(f"branding.name 은 {MAX_BRAND_NAME_LENGTH}자 이하여야 합니다 "
                     f"(현재 {len(bn)}자) — 사이드바 폭을 넘깁니다.")
        # '$' 는 compose 의 ${VAR} 치환·HCL 의 ${...} 보간으로 해석돼 렌더 결과가
        # 조용히 바뀐다. 제어문자(개행 등)는 env 한 줄 값을 깨뜨린다.
        if "$" in bn or re.search(r"[\x00-\x1f\x7f]", bn):
            e.append(f"branding.name 에 '$' 나 제어문자(개행 등)는 쓸 수 없습니다: {bn!r}")
        if self.oidc.enabled:
            if not self.oidc.client_id:
                e.append("oidc.issuer_url 을 채웠으면 admin-ui SSO 용 oidc.client_id 도 필요합니다.")
            if not (self.oidc.authorize_url and self.oidc.token_url):
                e.append("oidc 를 쓰려면 authorize_url / token_url 도 필요합니다 (hosted-ui 엔드포인트).")
        if self.oidc.vk_ttl_hours < 0:
            e.append("oidc.vk_ttl_hours 는 0 이상이어야 합니다 (0=차트 기본).")
        _oa = self.observability.alerting
        if _oa.enabled and self.deploy.target != "eks":
            e.append("observability.alerting.enabled 는 eks 전용입니다 (kube-prometheus-stack "
                     f"Alertmanager) — target={self.deploy.target!r}.")
        if _oa.slack_secret_name and not _K8S_NAME_RE.fullmatch(_oa.slack_secret_name):
            e.append("observability.alerting.slack_secret_name 은 k8s 리소스 이름이어야 합니다 "
                     f"(소문자·숫자·'-'·'.', 최대 253자): {_oa.slack_secret_name!r}")
        return e


def _safe_int(v: Any, errors: list[str], path: str) -> int:
    # bool 은 int 의 서브클래스라 int(True)=1 이 통과된다 — min_acu: true 가
    # 1.0 으로 새는 걸 차단. 비정수 float(587.9)의 침묵 절삭도 거부한다.
    if isinstance(v, bool):
        errors.append(f"{path} 는 정수여야 합니다: {v!r}")
        return 0
    if isinstance(v, float) and not v.is_integer():
        errors.append(f"{path} 는 정수여야 합니다(소수점 값 불가): {v!r}")
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        errors.append(f"{path} 는 정수여야 합니다: {v!r}")
        return 0


def _safe_int_or_none(v: Any, errors: list[str], path: str) -> int | None:
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        errors.append(f"{path} 는 정수여야 합니다: {v!r}")
        return None
    if isinstance(v, float) and not v.is_integer():
        errors.append(f"{path} 는 정수여야 합니다(소수점 값 불가): {v!r}")
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        errors.append(f"{path} 는 정수여야 합니다: {v!r}")
        return None


def _safe_float_or_none(v: Any, errors: list[str], path: str) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        errors.append(f"{path} 는 숫자여야 합니다: {v!r}")
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        errors.append(f"{path} 는 숫자여야 합니다: {v!r}")
        return None


def _opt_str(v: object, dflt: str = "") -> str:
    """null 을 기본값으로 — `key: null` 을 str(None)="None" 으로 코어션해
    필수값 검사를 통과시키고 CLIENT_ID="None" 같은 값이 배포되는 사고 방지.
    false/0 같은 다른 falsy 도 코어션하지 않는다 — target: false 가
    조용히 compose 배포가 되는 걸 막고 검증에서 거절시킨다."""
    return dflt if v is None else str(v)


def _safe_bool(v: Any, errors: list[str], path: str, default: bool = False) -> bool:
    """yaml 의 unquoted true/false/on/off 는 이미 bool 로 온다. 인용된 문자열
    ("false")은 bool() 이 True 로 평가해 플래그가 조용히 켜지므로 명시적으로 거절한다.
    int 0/1 은 허용."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    errors.append(f"{path} 는 true/false 여야 합니다: {v!r}")
    return default


def _port_val(v: Any, dflt: int, path: str, errors: list[str]) -> int:
    if v in (None, ""):
        return dflt
    try:
        if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
            raise ValueError
        n = int(v)
        if not (1 <= n <= 65535):
            raise ValueError
        return n
    except (TypeError, ValueError):
        errors.append(f"{path} 는 1-65535 정수 포트여야 합니다: {v!r}")
        return dflt


def _ports_from(v: Any, errors: list[str]) -> "PortsConfig":
    m = _require_mapping(v, "network.ports", errors)
    if not m:
        return PortsConfig()
    _check_unknown(m, frozenset({"http", "https", "gateway", "admin_api", "admin_ui"}),
                   "network.ports", errors)
    pc = PortsConfig(
        http=_port_val(m.get("http"), 80, "network.ports.http", errors),
        https=_port_val(m.get("https"), 443, "network.ports.https", errors),
        gateway=_port_val(m.get("gateway"), 8000, "network.ports.gateway", errors),
        admin_api=_port_val(m.get("admin_api"), 8080, "network.ports.admin_api", errors),
        admin_ui=_port_val(m.get("admin_ui"), 3000, "network.ports.admin_ui", errors),
    )
    # 같은 그룹 안 호스트 포트 중복은 caddy 바인드 시 확정 실패 — 그룹 간
    # 중복(http vs gateway 등)은 mode 에 따라 한쪽만 바인드돼 무해
    for group in (("http", "https"), ("gateway", "admin_api", "admin_ui")):
        seen: dict[int, str] = {}
        for k in group:
            n = getattr(pc, k)
            if n in seen:
                errors.append(f"network.ports.{k}={n} 이(가) "
                              f"network.ports.{seen[n]} 와 충돌합니다")
            else:
                seen[n] = k
    return pc


def _require_mapping(node: Any, path: str, errors: list[str]) -> dict:
    if node is None:
        return {}
    if not isinstance(node, dict):
        errors.append(f"{path} 는 mapping 이어야 합니다.")
        return {}
    return node


def _check_unknown(node: dict, allowed: frozenset, path: str, errors: list[str]) -> None:
    """스키마에 없는 키 거부 — 오타(iamges: 등)가 기본값으로 침묵 적용되는 걸 막는다.
    allowed_cidrs 오타가 방화벽을 여는 배포 사고와 같은 부류라 경고가 아니라 오류."""
    for k in node:
        if k not in allowed:
            errors.append(f"{path}.{k} 는 알 수 없는 키입니다 (허용: {sorted(allowed)})")


_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_REGION_RE = re.compile(r"^[a-z]{2}(-gov|-iso|-isob)?-[a-z]+-\d+$")
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_S3_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_DDB_TABLE_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,255}$")
_BEDROCK_ARN_RE = re.compile(r"^arn:aws[a-z-]*:bedrock:.+$")


def _str_items(v: Any, path: str, errors: list[str],
               item_re: re.Pattern | None = None,
               item_desc: str = "문자열") -> list[str]:
    """str 리스트 필드 — 스칼라 str 은 1-항목 리스트로 감싼다. list 가 아니거나
    항목이 str/형식에 맞지 않으면 errors 적재 후 제외한다 (dict 순회가 키 목록을
    만들거나 None 이 "None" 이 되는 침묵 오용 방지)."""
    if v is None:
        return []
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list):
        errors.append(f"{path} 는 list 여야 합니다: {type(v).__name__}")
        return []
    out: list[str] = []
    for x in items:
        if not isinstance(x, str) or (item_re is not None and not item_re.fullmatch(x)):
            errors.append(f"{path} 항목은 {item_desc} 이어야 합니다: {x!r}")
        else:
            out.append(x)
    return out


_DOCKER_TAG_RE = re.compile(r"^[\w][\w.-]{0,127}$")


def _image_tags(v: Any, errors: list[str]) -> dict[str, str]:
    """images.tags — 서비스→태그 매핑. str(v) 코어션은 ~ → "None",
    false → "False", dict → "{'x':1}" 같은 가짜 태그를 만든다 —
    docker 태그 형식으로 검증해 잘못된 값은 거부한다."""
    raw = _require_mapping(v, "images.tags", errors)
    out: dict[str, str] = {}
    for k, val in raw.items():
        ks = _opt_str(k)
        if not isinstance(val, str) or not _DOCKER_TAG_RE.fullmatch(val):
            errors.append(
                f"images.tags.{ks} 는 docker 태그 문자열이어야 합니다: {val!r}")
            continue
        out[ks] = val
    return out


_TOP_LEVEL_KEYS = frozenset((
    "version", "env", "aws", "deploy", "network", "domain",
    "features", "images", "clients", "oidc", "infra", "branding",
    "observability",
))
_OBSERVABILITY_KEYS = frozenset(("alerting",))
_OBS_ALERTING_KEYS = frozenset(("enabled", "slack_secret_name"))
_BIZAL_KEYS = frozenset(("enabled", "reconciliation_mismatch",
                       "dead_letter_growth", "pricing_missing", "slack"))
_K8S_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
_AWS_KEYS = frozenset(("region", "account_id", "allowed_roles"))
_DEPLOY_KEYS = frozenset(("target", "size_tier", "sizing", "capacity_tier",
                          "tfstate_bucket", "tfstate_table", "release",
                          "namespace", "tf_env_dir"))
_NETWORK_KEYS = frozenset(("mode", "allowed_cidrs", "admin_internal", "ports"))
_DOMAIN_KEYS = frozenset(("mode", "name", "zone_id", "hosts"))
_DOMAIN_HOST_KEYS = frozenset(("gateway", "admin_ui", "admin_api"))
_FEATURE_KEYS = frozenset(("notifications", "alerting", "bi_insight", "pricing_lambda",
                           "web_search", "body_logging", "observability",
                           "dev_login", "service_keys",
    "business_alerts",))
_ALERTING_KEYS = frozenset(("enabled", "emails", "log_shipping"))
_NOTIF_KEYS = frozenset(("provider", "smtp_host", "smtp_port", "smtp_from",
                         "ses_from", "sender_name",
                         "internal_api_url", "internal_api_from",
                         "smtp_credentials_secret"))
_IMAGE_KEYS = frozenset(("registry", "tag", "tags"))
_CLIENT_KEYS = frozenset(("models_profile",))
_BRANDING_KEYS = frozenset(("name", "managed_login"))
_OIDC_KEYS = frozenset(("issuer_url", "audience", "client_id", "client_secret",
                        "authorize_url", "token_url", "provider_name", "required_group",
                        "vk_ttl_hours", "groups_claim", "group_prefix",
                        "reject_unmatched_groups", "admin_groups", "admin_emails"))
_INFRA_KEYS = frozenset(("azs", "vpc_cidr", "cognito_domain_suffix", "db", "cache",
                        "cognito_groups", "bedrock_allowed_model_arns",
                        "mantle_regions", "eks_version"))


def from_dict(raw: dict) -> GatewayConfig:
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise SchemaError("gateway.yaml 최상위는 mapping 이어야 합니다.")
    version = raw.get("version", SCHEMA_VERSION)
    # 1.0 == 1 이 True 라 float/string "1" 도 통과한다 — 타입까지 본다.
    if not isinstance(version, int) or isinstance(version, bool) \
            or version != SCHEMA_VERSION:
        errors.append(f"version 은 정수 {SCHEMA_VERSION} 이어야 합니다 (got {version!r}).")

    aws = _require_mapping(raw.get("aws"), "aws", errors)
    deploy = _require_mapping(raw.get("deploy"), "deploy", errors)
    network = _require_mapping(raw.get("network"), "network", errors)
    domain = _require_mapping(raw.get("domain"), "domain", errors)
    features = _require_mapping(raw.get("features"), "features", errors)
    notif = _require_mapping(features.get("notifications"), "features.notifications", errors)
    alerting = _require_mapping(features.get("alerting"), "features.alerting", errors)
    bizal = _require_mapping(features.get("business_alerts"), "features.business_alerts", errors)
    images = _require_mapping(raw.get("images"), "images", errors)
    clients = _require_mapping(raw.get("clients"), "clients", errors)
    branding = _require_mapping(raw.get("branding"), "branding", errors)
    oidc = _require_mapping(raw.get("oidc"), "oidc", errors)
    infra = _require_mapping(raw.get("infra"), "infra", errors)
    _obs = _require_mapping(raw.get("observability"), "observability", errors)
    _obs_alert = _require_mapping(_obs.get("alerting"), "observability.alerting", errors)

    _check_unknown(raw, _TOP_LEVEL_KEYS, "gateway.yaml", errors)
    _check_unknown(_obs, _OBSERVABILITY_KEYS, "observability", errors)
    _check_unknown(_obs_alert, _OBS_ALERTING_KEYS, "observability.alerting", errors)
    _slack_secret = _obs_alert.get("slack_secret_name")
    if _slack_secret is not None and not isinstance(_slack_secret, str):
        errors.append("observability.alerting.slack_secret_name 은 문자열이어야 합니다: "
                      f"{_slack_secret!r}")
        _slack_secret = ""
    _check_unknown(aws, _AWS_KEYS, "aws", errors)
    _check_unknown(deploy, _DEPLOY_KEYS, "deploy", errors)
    _check_unknown(network, _NETWORK_KEYS, "network", errors)
    _check_unknown(domain, _DOMAIN_KEYS, "domain", errors)
    _domain_hosts = domain.get("hosts") if isinstance(domain.get("hosts"), dict) else {}
    if domain.get("hosts") is not None and not isinstance(domain.get("hosts"), dict):
        errors.append("domain.hosts 는 mapping 이어야 합니다")
    _check_unknown(_domain_hosts, _DOMAIN_HOST_KEYS, "domain.hosts", errors)
    for _hk, _hv in _domain_hosts.items():
        # null/str(None) 같은 값은 "None" 리터럴 호스트로 렌더돼 ingress 를
        # 깨뜨리므로 형식 검증 — 소문자 RFC1123 hostname, 스킴/경로 불가.
        if not isinstance(_hv, str) or not _HOSTNAME_RE.fullmatch(_hv):
            errors.append(
                f"domain.hosts.{_hk} 는 소문자 hostname 이어야 합니다: {_hv!r}")
    _check_unknown(features, _FEATURE_KEYS, "features", errors)
    _check_unknown(notif, _NOTIF_KEYS, "features.notifications", errors)
    _check_unknown(alerting, _ALERTING_KEYS, "features.alerting", errors)
    _check_unknown(bizal, _BIZAL_KEYS, "features.business_alerts", errors)
    _check_unknown(images, _IMAGE_KEYS, "images", errors)
    _check_unknown(clients, _CLIENT_KEYS, "clients", errors)
    _check_unknown(branding, _BRANDING_KEYS, "branding", errors)
    _brand_raw = branding.get("name")
    if _brand_raw is not None and not isinstance(_brand_raw, str):
        errors.append(f"branding.name 은 문자열이어야 합니다: {_brand_raw!r}")
    _check_unknown(oidc, _OIDC_KEYS, "oidc", errors)
    _check_unknown(infra, _INFRA_KEYS, "infra", errors)
    _infra_db = infra.get("db") if isinstance(infra.get("db"), dict) else {}
    _infra_cache = infra.get("cache") if isinstance(infra.get("cache"), dict) else {}
    if infra.get("db") is not None and not _infra_db:
        errors.append("infra.db 는 mapping 이어야 합니다")
    if infra.get("cache") is not None and not _infra_cache:
        errors.append("infra.cache 는 mapping 이어야 합니다")
    _check_unknown(_infra_db, ("mode", "min_acu", "max_acu",
                               "instance_class", "engine_version"), "infra.db", errors)
    _check_unknown(_infra_cache, ("mode", "node_type", "num_clusters",
                                  "num_node_groups",
                                  "replicas_per_node_group"), "infra.cache", errors)

    cfg = GatewayConfig(
        env=_opt_str(raw.get("env"), ""),
        aws=AwsConfig(
            region=_opt_str(aws.get("region"), "ap-northeast-2"),
            account_id=_opt_str(aws.get("account_id"), ""),
            # 스칼라("DevRole")는 1-항목 리스트로 감싼다 — int/dict 같은
            # 비-list 는 순회 불가/키 순회 침묵오용이라 명시 오류로 둔다.
            # 콤마는 금지 — ALLOWED_IAM_ROLES 가 ",".join() 이라 항목 안
            # 콤마가 두 role 로 갈라져 허가 목록이 조용히 넓어진다.
            allowed_roles=_str_items(
                aws.get("allowed_roles"), "aws.allowed_roles", errors,
                item_re=re.compile(r"^[a-zA-Z0-9+=.@_/*:-]+$")),
        ),
        deploy=DeployConfig(
            # `or` 패턴 — yaml null(`release:`)도 기본값으로. `str(None)` 은
            # "None" 리터럴이 돼 release/namespace 이름이 쓰레기가 된다
            target=_opt_str(deploy.get("target"), "compose"),
            size_tier=_opt_str(deploy.get("size_tier"), "t1"),
            # dict("x")/dict([1]) 같은 비-mapping 은 TypeError/ValueError 로
            # 날아든다 — 스키마 오류로 변환해야 CLI 가 깔끔하게 거절한다.
            sizing=_require_mapping(deploy.get("sizing"), "deploy.sizing", errors),
            capacity_tier=_opt_str(deploy.get("capacity_tier"), ""),
            tfstate_bucket=_opt_str(deploy.get("tfstate_bucket"), ""),
            tfstate_table=_opt_str(deploy.get("tfstate_table"), ""),
            release=_opt_str(deploy.get("release"), "llm-gateway"),
            namespace=_opt_str(deploy.get("namespace"), "llm-gateway"),
            tf_env_dir=_opt_str(deploy.get("tf_env_dir"), ""),
        ),
        network=NetworkConfig(
            mode=_opt_str(network.get("mode"), "public"),
            # 스칼라 str 은 1-항목으로 감싼다 — int/dict 비-list 는 침묵 순회
            # (dict 는 키 목록이 CIDR 로 들어감)가 아니라 명시 오류.
            allowed_cidrs=_str_items(network.get("allowed_cidrs"),
                                     "network.allowed_cidrs", errors),
            admin_internal=_safe_bool(network.get("admin_internal", False), errors,
                                      "network.admin_internal"),
            ports=_ports_from(network.get("ports"), errors),
        ),
        domain=DomainConfig(
            mode=_opt_str(domain.get("mode"), "none"),
            name=_opt_str(domain.get("name"), ""),
            zone_id=_opt_str(domain.get("zone_id"), ""),
            hosts={str(k): str(v) for k, v in _domain_hosts.items()},
        ),
        features=FeaturesConfig(
            notifications=NotificationConfig(
                provider=_opt_str(notif.get("provider"), "mock"),
                smtp_host=_opt_str(notif.get("smtp_host"), ""),
                smtp_port=_safe_int(notif.get("smtp_port", 587), errors,
                                    "features.notifications.smtp_port"),
                smtp_from=_opt_str(notif.get("smtp_from"), ""),
                ses_from=_opt_str(notif.get("ses_from"), ""),
                sender_name=_opt_str(notif.get("sender_name"), ""),
                internal_api_url=_opt_str(notif.get("internal_api_url"), ""),
                internal_api_from=_opt_str(notif.get("internal_api_from"), ""),
                smtp_credentials_secret=_opt_str(notif.get("smtp_credentials_secret"), ""),
            ),
            alerting=AlertingConfig(
                enabled=_safe_bool(alerting.get("enabled", False), errors,
                                   "features.alerting.enabled"),
                emails=_str_items(alerting.get("emails"),
                                  "features.alerting.emails", errors),
                log_shipping=_safe_bool(alerting.get("log_shipping", False), errors,
                                        "features.alerting.log_shipping"),
            ),
            bi_insight=_safe_bool(features.get("bi_insight", False), errors,
                                  "features.bi_insight"),
            pricing_lambda=_safe_bool(features.get("pricing_lambda", False), errors,
                                      "features.pricing_lambda"),
            web_search=_safe_bool(features.get("web_search", False), errors,
                                  "features.web_search"),
            # None/미선언 구분이 managed overlay 소유 경계라 삼치 유지
            body_logging=(_safe_bool(features["body_logging"], errors,
                                     "features.body_logging")
                          if "body_logging" in features else None),
            observability=_safe_bool(features.get("observability", False), errors,
                                     "features.observability"),
            dev_login=_safe_bool(features.get("dev_login", False), errors,
                                 "features.dev_login"),
            service_keys=_safe_bool(features.get("service_keys", False), errors,
                                    "features.service_keys"),
            business_alerts=BusinessAlertsConfig(
                enabled=_safe_bool(bizal.get("enabled", False), errors,
                                   "features.business_alerts.enabled"),
                reconciliation_mismatch=_safe_bool(
                    bizal.get("reconciliation_mismatch", False), errors,
                    "features.business_alerts.reconciliation_mismatch"),
                dead_letter_growth=_safe_bool(
                    bizal.get("dead_letter_growth", False), errors,
                    "features.business_alerts.dead_letter_growth"),
                pricing_missing=_safe_bool(bizal.get("pricing_missing", False), errors,
                                           "features.business_alerts.pricing_missing"),
                slack=_safe_bool(bizal.get("slack", False), errors,
                                 "features.business_alerts.slack"),
            ),
        ),
        images=ImagesConfig(
            registry=_opt_str(images.get("registry"), ""),
            tag=_opt_str(images.get("tag"), ""),
            tags=_image_tags(images.get("tags"), errors),
        ),
        clients=ClientsConfig(
            models_profile=_opt_str(clients.get("models_profile"), "global"),
        ),
        # 앞뒤 공백만 정리한다 — 길이·문자 검증은 validate() 에서 오류로 알린다.
        branding=BrandingConfig(
            name=(_brand_raw.strip() if isinstance(_brand_raw, str) else ""),
            managed_login=(_safe_bool(branding["managed_login"], errors,
                                      "branding.managed_login")
                           if "managed_login" in branding else False),
        ),
        observability=ObservabilityConfig(alerting=ObservabilityAlertingConfig(
            enabled=_safe_bool(_obs_alert.get("enabled", False), errors,
                               "observability.alerting.enabled"),
            slack_secret_name=(_slack_secret or "").strip(),
        )),
        infra=InfraConfig(
            azs=_str_items(infra.get("azs"), "infra.azs", errors),
            vpc_cidr=_opt_str(infra.get("vpc_cidr"), ""),
            cognito_domain_suffix=_opt_str(infra.get("cognito_domain_suffix"), ""),
            cognito_groups=_str_items(infra.get("cognito_groups"),
                                      "infra.cognito_groups", errors),
            bedrock_allowed_model_arns=_str_items(
                infra.get("bedrock_allowed_model_arns"),
                "infra.bedrock_allowed_model_arns", errors,
                item_re=_BEDROCK_ARN_RE, item_desc="bedrock ARN"),
            mantle_regions=_str_items(
                infra.get("mantle_regions"), "infra.mantle_regions", errors,
                item_re=_REGION_RE, item_desc="리전 ID"),
            # _opt_str 코어션 금지 — float 1.30 → "1.3" 왜곡을 validate 의
            # isinstance 검사가 잡으려면 raw 값이 필요하다.
            eks_version=infra.get("eks_version") or "",
            db=InfraDbConfig(
                mode=_opt_str(_infra_db.get("mode"), ""),
                min_acu=_safe_float_or_none(_infra_db.get("min_acu"), errors,
                                            "infra.db.min_acu"),
                max_acu=_safe_float_or_none(_infra_db.get("max_acu"), errors,
                                            "infra.db.max_acu"),
                instance_class=_opt_str(_infra_db.get("instance_class"), ""),
                engine_version=_opt_str(_infra_db.get("engine_version"), ""),
            ),
            cache=InfraCacheConfig(
                mode=_opt_str(_infra_cache.get("mode"), ""),
                node_type=_opt_str(_infra_cache.get("node_type"), ""),
                num_clusters=_safe_int_or_none(_infra_cache.get("num_clusters"),
                                               errors, "infra.cache.num_clusters"),
                num_node_groups=_safe_int_or_none(_infra_cache.get("num_node_groups"),
                                                  errors, "infra.cache.num_node_groups"),
                replicas_per_node_group=_safe_int_or_none(
                    _infra_cache.get("replicas_per_node_group"),
                    errors, "infra.cache.replicas_per_node_group"),
            ),
        ),
        oidc=OidcConfig(
            issuer_url=_opt_str(oidc.get("issuer_url"), ""),
            audience=_opt_str(oidc.get("audience"), ""),
            client_id=_opt_str(oidc.get("client_id"), ""),
            authorize_url=_opt_str(oidc.get("authorize_url"), ""),
            token_url=_opt_str(oidc.get("token_url"), ""),
            client_secret=_opt_str(oidc.get("client_secret"), ""),
            provider_name=_opt_str(oidc.get("provider_name"), "oidc:cognito"),
            required_group=_opt_str(oidc.get("required_group"), ""),
            groups_claim=_opt_str(oidc.get("groups_claim"), ""),
            group_prefix=_opt_str(oidc.get("group_prefix"), ""),
            reject_unmatched_groups=(
                None if oidc.get("reject_unmatched_groups") is None
                else _safe_bool(oidc.get("reject_unmatched_groups"), errors,
                                "oidc.reject_unmatched_groups")),
            admin_groups=_str_items(oidc.get("admin_groups"),
                                    "oidc.admin_groups", errors),
            admin_emails=_str_items(oidc.get("admin_emails"),
                                    "oidc.admin_emails", errors),
            vk_ttl_hours=_safe_int(oidc.get("vk_ttl_hours", 0), errors,
                                   "oidc.vk_ttl_hours"),
        ),
    )
    errors.extend(cfg.validate())
    if errors:
        raise SchemaError("\n".join(f"  - {m}" for m in errors))
    return cfg


def subnet_cidrs(vpc_cidr: str, n: int) -> dict[str, list[str]]:
    """infra.azs 개수에 맞춘 서브넷 CIDR 4종 — eks env(variables.tf)와
    gateway-ecs 의 공통 규약: private {a}.{b}.{i}/24, public {100+i},
    database {200+i}, elasticache {210+i}.

    파생 /24 가 vpc_cidr 네트워크 밖이면(마스크 </16 등) apply 중간에
    InvalidSubnet.Range 로 죽는다 — 산출 단계에서 검증한다."""
    import ipaddress
    octets = vpc_cidr.split(".")
    base = ".".join(octets[:2]) if len(octets) >= 2 else "10.30"
    out = {
        "private_subnet_cidrs": [f"{base}.{i + 1}.0/24" for i in range(n)],
        "public_subnet_cidrs": [f"{base}.{101 + i}.0/24" for i in range(n)],
        "database_subnet_cidrs": [f"{base}.{201 + i}.0/24" for i in range(n)],
        "elasticache_subnet_cidrs": [f"{base}.{211 + i}.0/24" for i in range(n)],
    }
    try:
        gen = [ipaddress.ip_network(c) for lst in out.values() for c in lst]
    except ValueError as exc:
        # 파생 규약이 무효 CIDR 자체를 만들면(azs 과다 등) 조용히 방출하면
        # 안 된다 — terraform 이 plan 에서 죽기 전에 스키마가 잡는다.
        raise SchemaError(
            f"파생 서브넷 CIDR 이 무효합니다(azs={n}개): {exc}") from exc
    try:
        vpc_net = ipaddress.ip_network(vpc_cidr, strict=False)
        outside = [str(c) for c in gen if not c.subnet_of(vpc_net)]
    except (ValueError, TypeError):
        # 형식 오류는 infra.vpc_cidr 스키마 검증이 잡는다 — subnet_of 의
        # IPv4↔IPv6 비교 TypeError 도 여기서 삼킨다.
        outside = []
    if outside:
        raise SchemaError(
            f"파생 서브넷이 vpc_cidr({vpc_cidr}) 범위 밖입니다: {outside} — "
            "infra.vpc_cidr 을 /16 이상으로 넓히거나 env terraform.tfvars 에 "
            "subnet CIDR 을 직접 선언하세요")
    return out


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
