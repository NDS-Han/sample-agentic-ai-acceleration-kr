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
import os
import re
import sys

import yaml
from pathlib import Path

from .. import schema
from ..schema import GatewayConfig


# PyYAML 은 ``00991142`` 같은 선행 0 숫자열을 str 로 보고 무인용 덤프한다 —
# 같은 파일을 go-yaml(helm) 이 읽으면 숫자로 해석돼 image tag 가
# ``%!s(float64=991142)`` 처럼 깨진다. helm 이 읽는 values.yaml 에 한해
# 숫자/불리언으로 해석될 수 있는 str 은 항상 인용한다(git sha 태그가 우연히
# 전부 숫자일 때 재발한다).
class _HelmSafeDumper(yaml.SafeDumper):
    pass


_AMBIGUOUS_SCALAR = re.compile(
    r"^(?:[-+]?(?:\.inf|\.nan)|[-+]?[0-9][0-9_.,eE+-]*|0[xX][0-9a-fA-F]+|"
    r"true|false|yes|no|y|n|on|off|null|~)$",
    re.IGNORECASE,
)


def _repr_ambiguous_str(dumper: yaml.Dumper, data: str):
    style = "'" if _AMBIGUOUS_SCALAR.match(data) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_HelmSafeDumper.add_representer(str, _repr_ambiguous_str)

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
        # migration 도 같은 트리에서 빌드/푸시된다 — tag 를 따르지 않으면
        # env overlay 의 옛 핀을 당겨 ImagePullBackOff (bootstrap push 와
        # 계약을 맞춘다). images.tags.migration 이 개별 핀으로 덮는다.
        v["migration"]["image"] = {"tag": cfg.images.tag}
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
        # 비워두면 빈 문자열 블록이 생기므로 여기선 host 만 관리한다.
        # domain.hosts 는 파생 네이밍 대신 실제 호스트를 소유한다 — 기존 배포
        # 채택(gateway-dev.* 등 다른 규칙) 시 URL 을 바꾸지 않기 위한 키다.
        derived = {"gateway": f"gateway.{cfg.domain.name}",
                   "adminUi": f"admin.{cfg.domain.name}",
                   "adminApi": f"admin-api.{cfg.domain.name}"}
        yaml_key = {"gateway": "gateway", "adminUi": "admin_ui",
                    "adminApi": "admin_api"}
        for key, default in derived.items():
            # 통째 교체가 아니라 병합 — admin_internal 이 위에서 넣은
            # scheme=internal 어노테이션이 여기서 날아가면 도메인 있는 배포도
            # admin ALB 가 internet-facing 으로 되돌아간다.
            v["ingress"][key] = {
                **v["ingress"].get(key, {}),
                "host": cfg.domain.hosts.get(yaml_key[key]) or default,
                "path": "/", "pathType": "Prefix"}
        # domain 도입 시 NEXTAUTH_URL 도 새 주소로 — 비워두면 이전 값(ALB DNS
        # 등)이 남아 Cognito callback allowlist 와 어긋나 SSO 가 깨진다
        v.setdefault("adminUi", {})["nextauthUrl"] = (
            f"https://{v['ingress']['adminUi']['host']}")

    # ── route53-acm — 정식 도메인: 443 리스너 + redirect 는 render 가 선언하고,
    #    certificate-arn 은 apply 가 ACM 인증서를 보장한 뒤 --set 으로 주입한다.
    if cfg.domain.mode == "route53-acm" and cfg.domain.name:
        for key in ("gateway", "adminUi", "adminApi"):
            v.setdefault("ingress", {}).setdefault(key, {}).setdefault(
                "annotations", {}).update({
                    "alb.ingress.kubernetes.io/listen-ports":
                        '[{"HTTP":80},{"HTTPS":443}]',
                    "alb.ingress.kubernetes.io/ssl-redirect": "443",
                })

    # ── selfsigned-temp — 도메인 없이 admin-ui 만 임시 HTTPS (Cognito SSO 콜백이
    #    https 를 요구하기 때문). 인증서는 apply 가 self-signed 로 만들어 ACM 에
    #    올리고 certificate-arn 을 --set 으로 주입한다 (apply 시점에만 ALB 주소를
    #    알 수 있어 render 에선 리스너·redirect 만 선언).
    if cfg.domain.mode == "selfsigned-temp":
        v.setdefault("ingress", {}).setdefault("adminUi", {}).setdefault(
            "annotations", {}).update({
                "alb.ingress.kubernetes.io/listen-ports": '[{"HTTP":80},{"HTTPS":443}]',
                "alb.ingress.kubernetes.io/ssl-redirect": "443",
            })

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
        if cfg.oidc.groups_claim:
            o["groupsClaim"] = cfg.oidc.groups_claim
        if cfg.oidc.group_prefix:
            o["groupPrefix"] = cfg.oidc.group_prefix
        if cfg.oidc.reject_unmatched_groups is not None:
            o["rejectUnmatchedGroups"] = cfg.oidc.reject_unmatched_groups
        # adminBootstrap — 선언된 키만 덮어쓴다. 둘 다 세팅하면
        # admin_emails 만 선언해도 groups 가 [] 로 env 의 ClaudeAdmin
        # 부트스트랩을 지워 관리자 잠김이 난다 (values merge 는 리스트 통째 교체).
        ab = v.setdefault("adminApi", {}).setdefault("adminBootstrap", {})
        if cfg.oidc.admin_groups:
            ab["groups"] = cfg.oidc.admin_groups
        if cfg.oidc.admin_emails:
            ab["emails"] = cfg.oidc.admin_emails
        v.setdefault("adminApi", {})["oidc"] = dict(o)
        v.setdefault("gatewayProxy", {})["oidc"] = dict(o)
        ui_env = {"OIDC_CLIENT_ID": cfg.oidc.client_id}
        if cfg.oidc.authorize_url:
            ui_env["OIDC_AUTHORIZE_URL"] = cfg.oidc.authorize_url
        if cfg.oidc.token_url:
            ui_env["OIDC_TOKEN_URL"] = cfg.oidc.token_url
        v.setdefault("adminUi", {}).setdefault("env", {}).update(ui_env)
    # dev login 은 admin-api 가 dev. 토큰을 받아야 완결된다 — 어느 한쪽만
    # 켜지면 화면은 있는데 401 이거나 API 는 열렸는데 UI 가 막힌 반쪽 상태.
    # gen 레이어가 양쪽을 features.dev_login 하나로 정렬해 env overlay 의
    # 흩어진 값까지 수렴시킨다. 단 OIDC 배선 시 admin-api 는 dev 토큰을
    # 무조건 거부(auth 삼중가드)하므로 UI 도 끈다 — 켜두면 영영 401 인
    # 로그인 버튼만 남는다.
    _dl = "true" if (cfg.features.dev_login and not cfg.oidc.enabled) \
        else "false"
    v.setdefault("adminUi", {}).setdefault("env", {})["DEV_LOGIN_ENABLED"] = _dl
    v.setdefault("adminApi", {}).setdefault("env", {})["DEV_LOGIN_ENABLED"] = _dl

    # features.service_keys — 기본 꺼짐이라 항상 명시값을 내린다(미방출이면
    # admin-api 앱 기본 true 가 이기고, env overlay 의 "false" 고정도
    # gateway.yaml 의 true 선언이 이겨야 한다). UI 버튼은 GET
    # /admin/service-accounts 응답 필드로 숨긴다.
    v.setdefault("adminApi", {}).setdefault("env", {})[
        "SERVICE_KEYS_ENABLED"] = "true" if cfg.features.service_keys else "false"

    # VK TTL — adminApi.env:OIDC_VK_TTL_HOURS 로 넣으면 oidcEnv 뒤에 같은 이름의
    # env 가 2개 생기는 함정이 있다(ops 8-Z) — 정식 키 adminApi.oidc.vkTtlHours.
    if cfg.oidc.vk_ttl_hours:
        v.setdefault("adminApi", {}).setdefault("oidc", {})[
            "vkTtlHours"] = cfg.oidc.vk_ttl_hours

    # ── deploy.capacity_tier + deploy.sizing (eks) — 용량 노브를 gateway.yaml
    #    이 소유하게 한다. capacity_tier(사용자 수 밴드)가 프리셋 바닥을 깔고
    #    sizing 의 명시 키가 개별 오버라이드한다. size_tier 는 eks 에서 문서용
    #    이라 여기서 실제 크기를 정한다:
    #      .gateway-proxy.min_replicas/max_replicas → autoscaling
    #      .gateway-proxy.bedrock_thread_pool_size  → env (동시 스트림
    #        슬롯/워커 — 기본 128, 스트림 hold 는 스레드 블록이라 CPU 가 안 잡힘)
    #      .redis.pool_size → redis.poolSize (**워커 프로세스당** —
    #        노드측 최대 = poolSize × WORKERS × maxReplica ≤ maxclients.
    #        40 은 finalize 폭풍에 MaxConnectionsError/예산 유실을 냈다 — 실측)
    from ..tiers import eks_capacity_sizing

    sz = eks_capacity_sizing(cfg)
    gp = sz.get("gateway-proxy") or {}
    if gp.get("min_replicas") or gp.get("max_replicas"):
        asg = v.setdefault("gatewayProxy", {}).setdefault("autoscaling", {})
        asg["enabled"] = True
        if gp.get("min_replicas"):
            asg["minReplicas"] = int(gp["min_replicas"])
        if gp.get("max_replicas"):
            asg["maxReplicas"] = int(gp["max_replicas"])
    if gp.get("bedrock_thread_pool_size"):
        v.setdefault("gatewayProxy", {}).setdefault("env", {})[
            "BEDROCK_THREAD_POOL_SIZE"] = str(gp["bedrock_thread_pool_size"])
    rd = sz.get("redis") or {}
    if rd.get("pool_size"):
        v.setdefault("redis", {})["poolSize"] = int(rd["pool_size"])

    if cfg.features.alerting.log_shipping:
        # Fargate 로그 라우터 — 클러스터명 추론은 env 디렉토리명
        # (module eks-fargate 가 <project>-<env> 로 짓고 env 디렉토리도 같은
        # 규칙). 커스텀 tf_env_dir 이면 차트 values 로 logGroupName 을 직접 준다.
        env_dir = cfg.deploy.tf_env_dir or \
            f"deployment/terraform/environments/llm-gateway-{cfg.env}"
        fl = v.setdefault("observability", {}).setdefault("fargateLogging", {})
        fl["enabled"] = True
        fl["region"] = cfg.aws.region
        fl["logGroupName"] = f"/aws/eks/{Path(env_dir).name}/fargate-logs"

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
        if cfg.domain.hosts:
            # 선언 누락 키는 파생 호스트로 채워진다 — 어느 키가 파생값인지 명시해
            # "모두 오버라이드했다"는 착각으로 URL 이 바뀌는 사고를 막는다.
            fallbacks = [f"{k}={p}.{cfg.domain.name}" for k, p in
                         (("gateway", "gateway"), ("admin_ui", "admin"),
                          ("admin_api", "admin-api"))
                         if k not in cfg.domain.hosts]
            note = ("ingress host 를 domain.hosts 선언값으로 씁니다 — live 와 "
                    "다르면 URL 이 바뀌므로 클라이언트 재안내가 필요합니다.")
            if fallbacks:
                note += f" 미선언 키는 파생 호스트로 채워집니다: {', '.join(fallbacks)}"
            notes.append(note)
        else:
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
    if cfg.oidc.enabled and cfg.features.dev_login:
        notes.append("dev_login 은 OIDC 배선 중엔 무시됩니다 — admin-api 가 "
                     "issuer 설정 시 dev. 토큰을 거부합니다(DEV_LOGIN_ENABLED=false "
                     "로 수렴).")
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
    """env 의 terraform.tfvars 에 선언된 vpc_cidr — 없으면 env 기본값.
    주석 처리된 값(`# vpc_cidr = "..."`)이 서브넷 산출 기준을 오염시키지
    않도록 스캐너로 주석을 제거한다."""
    from .common import tfvars_clean
    found = ""
    for text in _env_tfvars_texts(env_dir):
        m = re.search(r'(?m)^\s*vpc_cidr\s*=\s*"([^"]+)"',
                      tfvars_clean(text))
        if m:
            found = m.group(1)
    return found or "10.30.0.0/16"  # variables.tf 기본값


def render_tfvars(cfg: GatewayConfig, env_dir: Path | None = None) -> str:
    """eks env(terraform) 의 managed 오버레이 — gateway.yaml 이 소유하는 인프라
    키만. eks env tfvars 는 사람이 소유하므로 이 파일을 -var-file 로 뒤에
    붙인다 (나중 -var-file 가 이긴다)."""
    lines = [
        "# generated by deploy — gateway.yaml 이 소유하는 terraform 키만.",
        "# 적용: terraform -var-file=<이 파일> apply (env 디렉토리에서)",
    ]
    # 기능 토글은 "켜기"만 발산한다 — yaml 미선언/비활성이면 키 자체를 쓰지
    # 않아 auto-load 되는 env tfvars(사람 소유) 값이 그대로 유지된다. false 를
    # 쓰면 기존 Firehose/S3·SES IRSA 가 destroy 계획에 들어가므로 비활성화는
    # env terraform 수동 절차다 (schema warnings 도 같은 내용을 안내한다).
    if cfg.features.body_logging:
        lines.append("enable_body_logging = true")
    if cfg.features.notifications.provider == "ses":
        lines.append("enable_notification_ses = true")
    if cfg.features.alerting.enabled:
        lines.append("enable_alerting = true")
        # emails 는 enabled 게이트 안에서만 — schema 가 미리 잡지만 이중 방어.
        if cfg.features.alerting.emails:
            lines.append("alert_emails = "
                         + json.dumps(cfg.features.alerting.emails))
    if cfg.features.bi_insight:
        # admin-chat-agent 인프라(ECR·role·staging·tool Lambdas) — managed
        # overlay 가 module.agentcore_runtime + module.irsa 를 -target 적용한다.
        # db_tools 없이는 agent 가 쓸 수 없으므로 함께 켠다 (끄기는 env tfvars).
        lines.append("enable_chat_agent = true")
        lines.append("enable_chat_db_tools = true")
        # AgentCore Runtime 은 ECR 이미지와 같은 리전에 있어야 한다 —
        # provisioner(REGION)와 IRSA statement 스코프를 하나의 env로 정렬.
        # fallback 은 ap-northeast-2 가 아니라 배포 리전: export 를 잊은 셸이
        # NE2 로 렌더해 IRSA statement 가 엉뚱한 리전을 가리키는 사고가 났었다.
        lines.append('chat_agent_region = "{}"'.format(
            os.environ.get("CHAT_AGENT_REGION") or cfg.aws.region))
    inf = cfg.infra
    # Fargate profile·IRSA serviceaccount 스코프는 application_namespace 를
    # 따른다 — deploy.namespace 를 바꾸면 여기가 어긋나 파드가 스케줄되지
    # 못한다. helm 이 가는 네임스페이스로 항상 정렬한다.
    lines.append(f'application_namespace = "{cfg.deploy.namespace}"')
    if env_dir:
        try:
            from .common import tfvars_clean
            _et = "\n".join(_env_tfvars_texts(env_dir))
            _et = tfvars_clean(_et)
            # terraform 은 last-file-wins — 여러 파일 선언 시 마지막이 실효값
            _all = re.findall(r'(?m)^\s*application_namespace\s*=\s*"([^"]+)"',
                              _et)
            _m_val = _all[-1] if _all else None
            if _m_val and _m_val != cfg.deploy.namespace:
                print(f"⚠ env tfvars 의 application_namespace="
                      f"{_m_val} — managed overlay 가 deploy.namespace="
                      f"{cfg.deploy.namespace} 로 덮어씁니다. 단 module.eks 는 "
                      f"managed target 이 아니라 update 경로(deploy apply)는 "
                      f"Fargate profile/IRSA 를 수렴하지 않습니다 — 신 ns 의 "
                      f"파드가 스케줄되려면 env terraform apply 가 필요합니다",
                      file=sys.stderr)
            elif not _m_val and cfg.deploy.namespace != "llm-gateway":
                print(f"ⓘ deploy.namespace={cfg.deploy.namespace} — module.eks 는 "
                      "managed target 이 아니므로, 기존 env 에서는 env terraform "
                      "apply 가 Fargate profile/IRSA 에 반영해야 합니다",
                      file=sys.stderr)
        except OSError:
            pass
    if inf.vpc_cidr:
        lines.append(f'vpc_cidr = "{inf.vpc_cidr}"')
    if inf.azs:
        lines.append(f"azs = {json.dumps(inf.azs)}")
    if inf.vpc_cidr or inf.azs:
        # azs 개수와 서브넷 cidr 리스트 길이는 같아야 한다 — 선언되면 둘 다 생성.
        # 실효 azs 수는 terraform last-wins 기준: infra.azs 를 방출하면 env 선언을
        # 덮어쓰지만, 미방출이면 env tfvars 의 azs 가 그대로 산다 — 그 개수에
        # 맞춰야 리스트 길이가 어긋나지 않는다.
        n_azs = (len(inf.azs) or len(_tfvars_list(env_dir, "azs")) or 2)
        for k, v in schema.subnet_cidrs(
                inf.vpc_cidr or _env_vpc_cidr(env_dir), n_azs).items():
            lines.append(f"{k} = {json.dumps(v)}")
        # module.vpc/module.aurora 는 managed target 이 아니다 — update 경로
        # (deploy apply)의 targeted plan 은 이 키들을 평가하지 않으므로 선언이
        # 조용히 무시된다. 수렴은 bootstrap/env terraform 의 전체 apply 뿐.
        print("ⓘ infra.vpc_cidr/azs 선언 — managed overlay 에 싣지만 "
              "module.vpc/aurora 는 managed target 이 아니라 deploy apply 는 "
              "수렴하지 않습니다(기존 인프라 파괴 방지). 반영은 env terraform "
              "apply 또는 재-bootstrap 이 필요합니다", file=sys.stderr)
    if inf.cognito_domain_suffix:
        lines.append(f'cognito_domain_suffix = "{inf.cognito_domain_suffix}"')
    if inf.cognito_groups:
        # env tfvars 의 기존 그룹과 합집합 — 콜백/로그아웃 URL 과 같은 계약.
        # 미병합이면 env 선언 그룹이 managed plan 의 delete 로 잡혀 범위 가드가
        # 매 apply 를 중단한다 (삭제는 env terraform 수동 절차).
        groups = list(dict.fromkeys(
            [*_tfvars_list(env_dir, "cognito_groups"),
             *inf.cognito_groups, "ClaudeAdmin"]))
        lines.append(f"cognito_groups = {json.dumps(groups)}")
    if inf.bedrock_allowed_model_arns:
        lines.append(
            f"bedrock_allowed_model_arns = "
            f"{json.dumps(inf.bedrock_allowed_model_arns, indent=2)}")
    if inf.mantle_regions:
        lines.append(f"mantle_regions = {json.dumps(inf.mantle_regions)}")
    if cfg.domain.name:
        # domain 도입 시 Cognito 콜백·로그아웃을 도메인으로 — env tfvars 의 기존
        # 수동 목록과 합집합(-var-file 은 리스트를 통째로 교체하므로 병합 필요)
        # admin UI 호스트는 ingress 와 같은 domain.hosts 계약을 따라야 한다 —
        # 파생 admin.<domain> 만 넣으면 선언 호스트의 로그인이 redirect_mismatch.
        admin_host = (cfg.domain.hosts.get("admin_ui")
                      or f"admin.{cfg.domain.name}")
        for var, derived in (
            ("cognito_callback_urls",
             [f"https://{admin_host}/api/auth/callback"]),
            ("cognito_logout_urls",
             [f"https://{admin_host}/"]),
        ):
            merged = list(dict.fromkeys(
                [*_tfvars_list(env_dir, var), *derived]))
            lines.append(f"{var} = {json.dumps(merged, indent=2)}")
    return "\n".join(lines) + "\n"


def _env_tfvars_texts(env_dir: Path | None) -> list[str]:
    """env 의 HCL tfvars 소스들 — terraform.tfvars + *.auto.tfvars(abc순,
    terraform 자동 로드 계약). *.tfvars.json/-var-file 은 형식·호출자 맥락이
    달라 여기선 보지 않는다."""
    if not env_dir:
        return []
    files = [env_dir / "terraform.tfvars",
             *sorted(env_dir.glob("*.auto.tfvars"))]
    out: list[str] = []
    for f in files:
        try:
            if f.exists():
                out.append(f.read_text())
        except OSError:
            continue  # 읽기 실패·TOCTOU 삭제는 스캔 대상에서만 빠진다
    return out


def _tfvars_list(env_dir: Path | None, name: str) -> list[str]:
    """env tfvars(모든 자동 로드 소스)에 선언된 리스트 변수의 문자열 원소 —
    gen 오버레이가 리스트를 통째로 교체하므로 병합할 기존값을 읽는 용도.

    주석은 라인 필터가 아니라 tfvars_clean 스캐너로 제거 —
    `#`/`//` 라인 주석, `/* */` 블록 주석, heredoc·map 본문 안의 죽은 선언이
    살아나 OAuth 리다이렉트 허용 목록을 오염시키는 것을 막는다.
    접두 변수(legacy_cognito_callback_urls)도 ^ 앵커로 걸러낸다."""
    import re
    from .common import tfvars_clean
    out: list[str] = []
    for text in _env_tfvars_texts(env_dir):
        m = re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*\[(.*?)\]",
                      tfvars_clean(text), re.S)
        if m:
            out = re.findall(r'"([^"]+)"', m.group(1))  # 뒤 파일이 이긴다
    return out


def render(cfg: GatewayConfig, out_dir: Path, repo_root: Path = REPO_ROOT) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    env_vals = env_values_file(cfg, repo_root)
    extra_notes: list[str] = []

    # env overlay 기본값까지 반영된 최종 min/max — min 만 sizing 에 있고 max 는
    # overlay 값을 물려받는 조합(min4 + overlay max3 등)은 API 서버가 HPA 를
    # 거부한다. helm 레이어링(chart → env overlay → generated, 마지막이 이김)과
    # 같은 순서로 합성해 render 시점에 잡는다.
    gen = desired_values(cfg)
    env_asg: dict = {}
    if env_vals:
        env_asg = ((yaml.safe_load(env_vals.read_text()) or {})
                   .get("gatewayProxy", {}).get("autoscaling") or {})
        # env values 가 logGroupName 을 직접 선언했으면 gen 추론값을 내리지
        # 않는다 — gen 레이어가 마지막으로 적용되므로, 그대로 두면 env 의
        # 커스텀 클러스터명 override 가 조용히 덮어쓰기 된다.
        env_fl = (((yaml.safe_load(env_vals.read_text()) or {})
                   .get("observability") or {}).get("fargateLogging") or {})
        if env_fl.get("logGroupName"):
            (gen.get("observability") or {}).get("fargateLogging", {}) \
                .pop("logGroupName", None)
    gen_asg = ((gen.get("gatewayProxy") or {}).get("autoscaling") or {})
    # helm 레이어링과 같은 순서: chart 기본 → env overlay → gen(최종). 차트
    # 기본을 깔지 않으면 env 가 min 만 선언한 경우(min:25 > 차트 기본 max:20)
    # 같은 한쪽 선언이 검증을 빠져나간다(7차 리뷰 #1).
    # 차트 기본값 레이어 — 파일이 없으면(테스트의 스켈레톤 repo_root 등)
    # 기본 없이 env+gen 만으로 합성한다. 실제 리포지터리에서는 항상 존재하며,
    # 주입된 repo_root 를 따라야 다른 체크아웃 대상 렌더에서도 올바른 기본을
    # 읽는다(8차 리뷰 — 전역 REPO_ROOT 혼용 결함).
    _chart_path = repo_root / "deployment" / "charts" / "llm-gateway" / "values.yaml"
    if _chart_path.exists():
        chart_vals = yaml.safe_load(_chart_path.read_text()) or {}
    else:
        # 잘못 주입된 repo_root 는 차트 기본 레이어 누락을 조용히 숨긴다 —
        # 실 리포지터리엔 항상 있어야 하므로 노트로 표면화한다(9차 리뷰).
        chart_vals = {}
        extra_notes.append(
            f"chart values({_chart_path})를 찾지 못했습니다 — autoscaling 기본값 "
            "레이어 없이 env+gen 합성만 검증했습니다. repo_root 주입을 확인하세요.")

    chart_asg = ((chart_vals.get("gatewayProxy") or {}).get("autoscaling") or {})
    merged = {**chart_asg, **env_asg, **gen_asg}
    mn, mx = merged.get("minReplicas"), merged.get("maxReplicas")
    if merged.get("enabled") and (mn is not None or mx is not None):
        def _is_int(v) -> bool:
            # int() 절삭을 걸러야 한다 — minReplicas: 2.9 는 int(2.9)=2 로
            # 검증을 통과하지만 k8s API(HPA integer)에서 거절된다(8차 리뷰).
            # bool(True→1)·정수 float(3.0 → '3.0' 렌더 → k8s 거절)·inf/nan
            # (int() 가 OverflowError 로 캐시 범위를 빠져나감)도 같은 구멍이다.
            if isinstance(v, bool) or not isinstance(v, int):
                return False
            return True
        # 각 필드는 단독 선언돼도 검증한다 — 어느 쪽이든 비수치/범위 밖이면
        # 렌더는 통과하고 kubectl apply 에서야 터진다(11차 리뷰).
        for name, v in (("minReplicas", mn), ("maxReplicas", mx)):
            if v is None:
                continue
            if not _is_int(v):
                raise ValueError(
                    f"gatewayProxy.autoscaling: {name} 는 "
                    f"정수여야 합니다 ({name}={v!r})")
        if mn is not None and int(mn) < 1:
            # CPU 기반 HPA 의 최솟값 — scale-to-zero 는 이 HPA 가 아니므로
            # 0/음수는 apply 단계 거절이다(10차 리뷰).
            raise ValueError(
                f"gatewayProxy.autoscaling: minReplicas 는 1 이상이어야 합니다 "
                f"(min={mn})")
        if mx is not None and int(mx) < 1:
            # max 단독 선언(minReplicas: null 오버레이로 chart 기본 min 삭제)도
            # 렌더에서 잡는다 — apply 단계의 HPA 거절과 같은 구멍(12차 리뷰).
            raise ValueError(
                f"gatewayProxy.autoscaling: maxReplicas 는 1 이상이어야 합니다 "
                f"(max={mx})")
        if mn is not None and mx is not None and int(mn) > int(mx):
            raise ValueError(
                f"gatewayProxy.autoscaling: minReplicas({mn}) > "
                f"maxReplicas({mx}) — deploy.sizing/capacity_tier 와 "
                f"{env_vals.name if env_vals else 'chart 기본값'} 기본값의 "
                "합성 결과입니다. min 과 max 를 함께 선언하세요")

    # executorInflightTarget 은 워커당 풀(BEDROCK_THREAD_POOL_SIZE)의
    # ~70% 가정값 — sizing 으로 풀을 바꾸는데 env values 의 타깃이 낡은
    # 채(예: pool 256 인데 128 용 90)면 스케일 기준점이 어긋난다.
    # sizing 은 gateway.yaml 의 명시 의도라, 풀을 바꿨으면 파생 타깃을
    # gen 에 실어 보낸다(gen 은 마지막 레이어라 env 를 이긴다). env 에
    # 다른 명시값이 있으면 덮어쓰기 전에 경고한다 — 의도된 수동 타깃이면
    # env 를 고치거나 sizing 선언을 지워 합치시킨다.
    # ⚠️ 파생은 "선언된 타깃을 고치는" 역할에 한정한다 — 차트 기본은 이 키가
    #    주석 처리된 옵트인 기능이라, 아무 데도 선언이 없는데 방출하면
    #    리소스 HPA 만 쓰던 env 가 Pods-메트릭 HPA(전제: in-cluster otel +
    #    prometheus-adapter)로 바뀐다(5차 리뷰 m3).
    gen_pool = ((gen.get("gatewayProxy") or {}).get("env") or {}).get(
        "BEDROCK_THREAD_POOL_SIZE")
    target_declared = (
        env_asg.get("executorInflightTarget") is not None
        or ((gen.get("gatewayProxy") or {}).get("autoscaling") or {})
        .get("executorInflightTarget") is not None
    )
    if gen_pool and not target_declared:
        print(
            f"ⓘ sizing.bedrock_thread_pool_size={gen_pool} 인데 "
            "executorInflightTarget 선언이 없습니다 — executor-inflight "
            "HPA 는 옵트인이라 파생하지 않습니다. 켜려면 env overlay 에 "
            "executorInflightTarget 을 선언하세요(파생값이 정정합니다)",
            file=sys.stderr)
    if gen_pool and target_declared:
        try:
            derived = max(1, round(int(gen_pool) * 0.7))
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"sizing.gateway-proxy.bedrock_thread_pool_size="
                f"{gen_pool!r} — 정수여야 executorInflightTarget 을 "
                "파생할 수 있습니다") from e
        if env_vals:
            env_target = env_asg.get("executorInflightTarget")
            try:
                env_target_num = (None if env_target is None
                                  else float(env_target))
            except (TypeError, ValueError):
                env_target_num = None
                print(
                    f"⚠ {env_vals.name} 의 executorInflightTarget="
                    f"{env_target!r} 는 수치가 아닙니다 — gen 파생값이 "
                    "이깁니다",
                    file=sys.stderr)
            if env_target_num is not None and env_target_num != derived:
                print(
                    f"⚠ sizing.bedrock_thread_pool_size={gen_pool} → "
                    f"파생 executorInflightTarget={derived} 인데 "
                    f"{env_vals.name} 에 {env_target} 선언 — gen 값이 이긴다. "
                    "의도된 수동 타깃이면 env 값을 파생값과 맞추거나 sizing "
                    "선언을 지우세요",
                    file=sys.stderr)
        gen["gatewayProxy"].setdefault("autoscaling", {})[
            "executorInflightTarget"] = derived

    values_path = out_dir / "values.yaml"
    values_path.write_text(
        "# generated by deploy — gateway.yaml 이 소유하는 키만. 직접 수정하지 말 것.\n"
        "# 레이어 순서: chart values.yaml → env overlay → 이 파일(마지막이 이긴다)\n"
        + yaml.dump(
            gen,
            Dumper=_HelmSafeDumper,
            sort_keys=False,
            allow_unicode=True,
        ))

    tfvars_path = out_dir / "terraform.tfvars"
    tfvars_path.write_text(render_tfvars(cfg, env_dir=env_dir_path(cfg, repo_root)))

    meta = deploy_meta(cfg, env_vals, repo_root)
    meta["values_layers"].append(str(values_path))
    deploy_path = out_dir / "deploy.yaml"
    deploy_path.write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True))

    notes = feature_notes(cfg) + extra_notes
    if cfg.features.body_logging:
        notes.append(
            "body_logging: 인프라(Firehose/S3/IRSA)는 terraform 소유입니다 — "
            "`deploy apply` 가 output 누락/선언 drift 를 감지하면 managed "
            "overlay(-var-file)로 좁힌 plan 을 범위 검증해 module.body_logging·"
            "module.irsa 변경만 적용하고 output 을 pod env 로 주입합니다.")
    if cfg.features.bi_insight:
        notes.append(
            "bi_insight: managed overlay 가 module.agentcore_runtime 을 "
            "프로비저닝하고, apply 가 AgentCore Runtime 을 멱등 생성해 ARN 을 "
            "admin-api env 로 주입합니다 (provision_agentcore_chatagent.py — "
            "web_search 와 동일 패턴). chat_reader DB 비밀번호는 in-cluster "
            "pod 가 자동 동기화합니다. Runtime 리전은 ECR 과 같아야 합니다 — "
            "CHAT_AGENT_REGION 을 배포 리전과 정렬하십시오.")
    if not env_vals:
        notes.append(f"env overlay(values-eks-fargate-{cfg.env}.yaml)를 못 찾았습니다 — "
                     "차트 기본값 + 이 오버레이만으로 배포됩니다. 기존 배포를 업데이트하는 "
                     "거라면 env 값이 빠져 의도와 달라질 수 있습니다. 특히 시크릿은 "
                     "이 오버레이가 공급하지 않습니다 — chart 는 externalSecrets.enabled(ESO) "
                     "또는 .Values.secrets 중 하나가 반드시 필요하며, 둘 다 없으면 "
                     "migration Job과 전 pod가 Secret 참조 실패로 뜨지 못합니다.")
    return {"files": [values_path, deploy_path, tfvars_path], "notes": notes,
            "env_dir": meta["env_dir"]}
