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

    composed = yaml.safe_load(compose_path.read_text())
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
                      "smtp_port": int(env.get("SMTP_PORT", "587") or 587),
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

def _tf_outputs(env_dir: Path) -> dict:
    r = subprocess.run(["terraform", "output", "-json"], cwd=env_dir,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise CaptureError(f"terraform output 실패 (init/자격 확인 필요): {r.stderr.strip()[:200]}")
    try:
        return {k: v.get("value") for k, v in json.loads(r.stdout).items()}
    except json.JSONDecodeError as e:
        raise CaptureError(f"terraform output 파싱 실패: {e}")


def _aws_json(argv: list[str], region: str) -> dict:
    r = subprocess.run(["aws", *argv, "--region", region, "--output", "json"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise CaptureError(f"aws {' '.join(argv[:2])} 실패: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout or "{}")


def _taskdef_env(env_dir: Path, region: str, task_arn: str) -> dict[str, str]:
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
    api_td = (outputs.get("service_names") or {})
    image_tag, sizing, sender_type, sizing_env = "", {}, "mock", {}
    try:
        # admin-api task def — env 계약의 진원지
        api_arn = _aws_json(
            ["ecs", "describe-services", "--cluster", cluster,
             "--services", "admin-api"], region)["services"][0]["taskDefinition"]
        sizing_env = _taskdef_env(env_dir, region, api_arn)
        sender_type = sizing_env.get("EMAIL_SENDER_TYPE", "mock")
    except (CaptureError, KeyError, IndexError) as e:
        notes.append(f"admin-api task def 조회 실패 — env 기반 항목은 추정입니다: {e}")

    # 이미지 태그 + 레지스트리
    try:
        gws = _aws_json(["ecs", "describe-services", "--cluster", cluster,
                         "--services", *list(api_td or ["gateway-proxy"])], region)["services"]
        for s in gws:
            td_arn = s.get("taskDefinition", "")
            td = _aws_json(["ecs", "describe-task-definition",
                            "--task-definition", td_arn], region)["taskDefinition"]
            img = td["containerDefinitions"][0].get("image", "")
            if ":" in img:
                image_tag = img.rsplit(":", 1)[1]
            sizing[s["serviceName"]] = {
                "cpu": int(td.get("cpu", "0") or 0),
                "memory": int(td.get("memory", "0") or 0),
                "desired_count": s.get("desiredCount", 0),
            }
    except (CaptureError, KeyError, IndexError) as e:
        notes.append(f"서비스 상태 조회 실패 — sizing 은 비워둡니다: {e}")

    notif = {"provider": sender_type if sender_type in ("mock", "ses", "smtp") else "mock"}
    if sender_type == "ses":
        notif["ses_from"] = sizing_env.get("EMAIL_SENDER_ADDRESS", "")
    elif sender_type == "smtp":
        notif.update({"smtp_host": sizing_env.get("SMTP_HOST", ""),
                      "smtp_port": int(sizing_env.get("SMTP_PORT", "587") or 587),
                      "smtp_from": sizing_env.get("EMAIL_SENDER_ADDRESS", "")})

    # tier 추론 — serverless ACU 범위는 출력에 없어 sizing/env 로 유추
    tier = "t1"
    if sizing.get("gateway-proxy", {}).get("desired_count", 1) >= 4:
        tier = "t2"
    notes.append(f"size_tier 는 task 수로 추론(t{tier[-1]}) — DB/캐시 토폴로지는 "
                 "tfvars 기대값과 맞는지 validate 후 확인하세요")

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
