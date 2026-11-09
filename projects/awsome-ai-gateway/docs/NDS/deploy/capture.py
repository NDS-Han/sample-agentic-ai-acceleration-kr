# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""capture — 이미 배포된 환경을 읽어 gateway.yaml 후보를 역생성한다.

목적: imperative 스크립트/수작업으로 배포된 환경을 선언적 관리로 온보딩.
캡처가 못 읽는 값은 빈칸 + notes 로 남기고, 기존 gateway.yaml 과 다른 값은
호출자가 apply(yaml 유지)/absorb(캡처 채택)/skip 을 선택하게 한다.

backend 별 정보원:
  compose — gen/<env>/ 의 docker-compose.yml + .env + Caddyfile (정적 산출물)
  ecs     — terraform output + aws ecs describe-task-definition / services
  eks     — helm get values + kubectl 라이브 상태 + terraform output (env_dir)
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from .render.common import parse_env_file
from .render.compose import OBSERVABILITY, WEB_PORTS
from .schema import _HOSTNAME_RE


class CaptureError(Exception):
    pass


def _strip(s: str, prefix: str) -> str:
    return s[len(prefix):] if s.startswith(prefix) else s


def _is_true_env(v) -> bool:
    """env/values 의 명시적 on 판정 — 대소문자·yaml boolean·숫자 모두 흡수."""
    return str(v if v is not None else "").strip().lower() in ("true", "1", "yes", "on")


def _is_false_env(v) -> bool:
    """env/values 의 명시적 off 판정 — yaml `false`(비인용 bool), "0", "no" 포함.

    부재(None)는 false 가 아니다 — 앱 기본값이 진리라 "읽지 못함 = false"로
    오인하면 켜진 배포를 꺼진 것으로 캡처한다(보안 플래그의 fail-open).
    """
    return str(v if v is not None else "").strip().lower() in ("false", "0", "no", "off")


# ==============================================================================
# compose — 정적 산출물 역분석 (라이브 상태와 다를 수 있음 — doctor 가 별도 확인)
# ==============================================================================

def capture_compose(out_dir: Path) -> tuple[dict, list[str]]:
    notes: list[str] = []
    compose_path = out_dir / "docker-compose.yml"
    if not compose_path.exists():
        raise CaptureError(f"{compose_path} 없음 — compose 배포 산출물이 아닙니다")

    try:
        composed = yaml.safe_load(compose_path.read_text()) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise CaptureError(f"{compose_path} 파싱 실패: {exc}")
    if not isinstance(composed, dict):
        raise CaptureError(f"{compose_path} 의 최상위가 mapping 이 아닙니다")
    services = composed.get("services") or {}
    env = parse_env_file(out_dir / ".env")
    caddy_path = out_dir / "Caddyfile"
    caddy = caddy_path.read_text() if caddy_path.exists() else ""
    if not caddy_path.exists():
        notes.append("Caddyfile 없음 — allowed_cidrs/domain 은 추론 불가로 둡니다")

    # env 이름: compose 프로젝트명 "llm-gateway-<env>" → 없으면 디렉토리명
    env_name = _strip(str(composed.get("name", "")), "llm-gateway-") or out_dir.name

    # allowed_cidrs — Caddyfile 의 remote_ip 매처
    m = re.search(r"@blocked not remote_ip ([^\n]+)", caddy)
    cidrs = m.group(1).strip().split() if m else []

    # domain — Caddyfile 이 포트 블록(:8000)이면 none, host 블록이면 이름 추출.
    # compose 경로의 TLS 는 Caddy 자동 발급이라 zone_id 는 알 수 없다.
    host_m = re.search(r"^gateway\.([^\s{]+)\s*\{", caddy, re.M)
    if host_m:
        domain = {"mode": "route53-acm", "name": host_m.group(1).strip()}
        notes.append("domain 은 Caddyfile 에서 추론 — compose 는 자동 TLS 라 "
                     "route53-acm 은 의미상 매핑입니다(zone_id 는 비워둠).")
    elif re.search(r"^:(\d+)\s*\{", caddy, re.M):
        domain = {"mode": "none"}
    else:
        domain = {"mode": "none"}
        notes.append("Caddyfile 에서 라우팅을 못 읽어 domain.mode=none 으로 둡니다")

    # network.mode — compose 는 public/private 구분이 없다 (호스트 방화벽 영역)
    notes.append("network.mode 는 compose 에서 구분이 없어 public 으로 둡니다")

    # images — 앱 서비스의 image: 핀 여부 (build: 면 레지스트리 없음)
    registry, tag = "", ""
    for name in ("gateway-proxy", "admin-api", "admin-ui"):
        img = (services.get(name) or {}).get("image", "")
        if img and "/" in img and ":" in img:
            registry = img.rsplit("/", 1)[0]
            tag = img.rsplit(":", 1)[1]
            break

    # notifications
    provider = env.get("EMAIL_SENDER_TYPE", "mock")
    notif = {"provider": provider if provider in ("mock", "ses", "smtp") else "mock"}
    if provider == "ses":
        notif["ses_from"] = env.get("EMAIL_SENDER_ADDRESS", "")
    elif provider == "smtp":
        notif.update({"smtp_host": env.get("SMTP_HOST", ""),
                      "smtp_port": _safe_int(env.get("SMTP_PORT"), 587),
                      "smtp_from": env.get("EMAIL_SENDER_ADDRESS", "")})
    elif provider not in ("mock", "ses", "smtp"):
        notes.append(f"EMAIL_SENDER_TYPE={provider} 는 스키마에 없는 값 — mock 으로 두고 확인하세요")

    # features — 스키마 키를 전부 기록한다. 라이브 신호를 못 읽은 기능은
    # false 로 명시해야 캡처 결과가 "기능 목록" 역할을 한다(키가 아예
    # 빠지면 사용자는 그 옵션이 존재하는지 알 수 없다).
    api_env = (services.get("admin-api") or {}).get("environment") or {}
    if isinstance(api_env, list):
        api_env = dict(e.split("=", 1) for e in api_env if "=" in e)
    ui_env = (services.get("admin-ui") or {}).get("environment") or {}
    if isinstance(ui_env, list):
        ui_env = dict(e.split("=", 1) for e in ui_env if "=" in e)
    # SERVICE_KEYS_ENABLED — 렌더러는 .env(env_file)에만 쓰고 environment: 블록은
    # override 라 api_env 우선. 둘 다 부재면 앱 기본 true 가 진리다. 옆의
    # dev_login 처럼 두 소스를 봐야 한다 — api_env 만 보면 .env 에 false 로
    # 배포된 것도 true 로 캡처돼 채택 시 발급이 켜진다.
    sk_env = api_env.get("SERVICE_KEYS_ENABLED")
    if sk_env is None:
        sk_env = env.get("SERVICE_KEYS_ENABLED")
    features = {
        "notifications": notif,
        "bi_insight": bool(env.get("AGENTCORE_RUNTIME_ARN")
                           or api_env.get("AGENTCORE_RUNTIME_ARN")),
        "pricing_lambda": bool(env.get("LITELLM_PRICING_LAMBDA")
                               or api_env.get("LITELLM_PRICING_LAMBDA")),
        "web_search": _is_true_env(env.get("WEB_SEARCH_ENABLED")),
        "body_logging": bool(env.get("FIREHOSE_STREAM_NAME")),
        "observability": any(s in services for s in OBSERVABILITY),
        "dev_login": (_is_true_env(env.get("DEV_LOGIN_ENABLED"))
                      or _is_true_env(api_env.get("DEV_LOGIN_ENABLED"))
                      or _is_true_env(ui_env.get("DEV_LOGIN_ENABLED"))),
        "service_keys": not _is_false_env(sk_env),
    }

    # oidc — admin-ui environment(직접 주입된 것) + .env
    issuer = env.get("OIDC_ISSUER_URL") or ui_env.get("OIDC_ISSUER_URL", "")
    oidc: dict = {}
    if issuer:
        oidc = {
            "issuer_url": issuer,
            "client_id": env.get("OIDC_CLIENT_ID") or ui_env.get("OIDC_CLIENT_ID", ""),
            "authorize_url": env.get("OIDC_AUTHORIZE_URL") or ui_env.get("OIDC_AUTHORIZE_URL", ""),
            "token_url": env.get("OIDC_TOKEN_URL") or ui_env.get("OIDC_TOKEN_URL", ""),
            "provider_name": env.get("OIDC_PROVIDER_NAME", "oidc:cognito"),
            "required_group": env.get("OIDC_REQUIRED_GROUP", ""),
        }
        if env.get("OIDC_CLIENT_SECRET"):
            notes.append("OIDC_CLIENT_SECRET 이 .env 에 있습니다 — 캡처 yaml 에는 "
                         "쓰지 않았습니다(시크릿). 필요하면 직접 옮기거나 .env 에 두세요")

    # 캡처 불가 항목 notes
    if not env.get("VIRTUAL_KEY_ENCRYPTION_KEY"):
        notes.append(".env 에 VIRTUAL_KEY_ENCRYPTION_KEY 가 없습니다 — VK DEK 유실 시 발급 키 전부 무효")

    # 이 산출물이 gen/<env> 아래가 아니면(수작업 배포) 채택 시 새 .env 가 생성되고
    # compose 프로젝트명(llm-gateway-<env>)이 달라져 볼륨이 새로 만들어진다 —
    # 기존 DB 데이터와 VK 가 이어지지 않는다. 채택 절차에 반드시 안내해야 한다.
    if out_dir.resolve().name != env_name or "gen" not in out_dir.resolve().parts:
        notes.append(
            f"⚠️ 수작업 배포를 채택하면 새 프로젝트(llm-gateway-{env_name})와 "
            f"새 볼륨으로 뜹니다 — 기존 DB 데이터·시크릿이 이어지지 않습니다. "
            f"채택 전에 ① 기존 .env 를 deployment/gen/{env_name}/.env 로 복사하고 "
            f"② 기존 볼륨(<old-project>_pgdata)을 옮기거나 DB 덤프로 이관하세요 "
            f"(update.md '수작업 compose 온보딩' 절).")

    doc = {
        "version": 1,
        "env": env_name,
        "aws": {"region": env.get("AWS_REGION", "")},
        "deploy": {"target": "compose", "size_tier": "t0"},
        "network": {"mode": "public", "allowed_cidrs": cidrs},
        "domain": domain,
        "features": features,
        "clients": {"models_profile": "global"},
    }
    if registry or tag:
        doc["images"] = {"registry": registry, "tag": tag}
    if oidc:
        doc["oidc"] = oidc
    return doc, notes


# ==============================================================================
# ecs — terraform output + 라이브 task def / service 상태
# ==============================================================================

def _sp(argv: list[str], *, cwd: Path | None = None, timeout: int = 60,
        tool: str = "") -> subprocess.CompletedProcess:
    """subprocess 래퍼 — 바이너리 부재/타임아웃을 CaptureError 로 번역."""
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        raise CaptureError(f"{tool or argv[0]} 명령을 못 찾았습니다 — 설치/PATH 확인 필요")
    except subprocess.TimeoutExpired:
        raise CaptureError(f"{tool or argv[0]} 응답 없음({timeout}s) — 네트워크/자격 확인 필요")


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _tf_outputs(env_dir: Path) -> dict:
    r = _sp(["terraform", "output", "-json"], cwd=env_dir, timeout=120, tool="terraform")
    if r.returncode != 0:
        raise CaptureError(f"terraform output 실패 (init/자격 확인 필요): {r.stderr.strip()[:200]}")
    try:
        return {k: v.get("value") for k, v in json.loads(r.stdout).items()}
    except json.JSONDecodeError as e:
        raise CaptureError(f"terraform output 파싱 실패: {e}")


def _aws_json(argv: list[str], region: str) -> dict:
    r = _sp(["aws", *argv, "--region", region, "--output", "json"], tool="aws")
    if r.returncode != 0:
        raise CaptureError(f"aws {' '.join(argv[:2])} 실패: {r.stderr.strip()[:200]}")
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError as e:
        raise CaptureError(f"aws {' '.join(argv[:2])} 출력 파싱 실패: {e}")


def _taskdef_env(region: str, task_arn: str) -> dict[str, str]:
    td = _aws_json(["ecs", "describe-task-definition", "--task-definition", task_arn], region)
    try:
        cenv = td["taskDefinition"]["containerDefinitions"][0].get("environment") or []
        return {e["name"]: e.get("value", "") for e in cenv}
    except (KeyError, IndexError):
        return {}


def capture_ecs(env_dir: Path, region: str) -> tuple[dict, list[str]]:
    notes: list[str] = []
    outputs = _tf_outputs(env_dir)

    cluster = outputs.get("ecs_cluster_name") or ""
    env_name = _strip(cluster, "llm-gateway-") or "captured"
    admin_ui_url = outputs.get("admin_ui_url") or ""
    https = bool(outputs.get("https_enabled"))

    # 도메인 — admin_ui_url 의 호스트가 admin.<domain> 이면 route53-acm 으로 추론
    m = re.match(r"https://admin\.([^/]+)", admin_ui_url)
    if m and https:
        domain = {"mode": "route53-acm", "name": m.group(1)}
        notes.append("zone_id 는 출력에 없어 비워둡니다 — Route53 콘솔에서 확인해 채우세요")
    else:
        domain = {"mode": "none"}

    # task def 에서 env 계약 역추출
    name_map = outputs.get("service_names") or {}
    service_names = list(name_map.values())
    image_tag, sizing, sender_type, sizing_env = "", {}, "mock", {}
    api_env_ok = True
    try:
        # admin-api task def — env 계약의 진원지. 실제 서비스 이름은 output 의
        # service_names 맵이 정본이다(고정 "admin-api" 가정은 다른 명명 배포에서
        # 조용히 mock 으로 떨어지는 함정)
        api_arn = _aws_json(
            ["ecs", "describe-services", "--cluster", cluster,
             "--services", name_map.get("admin-api", "admin-api")],
            region)["services"][0]["taskDefinition"]
        sizing_env = _taskdef_env(region, api_arn)
        sender_type = sizing_env.get("EMAIL_SENDER_TYPE", "mock")
    except (CaptureError, KeyError, IndexError) as e:
        api_env_ok = False
        notes.append(f"admin-api task def 조회 실패 — env 기반 항목은 추정입니다: {e}")
        notes.append("service_keys 는 읽기 실패로 false 기록 — 실제 값은 "
                     "ECS 콘솔/task def 에서 확인하세요")

    # 이미지 태그 + 레지스트리
    try:
        gws = _aws_json(["ecs", "describe-services", "--cluster", cluster,
                         "--services", *(service_names or ["gateway-proxy"])], region)["services"]
        live_to_logical = {v: k for k, v in name_map.items()}
        for s in gws:
            td_arn = s.get("taskDefinition", "")
            td = _aws_json(["ecs", "describe-task-definition",
                            "--task-definition", td_arn], region)["taskDefinition"]
            img = td["containerDefinitions"][0].get("image", "")
            if ":" in img:
                image_tag = img.rsplit(":", 1)[1]
            # deploy.sizing 키는 논리 서비스명 — 라이브명이 커스텀이면 역매핑
            logical = live_to_logical.get(s["serviceName"], s["serviceName"])
            sizing[logical] = {
                "cpu": _safe_int(td.get("cpu")),
                "memory": _safe_int(td.get("memory")),
                "desired_count": s.get("desiredCount", 0),
            }
    except (CaptureError, KeyError, IndexError) as e:
        notes.append(f"서비스 상태 조회 실패 — sizing 은 비워둡니다: {e}")

    notif = {"provider": sender_type if sender_type in ("mock", "ses", "smtp") else "mock"}
    if sender_type == "ses":
        notif["ses_from"] = sizing_env.get("EMAIL_SENDER_ADDRESS", "")
    elif sender_type == "smtp":
        notif.update({"smtp_host": sizing_env.get("SMTP_HOST", ""),
                      "smtp_port": _safe_int(sizing_env.get("SMTP_PORT"), 587),
                      "smtp_from": sizing_env.get("EMAIL_SENDER_ADDRESS", "")})

    # tier 추론 + infra.db/infra.cache — 실제 DB/캐시 토폴로지를 읽어 기록한다.
    # 읽은 값을 override 로 명시해야 채택 후 apply 가 tier 프리셋으로 되돌려
    # 실제 인프라를 다운사이즈하지 않는다.
    tier_signals: list[str] = []
    db_acu_max, cache_nodes = 0.0, 0
    infra_doc: dict = {}
    try:
        db_ep = outputs.get("db_endpoint") or ""
        clusters = _aws_json(
            ["rds", "describe-db-clusters"], region).get("DBClusters", [])
        for c in clusters:
            ep = (c.get("Endpoint") or "")
            if db_ep and ep and ep in db_ep:
                sc = c.get("ServerlessV2ScalingConfiguration") or {}
                if sc:
                    db_acu_max = float(sc.get("MaxCapacity") or 0)
                    infra_doc["db"] = {
                        "mode": "serverless",
                        "min_acu": float(sc.get("MinCapacity") or 0.5),
                        "max_acu": db_acu_max,
                    }
                    tier_signals.append(f"Aurora {infra_doc['db']['min_acu']}"
                                        f"~{db_acu_max}ACU(serverless)")
                else:
                    infra_doc["db"] = {
                        "mode": "provisioned",
                        "instance_class": c.get("DBClusterInstanceClass", ""),
                    }
                    tier_signals.append(
                        f"Aurora provisioned {c.get('DBClusterInstanceClass','?')}")
                break
    except (CaptureError, KeyError, ValueError):
        pass
    try:
        redis_ep = outputs.get("redis_endpoint") or ""
        rgs = _aws_json(
            ["elasticache", "describe-replication-groups"], region
        ).get("ReplicationGroups", [])
        for rg in rgs:
            node_groups = rg.get("NodeGroups", [{}])
            eps = [m.get("PrimaryEndpoint", {}).get("Address", "")
                   for m in node_groups]
            members = rg.get("MemberClusters", [])
            if redis_ep and (redis_ep in eps or
                             any(redis_ep in (m or "") for m in members)):
                node_type = rg.get("CacheNodeType", "")
                if rg.get("ClusterEnabled"):
                    # cluster mode — NodeGroups = 샤드, NodeGroupMembers-1 = 샤드당 replica
                    ng = node_groups or [{}]
                    replicas_live = max(
                        len(ng[0].get("NodeGroupMembers", [])) - 1, 0)
                    if replicas_live == 0:
                        # 스키마/모듈 최소가 1 — 라이브 0 을 표현 못 하므로
                        # 1 로 기록하되 upsize 임을 알린다
                        notes.append("cache cluster 의 샤드당 replica 가 0 입니다 — "
                                     "스키마 최소 1 로 기록했으니 채택 시 노드가 늘어납니다")
                    infra_doc["cache"] = {
                        "mode": "cluster", "node_type": node_type,
                        "num_node_groups": len(node_groups) or 1,
                        "replicas_per_node_group": replicas_live or 1,
                    }
                    cache_nodes = sum(
                        len(g.get("NodeGroupMembers", [])) or 1 for g in node_groups)
                    tier_signals.append(f"cache {node_type} {infra_doc['cache']['num_node_groups']}sh")
                else:
                    cache_nodes = len(members)
                    infra_doc["cache"] = {
                        "mode": "replicated" if cache_nodes > 1 else "single",
                        "node_type": node_type,
                        "num_clusters": cache_nodes or 1,
                    }
                    tier_signals.append(f"cache {node_type}x{cache_nodes}")
                break
    except (CaptureError, KeyError, ValueError):
        pass
    if infra_doc:
        notes.append("infra.db/infra.cache 를 라이브 스펙으로 기록했습니다 — "
                     "채택해도 apply 가 실제 인프라 크기를 유지합니다")

    gw_tasks = sizing.get("gateway-proxy", {}).get("desired_count", 1)
    # 신호 종합: task 4+/ACU>4/캐시 multi-node 중 하나라도 크면 t2
    is_big = gw_tasks >= 4 or db_acu_max > 4 or cache_nodes >= 2
    tier = "t2" if is_big else "t1"
    sig = ", ".join(tier_signals) or "읽지 못함"
    notes.append(
        f"size_tier={tier} 추론 근거: gateway task {gw_tasks}개, {sig}. "
        f"task 수/cpu/memory 는 deploy.sizing 에 라이브 값으로 기록했고 "
        f"ACU/캐시는 infra.* 에 기록했습니다 — 채택 전 실제 스펙과 대조하세요")

    oidc = {}
    issuer = outputs.get("cognito_issuer_url") or sizing_env.get("OIDC_ISSUER_URL", "")
    if issuer:
        oidc = {
            "issuer_url": issuer,
            "client_id": outputs.get("cognito_client_id") or sizing_env.get("OIDC_CLIENT_ID", ""),
            "required_group": sizing_env.get("OIDC_REQUIRED_GROUP", ""),
        }
        # authorize/token 은 Cognito hosted UI 규칙으로 재구성 가능
        hosted = outputs.get("cognito_hosted_ui_domain")
        if hosted:
            oidc["authorize_url"] = f"https://{hosted}/oauth2/authorize"
            oidc["token_url"] = f"https://{hosted}/oauth2/token"

    notes.append("allowed_cidrs 는 SG 를 읽지 않고 비워둡니다 — 실제 인입 대역을 "
                 "확인해 채우세요 (ALB SG 인그레스)")
    notes.append("tfstate_bucket/table 은 backend.hcl/backend.tf 를 보고 채우세요")

    doc = {
        "version": 1,
        "env": env_name,
        "aws": {"region": region},
        # 라이브 desired/cpu/memory 를 그대로 기록 — 채택 후 apply 가 task 수를
        # 프리셋으로 되돌리지 않고, DB 커넥션 예산 검증도 실제 수요로 계산된다
        "deploy": {"target": "ecs", "size_tier": tier, "sizing": sizing},
        "network": {"mode": "public", "allowed_cidrs": []},
        "domain": domain,
        # features — 스키마 키를 전부 기록한다 (못 읽은 기능 = false 명시,
        # 키 생략은 사용자에게 옵션 존재를 숨긴다).
        "features": {
            "notifications": notif,
            "bi_insight": bool(sizing_env.get("AGENTCORE_RUNTIME_ARN")),
            "pricing_lambda": bool(sizing_env.get("LITELLM_PRICING_LAMBDA")),
            "web_search": _is_true_env(sizing_env.get("WEB_SEARCH_ENABLED")),
            "body_logging": bool(sizing_env.get("FIREHOSE_STREAM_NAME")),
            # observability 는 compose 전용 — ecs 에서 true 를 기록하면
            # 캡처 문서 자체가 스키마 위반이 된다.
            "observability": False,
            "dev_login": _is_true_env(sizing_env.get("DEV_LOGIN_ENABLED")),
            # task def 읽기 실패(sizing_env={})면 앱 기본 true 로 추정해
            # service_keys=true 를 기록하게 된다 — 보안 플래그의 fail-open 이라
            # 읽기 실패는 false 로 두고 notes 로 실제 값 확인을 요구한다.
            "service_keys": (api_env_ok
                             and not _is_false_env(
                                 sizing_env.get("SERVICE_KEYS_ENABLED"))),
        },
        "images": {"tag": image_tag},
        "clients": {"models_profile": "global"},
    }
    if infra_doc:
        doc["infra"] = infra_doc
    if oidc:
        doc["oidc"] = oidc
    return doc, notes


# ==============================================================================
# eks — helm release 의 effective values + kubectl 라이브 상태
# ==============================================================================

def _helm_values(release: str, namespace: str, context: str = "") -> dict:
    argv = ["helm", "get", "values", release, "-n", namespace, "--all", "-o", "yaml"]
    if context:
        argv += ["--kube-context", context]
    r = _sp(argv, tool="helm")
    if r.returncode != 0:
        raise CaptureError(
            f"helm get values 실패 — 릴리스 '{release}' 가 ns '{namespace}' 에 있는지, "
            f"kubeconfig/context 를 확인하세요: {r.stderr.strip()[:200]}")
    return yaml.safe_load(r.stdout) or {}


def _helm_app_version(release: str, namespace: str, context: str = "") -> str:
    """설치된 릴리스의 차트 appVersion — values 에 tag 가 없는 서비스의
    폴백 이미지 태그 해석에 쓴다. 조회 실패는 best-effort 로 빈 문자열."""
    argv = ["helm", "list", "-n", namespace, "-o", "json",
            "--filter", f"^{re.escape(release)}$"]
    if context:
        argv += ["--kube-context", context]
    r = _sp(argv, tool="helm")
    if r.returncode != 0:
        return ""
    try:
        for item in json.loads(r.stdout or "[]"):
            if item.get("name") == release:
                return str(item.get("app_version", ""))
    except json.JSONDecodeError:
        pass
    return ""


def _kubectl_json(resource: str, namespace: str, context: str = "") -> dict:
    argv = ["kubectl", "get", resource, "-n", namespace, "-o", "json"]
    if context:
        argv += ["--context", context]
    try:
        r = _sp(argv, tool="kubectl")
    except CaptureError:
        return {}
    if r.returncode != 0:
        return {}  # 라이브 조회는 best-effort — values 만으로도 캡처는 된다
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _val(d: dict, *path, default=""):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


_HOST_KEY_MAP = {"gateway": "gateway", "admin_ui": "adminUi",
                 "admin_api": "adminApi"}


def _capture_hosts(domain: dict, ing: dict, notes: list[str]) -> None:
    """values ingress 호스트를 domain.hosts 로 채운다 — 파생 규칙
    (gateway./admin./admin-api.{name})과 다르게 쓰는 배포 채택 시 URL 보존.
    스키마와 같은 hostname 형식을 통과한 값만 넣는다."""
    base = domain.get("name", "")
    if not base:
        notes.append("ingress host 에서 도메인 베이스를 못 뽑았습니다 — "
                     "domain.name 을 확인하고 hosts 를 수동 채우세요")
        return
    hosts = {k: str(_val(ing, chart_key, "host"))
             for k, chart_key in _HOST_KEY_MAP.items()}
    derived = {"gateway": f"gateway.{base}", "admin_ui": f"admin.{base}",
               "admin_api": f"admin-api.{base}"}
    overrides = {k: h for k, h in hosts.items()
                 if h and h != derived[k] and _HOSTNAME_RE.match(h)}
    if not overrides:
        notes.append("ingress host 가 파생 규칙(gateway./admin./admin-api."
                     f"{base})과 일치합니다 — domain.hosts 생략")
        return
    domain["hosts"] = overrides
    notes.append(f"ingress hosts: gateway={hosts['gateway']} "
                 f"admin={hosts['admin_ui']} admin-api={hosts['admin_api']} — "
                 "파생 규칙과 달라 domain.hosts 로 캡처했습니다")
    skipped = {k: h for k, h in hosts.items()
               if h and h != derived[k] and k not in overrides}
    if skipped:
        notes.append(f"⚠ hostname 형식이 아닌 호스트는 제외했습니다: {skipped}")


def _domain_base(host: str, env: str) -> str:
    """gateway.<base> / gateway-<env>.<base> 형태에서 베이스 도메인만 추출."""
    for prefix in ("gateway.", f"gateway-{env}.", "gateway-"):
        if host.startswith(prefix):
            rest = host[len(prefix):]
            # gateway-<env>.<base> 는 .base 부분만
            if prefix == "gateway-" and "." in rest:
                return rest.split(".", 1)[1]
            return rest
    return ""


def capture_eks(namespace: str = "llm-gateway", release: str = "llm-gateway",
                context: str = "", env_dir: Path | None = None) -> tuple[dict, list[str]]:
    notes: list[str] = []
    v = _helm_values(release, namespace, context)

    env = str(_val(v, "global", "environment") or release)
    region = str(_val(v, "aws", "region"))
    genv = _val(v, "gatewayProxy", "env", default={}) or {}

    # ── 라이브 대조용 수집 ─────────────────────────────────────────────────
    live = _kubectl_json("ingress", namespace, context)
    deploys = _kubectl_json("deployments", namespace, context)

    # ── ingress → domain / allowed_cidrs ───────────────────────────────────
    ing = v.get("ingress") or {}
    ann = dict(ing.get("annotations") or {})
    cidrs_raw = ann.get("alb.ingress.kubernetes.io/inbound-cidrs", "")
    cidrs = [c.strip() for c in str(cidrs_raw).split(",") if c.strip()]

    gw_host = str(_val(ing, "gateway", "host"))
    cert_arn = ann.get("alb.ingress.kubernetes.io/certificate-arn", "")
    if cert_arn or str(_val(ing, "gateway", "tls", "enabled")) == "True":
        domain = {"mode": "route53-acm", "name": _domain_base(gw_host, env)}
        _capture_hosts(domain, ing, notes)
        notes.append("zone_id 는 values 에 없어 비워둡니다 — Route53 에서 확인해 채우세요")
    elif gw_host:
        domain = {"mode": "route53-acm", "name": _domain_base(gw_host, env)}
        _capture_hosts(domain, ing, notes)
        notes.append("ingress 에 인증서가 없어 HTTP 일 수 있습니다 — domain.mode 확인 필요")
    else:
        domain = {"mode": "none"}
        notes.append("ingress.host 비어 있음 — ALB DNS 직접 접근 모드로 추정")

    # 라이브 ingress 와 values 의 host 가 다른지 (kubectl 패치 드리프트)
    live_hosts = []
    live_schemes: dict[str, str] = {}
    for item in live.get("items", []):
        anns = (item.get("metadata") or {}).get("annotations") or {}
        for rule in (item.get("spec") or {}).get("rules") or []:
            live_hosts.append((item["metadata"]["name"], rule.get("host", "")))
        scheme = anns.get("alb.ingress.kubernetes.io/scheme")
        if scheme:
            live_schemes[item["metadata"]["name"]] = scheme
        live_cidrs = anns.get("alb.ingress.kubernetes.io/inbound-cidrs")
        if live_cidrs and live_cidrs != cidrs_raw:
            notes.append(f"⚠ ingress/{item['metadata']['name']} 의 live inbound-cidrs "
                         f"({live_cidrs})가 helm values 와 다릅니다 — kubectl 패치 드리프트")

    def _scheme(suffix: str, values_sec: str) -> str:
        for nm, sc in live_schemes.items():
            if nm.endswith(f"-{suffix}"):
                return sc
        sec_anns = _val(ing, values_sec, "annotations", default={}) or {}
        return str(sec_anns.get("alb.ingress.kubernetes.io/scheme", ""))

    gw_scheme = _scheme("gateway", "gateway") or str(ann.get(
        "alb.ingress.kubernetes.io/scheme", ""))
    ui_scheme = _scheme("admin-ui", "adminUi") or gw_scheme
    api_scheme = _scheme("admin-api", "adminApi") or gw_scheme
    net_mode = "private" if gw_scheme == "internal" else "public"
    admin_internal = (net_mode == "public"
                      and (ui_scheme == "internal" or api_scheme == "internal"))
    if admin_internal:
        notes.append("admin Ingress 만 scheme=internal — network.admin_internal=true 로 캡처")

    # ── images — per-service 태그가 다르면 images.tags 맵으로 기록한다 ───────
    #    (US-19 같은 멀티태그 릴리스 — 단일 images.tag 로는 표현 불가)
    svc_keys = (("gateway-proxy", "gatewayProxy"), ("admin-api", "adminApi"),
                ("admin-ui", "adminUi"), ("scheduler", "scheduler"),
                ("notification-worker", "notificationWorker"),
                ("cost-recorder-worker", "costRecorderWorker"),
                ("migration", "migration"))
    helm_keys = dict(svc_keys)

    # 라이브 deployment 의 실제 이미지 태그 수집 — deployment 명명 = <fullname>-<svc>.
    # fullname 은 release/chart override 로 달라지므로 접미사로 svc 를 맞춘다
    # (하드코딩 prefix 는 커스텀 release 에서 매칭이 침묵 누락된다).
    # values 에 tag 가 없는 서비스는 차트 appVersion 폴백으로 이미지가 정해지므로
    # 라이브 태그가 곧 유효 태그다 — 기록하지 않으면 채택본 apply 가 폴백 대상
    # 서비스를 global images.tag 로 덮어쓴다.
    live_tags: dict[str, str] = {}
    for item in deploys.get("items", []):
        name = item["metadata"]["name"]
        svc_key = next((s for s in helm_keys if name.endswith(f"-{s}")), "")
        if not svc_key:
            continue
        containers = ((item.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
        for c in containers:
            img = c.get("image", "")
            live_tag = img.rsplit(":", 1)[-1] if ":" in img else ""
            if live_tag:
                live_tags[svc_key] = live_tag

    # values 미명시 + 라이브 없음(migration Job 등)의 최종 폴백 = 차트 appVersion
    app_version = _helm_app_version(release, namespace, context)

    tags = {}
    for svc, key in svc_keys:
        declared = str(_val(v, key, "image", "tag"))
        live_tag = live_tags.get(svc, "")
        if live_tag and declared and live_tag != declared:
            notes.append(f"⚠ {svc} 라이브 이미지 태그 {live_tag} ≠ helm values "
                         f"{declared} — 수동 set image 또는 stale release. 라이브 값으로 캡처")
        elif live_tag and not declared and app_version and live_tag != app_version:
            notes.append(f"⚠ {svc} 라이브 이미지 태그 {live_tag} ≠ 차트 appVersion "
                         f"{app_version} 폴백 — 수동 set image 추정. 라이브 값으로 캡처")
        # 캡처는 현재 상태 기준 — 라이브 > values 명시 > 차트 폴백 순
        tags[svc] = live_tag or declared or app_version
    unique = {t for t in tags.values() if t}
    tag = tags.get("gateway-proxy", "")
    per_service = ({helm_keys[s]: t for s, t in tags.items() if t}
                   if len(unique) > 1 else {})

    # ── features ────────────────────────────────────────────────────────────
    email = _val(v, "notificationWorker", "email", default={}) or {}
    provider = str(email.get("provider", "mock"))
    notif = {"provider": provider}
    if provider == "ses":
        notif["ses_from"] = str(_val(email, "ses", "fromAddress"))
    elif provider == "smtp":
        smtp = _val(email, "smtp", default={}) or {}
        notif.update({
            "smtp_host": str(smtp.get("host", "")),
            "smtp_port": smtp.get("port", 587),
            "smtp_from": str(smtp.get("fromAddress", "")),
            "smtp_credentials_secret": str(smtp.get("credentialsSecretName", "")),
        })
    elif provider == "internal_api":
        ia = _val(email, "internalApi", default={}) or {}
        notif.update({
            "internal_api_url": str(ia.get("url", "")),
            "internal_api_from": str(ia.get("fromAddress", "")),
        })
    elif provider != "mock":
        notes.append(f"email.provider={provider} 는 스키마에 없는 값 — "
                     "notifications.provider 를 직접 맞추세요")

    otel_mode = str(_val(v, "observability", "otel", "mode"))
    aenv = _val(v, "adminApi", "env", default={}) or {}
    # 스키마 키를 전부 기록한다 — 못 읽은 기능은 false 로 명시. 키가 빠지면
    # 캡처된 yaml 을 보는 사용자가 그 옵션의 존재 자체를 모른다.
    features = {
        "notifications": notif,
        "bi_insight": bool(aenv.get("AGENTCORE_RUNTIME_ARN")),
        "pricing_lambda": bool(aenv.get("LITELLM_PRICING_LAMBDA")),
        "web_search": _is_true_env(genv.get("WEB_SEARCH_ENABLED")),
        "observability": otel_mode not in ("", "disabled", "none"),
        "dev_login": False,
        # values 에 손으로 false(비인용 bool)를 박으면 str(False)="False" 가
        # 되어 true 로 오인한다 — _is_false_env 로 정규화.
        "service_keys": not _is_false_env(aenv.get("SERVICE_KEYS_ENABLED")),
    }
    # body_logging 은 eks managed overlay 가 "켜기"만 소유하므로 false 를
    # 박지 않는다 — 박으면 validate 마다 수동 비활성화 경고가 난다.
    if genv.get("FIREHOSE_STREAM_NAME"):
        features["body_logging"] = True
    else:
        notes.append("body_logging 미감지(FIREHOSE_STREAM_NAME 없음) — "
                     "키를 생략했습니다. 켜려면 features.body_logging: true")

    # dev login — 라이브에 DEV_LOGIN_ENABLED=true 가 켜져 있으면 보존해야 한다.
    # 안 잡으면 채택 첫 apply 에 oidc 설정 경로가 adminUi 를 "false" 로 되돌려
    # 운영 중이던 개발자 로그인이 꺼진다.
    if (_is_true_env(_val(v, "adminUi", "env", "DEV_LOGIN_ENABLED"))
            or _is_true_env(aenv.get("DEV_LOGIN_ENABLED"))):
        features["dev_login"] = True
        notes.append("dev login(DEV_LOGIN_ENABLED=true)이 켜져 있습니다 — "
                     "features.dev_login: true 로 기록했습니다. 의도가 아니면 "
                     "false 로 바꾸세요 (인증 없는 관리자 로그인)")

    # ── oidc — adminApi.oidc + adminUi.env (admin-ui 는 env 직접 주입) ───────
    oidc = {}
    issuer = str(_val(v, "adminApi", "oidc", "issuerUrl") or _val(v, "gatewayProxy", "oidc", "issuerUrl"))
    if issuer:
        ui_env = _val(v, "adminUi", "env", default={}) or {}
        oidc = {
            "issuer_url": issuer,
            "client_id": str(ui_env.get("OIDC_CLIENT_ID", "")),
            "authorize_url": str(ui_env.get("OIDC_AUTHORIZE_URL", "")),
            "token_url": str(ui_env.get("OIDC_TOKEN_URL", "")),
            "provider_name": str(_val(v, "adminApi", "oidc", "providerName", default="oidc:cognito")),
            "required_group": str(_val(v, "adminApi", "oidc", "requiredGroup") or
                                  _val(v, "gatewayProxy", "oidc", "requiredGroup")),
        }
        vk_ttl = _val(v, "adminApi", "oidc", "vkTtlHours")
        if vk_ttl not in (None, ""):
            oidc["vk_ttl_hours"] = vk_ttl
        if not ui_env.get("OIDC_CLIENT_ID"):
            notes.append("adminUi.env.OIDC_CLIENT_ID 를 못 읽었습니다 — 확인해 채우세요")
    else:
        notes.append("OIDC issuer 없음 — admin-ui 는 dev-login 모드일 수 있습니다")

    # ── terraform outputs (env_dir 있으면) — region/cognito 보강 ─────────────
    if env_dir and env_dir.exists():
        try:
            outs = _tf_outputs(env_dir)
            if not region:
                region = str(outs.get("aws_region", "") or outs.get("region", ""))
            if not oidc.get("issuer_url") and outs.get("cognito_issuer_url"):
                oidc["issuer_url"] = outs["cognito_issuer_url"]
                notes.append("oidc.issuer_url 을 terraform output 에서 가져왔습니다")
            # helm values(ESO/secretKeyRef 경로)에 리터럴이 없어 비워진 OIDC 필드는
            # terraform output 으로 복원 — ecs capture 와 동일 규칙.
            if oidc:
                if not oidc.get("client_id") and outs.get("cognito_client_id"):
                    oidc["client_id"] = outs["cognito_client_id"]
                    notes.append("oidc.client_id 를 terraform output 에서 가져왔습니다")
                hosted = outs.get("cognito_hosted_ui_domain")
                if hosted:
                    if not oidc.get("authorize_url"):
                        oidc["authorize_url"] = f"https://{hosted}/oauth2/authorize"
                    if not oidc.get("token_url"):
                        oidc["token_url"] = f"https://{hosted}/oauth2/token"
        except CaptureError:
            notes.append(f"{env_dir} 의 terraform output 을 못 읽었습니다 — region 등을 확인하세요")

    # 시크릿 공급 경로 — chart 는 externalSecrets(ESO) 또는 .Values.secrets 중
    # 하나가 없으면 pod 가 기동하지 못한다. 캡처본엔 이 설정이 없으니 상태를 기록한다.
    eso = _val(v, "externalSecrets", "enabled")
    inline_secrets = bool(_val(v, "secrets", default={}) or {})
    if str(eso).lower() == "true":
        notes.append("시크릿 경로: externalSecrets(ESO) 사용 중 — 채택 후 apply 도 이 "
                     "전제가 유지돼야 합니다 (env values 가 공급)")
    elif inline_secrets:
        notes.append("시크릿 경로: chart .Values.secrets 인라인 — env values 파일이 "
                     "공급 중인지 확인하세요 (values 파일은 git 에 올리지 말 것)")
    else:
        notes.append("⚠️ 시크릿 공급 경로를 helm values 에서 못 읽었습니다 — "
                     "externalSecrets/.Values.secrets 둘 다 비어 있으면 채택 후 "
                     "apply 시 migration Job과 pod 가 기동하지 못합니다")

    notes.append("size_tier 는 eks=t3 으로 둡니다 — 실제 리소스 스펙은 HPA/requests 참조")
    notes.append("externalSecrets/ESO, Fargate profile, RDS Proxy 등 eks 고유 구성은 "
                 "gateway.yaml 에 표현이 없습니다 — 캡처본만으로 eks 재배포는 아직 불가")

    doc = {
        "version": 1,
        "env": env,
        "aws": {"region": region},
        # 실제 릴리스/namespace/env_dir 를 기록 — 기본값이 아닌 배포도
        # 채택 후 apply 가 같은 대상을 가리키도록
        "deploy": {"target": "eks", "size_tier": "t3",
                   "release": release, "namespace": namespace,
                   "tf_env_dir": str(env_dir) if env_dir else ""},
        "network": {"mode": net_mode, "allowed_cidrs": cidrs,
                    "admin_internal": admin_internal},
        "domain": domain,
        "features": features,
        "images": {"tag": tag},
        "clients": {"models_profile": "global"},
    }
    if per_service:
        doc["images"]["tags"] = per_service
    if _val(v, "global", "imageRegistry"):
        doc["images"]["registry"] = str(v["global"]["imageRegistry"])
    if oidc:
        doc["oidc"] = oidc
    return doc, notes


# ==============================================================================
# 비교 — 선언된 doc 과 캡처된 doc 의 차이를 key path 단위로
# ==============================================================================

def _flatten(d: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in (d or {}).items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def diff_docs(declared: dict, captured: dict) -> list[tuple[str, object, object]]:
    """(path, declared_value, captured_value) — 어느 쪽이든 값이 다르면 포함."""
    fd, fc = _flatten(declared), _flatten(captured)
    diffs = []
    for key in sorted(set(fd) | set(fc)):
        dv, cv = fd.get(key), fc.get(key)
        if dv != cv:
            diffs.append((key, dv, cv))
    return diffs


_MISSING = object()


def _lookup(doc: dict, dotted: str):
    """점 경로로 중첩 dict 를 내려간다 — 경로가 없으면 _MISSING."""
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def dropped_keys(diffs: list[tuple[str, object, object]],
                 captured_raw: dict) -> list[tuple[str, object]]:
    """캡처본을 통째로 채택(cp)하면 삭제되는 yaml 전용 키 — (path, declared_value).

    "못 읽은 키" 판정은 **원본** 캡처 doc 으로 한다. 정규화본(`asdict(from_dict)`)
    은 스키마 기본값("", False, 0)으로 빈칸을 채워, 캡처가 못 읽은 문자열 키
    (tfstate, oidc.client_secret — 캡처 자체가 방출하지 않음)와 "실제로 빈
    값이었던 키"를 구별하지 못한다. 원본 기준: 경로 부재 또는 ""/None 이면
    읽기 불가 — cp 는 물론 interactive 의 absorb 도 선언값을 지운다.
    []/{} 는 캡처가 명시적으로 방출하는 "읽은 빈 값"이라 absorb 대상으로 둔다
    (예: ecs allowed_cidrs — 라이브가 실제로 비어 있으면 채택 가능해야 한다).
    비어있는 선언값 자체는 지워져도 무의미하므로 제외한다.
    """
    out = []
    for k, dv, _cv in diffs:
        if dv in (None, [], {}, ""):
            continue
        rv = _lookup(captured_raw, k)
        if rv is _MISSING or rv is None or rv == "":
            out.append((k, dv))
    return out
