# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""capture — 이미 배포된 환경을 읽어 gateway.yaml 후보를 역생성한다.

목적: imperative 스크립트/수작업으로 배포된 환경을 선언적 관리로 온보딩.
캡처가 못 읽는 값은 빈칸 + notes 로 남기고, 기존 gateway.yaml 과 다른 값은
호출자가 apply(yaml 유지)/absorb(캡처 채택)/skip 을 선택하게 한다.

backend 별 정보원:
  compose — gen/<env>/ 의 docker-compose.yml + .env + Caddyfile (정적 산출물)
  ecs     — terraform output + aws ecs describe-task-definition / services
  eks     — 미구현 (helm get values + terraform output 조합이 필요)
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from .render.common import parse_env_file
from .render.compose import OBSERVABILITY, WEB_PORTS


class CaptureError(Exception):
    pass


def _strip(s: str, prefix: str) -> str:
    return s[len(prefix):] if s.startswith(prefix) else s


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

    # features
    features = {
        "notifications": notif,
        "observability": any(s in services for s in OBSERVABILITY),
        "web_search": env.get("WEB_SEARCH_ENABLED") == "true",
        "body_logging": bool(env.get("FIREHOSE_STREAM_NAME")),
    }

    # oidc — admin-ui environment(직접 주입된 것) + .env
    ui_env = (services.get("admin-ui") or {}).get("environment") or {}
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
    service_names = list((outputs.get("service_names") or {}).values())
    image_tag, sizing, sender_type, sizing_env = "", {}, "mock", {}
    try:
        # admin-api task def — env 계약의 진원지
        api_arn = _aws_json(
            ["ecs", "describe-services", "--cluster", cluster,
             "--services", "admin-api"], region)["services"][0]["taskDefinition"]
        sizing_env = _taskdef_env(region, api_arn)
        sender_type = sizing_env.get("EMAIL_SENDER_TYPE", "mock")
    except (CaptureError, KeyError, IndexError) as e:
        notes.append(f"admin-api task def 조회 실패 — env 기반 항목은 추정입니다: {e}")

    # 이미지 태그 + 레지스트리
    try:
        gws = _aws_json(["ecs", "describe-services", "--cluster", cluster,
                         "--services", *(service_names or ["gateway-proxy"])], region)["services"]
        for s in gws:
            td_arn = s.get("taskDefinition", "")
            td = _aws_json(["ecs", "describe-task-definition",
                            "--task-definition", td_arn], region)["taskDefinition"]
            img = td["containerDefinitions"][0].get("image", "")
            if ":" in img:
                image_tag = img.rsplit(":", 1)[1]
            sizing[s["serviceName"]] = {
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

    # tier 추론 — 실제 DB/캐시 토폴로지를 읽어 task 수와 교차검증한다.
    # task 수만 보면 운영 중 축소된 배포를 과소평가해 채택 후 apply 가
    # ACU/캐시를 다운사이즈한다 — 실제 스펙을 우선으로 읽는다.
    tier_signals: list[str] = []
    db_acu_max, cache_nodes = 0.0, 0
    try:
        db_ep = outputs.get("db_endpoint") or ""
        clusters = _aws_json(
            ["rds", "describe-db-clusters"], region).get("DBClusters", [])
        for c in clusters:
            ep = (c.get("Endpoint") or "")
            if db_ep and ep and ep in db_ep:
                sc = c.get("ServerlessV2ScalingConfiguration") or {}
                db_acu_max = float(sc.get("MaxCapacity") or 0)
                tier_signals.append(f"Aurora max {db_acu_max}ACU"
                                    + ("(serverless)" if sc else f"({c.get('DBClusterInstanceClass','?')})"))
                break
    except (CaptureError, KeyError, ValueError):
        pass
    try:
        redis_ep = outputs.get("redis_endpoint") or ""
        rgs = _aws_json(
            ["elasticache", "describe-replication-groups"], region
        ).get("ReplicationGroups", [])
        for rg in rgs:
            eps = [m.get("PrimaryEndpoint", {}).get("Address", "")
                   for m in rg.get("NodeGroups", [{}])]
            members = rg.get("MemberClusters", [])
            if redis_ep and (redis_ep in eps or
                             any(redis_ep in (m or "") for m in members)):
                cache_nodes = len(members)
                tier_signals.append(f"cache {rg.get('CacheNodeType','?')}x{cache_nodes}")
                break
    except (CaptureError, KeyError, ValueError):
        pass

    gw_tasks = sizing.get("gateway-proxy", {}).get("desired_count", 1)
    # 신호 종합: task 4+/ACU>4/캐시 multi-node 중 하나라도 크면 t2
    is_big = gw_tasks >= 4 or db_acu_max > 4 or cache_nodes >= 2
    tier = "t2" if is_big else "t1"
    sig = ", ".join(tier_signals) or "읽지 못함"
    notes.append(
        f"size_tier={tier} 추론 근거: gateway task {gw_tasks}개, {sig}. "
        f"⚠️ 실제보다 작게 추론되면 다음 apply 가 ACU/캐시/task를 축소합니다 — "
        f"채택 전 실제 스펙과 대조하세요")

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
        "deploy": {"target": "ecs", "size_tier": tier, "sizing": {}},
        "network": {"mode": "public", "allowed_cidrs": []},
        "domain": domain,
        "features": {
            "notifications": notif,
            "body_logging": bool(sizing_env.get("FIREHOSE_STREAM_NAME")),
            "web_search": sizing_env.get("WEB_SEARCH_ENABLED") == "true",
        },
        "images": {"tag": image_tag},
        "clients": {"models_profile": "global"},
    }
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
        notes.append(f"ingress hosts: gateway={gw_host} admin={_val(ing,'adminUi','host')} "
                     f"admin-api={_val(ing,'adminApi','host')} — 우리 네이밍 규칙"
                     f"(gateway./admin./admin-api.{{domain}})과 다르면 직접 맞추세요")
        notes.append("zone_id 는 values 에 없어 비워둡니다 — Route53 에서 확인해 채우세요")
    elif gw_host:
        domain = {"mode": "route53-acm", "name": _domain_base(gw_host, env)}
        notes.append("ingress 에 인증서가 없어 HTTP 일 수 있습니다 — domain.mode 확인 필요")
    else:
        domain = {"mode": "none"}
        notes.append("ingress.host 비어 있음 — ALB DNS 직접 접근 모드로 추정")

    # 라이브 ingress 와 values 의 host 가 다른지 (kubectl 패치 드리프트)
    live_hosts = []
    for item in live.get("items", []):
        anns = (item.get("metadata") or {}).get("annotations") or {}
        for rule in (item.get("spec") or {}).get("rules") or []:
            live_hosts.append((item["metadata"]["name"], rule.get("host", "")))
        live_cidrs = anns.get("alb.ingress.kubernetes.io/inbound-cidrs")
        if live_cidrs and live_cidrs != cidrs_raw:
            notes.append(f"⚠ ingress/{item['metadata']['name']} 의 live inbound-cidrs "
                         f"({live_cidrs})가 helm values 와 다릅니다 — kubectl 패치 드리프트")

    # ── images — per-service 태그가 다르면 images.tags 맵으로 기록한다 ───────
    #    (US-19 같은 멀티태그 릴리스 — 단일 images.tag 로는 표현 불가)
    tags = {}
    helm_keys = {}
    for svc, key in (("gateway-proxy", "gatewayProxy"), ("admin-api", "adminApi"),
                     ("admin-ui", "adminUi"), ("scheduler", "scheduler"),
                     ("notification-worker", "notificationWorker"),
                     ("cost-recorder-worker", "costRecorderWorker"),
                     ("migration", "migration")):
        tags[svc] = str(_val(v, key, "image", "tag"))
        helm_keys[svc] = key
    unique = {t for t in tags.values() if t}
    tag = tags.get("gateway-proxy", "")
    per_service = ({helm_keys[s]: t for s, t in tags.items() if t}
                   if len(unique) > 1 else {})

    # 라이브 이미지 drift
    for item in deploys.get("items", []):
        name = item["metadata"]["name"]
        containers = ((item.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
        for c in containers:
            img = c.get("image", "")
            live_tag = img.rsplit(":", 1)[-1] if ":" in img else ""
            svc_key = name.replace("llm-gateway-", "")
            if svc_key in tags and live_tag and live_tag != tags[svc_key]:
                notes.append(f"⚠ {svc_key} 라이브 이미지 태그 {live_tag} ≠ helm values "
                             f"{tags[svc_key]} — 수동 set image 또는 stale release")

    # ── features ────────────────────────────────────────────────────────────
    email = _val(v, "notificationWorker", "email", default={}) or {}
    provider = str(email.get("provider", "mock"))
    notif = {"provider": provider}
    if provider == "ses":
        notif["ses_from"] = str(_val(email, "ses", "fromAddress"))
    elif provider not in ("mock", "ses", "smtp"):
        notes.append(f"email.provider={provider} 는 스키마에 없는 값(internal_api 등) — "
                     "notifications.provider 를 직접 맞추세요")

    otel_mode = str(_val(v, "observability", "otel", "mode"))
    features = {
        "notifications": notif,
        "observability": otel_mode not in ("", "disabled", "none"),
        "web_search": str(genv.get("WEB_SEARCH_ENABLED")) == "true",
        "body_logging": bool(genv.get("FIREHOSE_STREAM_NAME")),
    }

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
        "network": {"mode": "public", "allowed_cidrs": cidrs},
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
