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

from .render.common import parse_env_file, tfvars_clean as _tfvars_clean
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

    부재(None)는 false 가 아니다 — "읽지 못함" 과 "명시적 off" 를 구분해
    캡처가 무엇을 모르는지 표현할 수 있게 한다.
    """
    return str(v if v is not None else "").strip().lower() in ("false", "0", "no", "off")


# ==============================================================================
# compose — 정적 산출물 역분석 (라이브 상태와 다를 수 있음 — doctor 가 별도 확인)
# ==============================================================================

def capture_compose(out_dir: Path, unknown: set[str] | None = None
                    ) -> tuple[dict, list[str]]:
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
    services = _dmap(composed.get("services"))
    env_path = out_dir / ".env"
    env = parse_env_file(env_path)
    env_missing = not env_path.exists()
    caddy_path = out_dir / "Caddyfile"
    try:
        caddy = caddy_path.read_text() if caddy_path.exists() else ""
    except (OSError, UnicodeDecodeError):
        caddy = ""
        notes.append("Caddyfile 읽기 실패(인코딩/권한) — domain/cidrs 추론 불가")
    if not caddy:
        notes.append("Caddyfile 없음/빈 내용 — allowed_cidrs/domain 은 추론 불가로 둡니다")
        if unknown is not None:
            # 읽기 실패를 "비어 있음/none" 으로 기록하면 채택 diff 가 방화벽
            # 허용목록 삭제·도메인 해제 행으로 보인다 — 못 읽은 키는 unknown.
            unknown.update({"network.allowed_cidrs", "domain.mode"})

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
        img = _dmap(services.get(name)).get("image", "")
        if img and "/" in img and ":" in img:
            registry = img.rsplit("/", 1)[0]
            tag = img.rsplit(":", 1)[1]
            break

    # notifications
    provider = env.get("EMAIL_SENDER_TYPE", "mock")
    notif = {"provider": provider if provider in
             ("mock", "ses", "smtp", "internal_api") else "mock"}
    if provider == "ses":
        notif["ses_from"] = env.get("EMAIL_SENDER_ADDRESS", "")
    elif provider == "smtp":
        notif.update({"smtp_host": env.get("SMTP_HOST", ""),
                      "smtp_port": _safe_int(env.get("SMTP_PORT"), 587),
                      "smtp_from": env.get("EMAIL_SENDER_ADDRESS", "")})
    elif provider == "internal_api":
        notif["internal_api_url"] = env.get("EMAIL_API_URL", "")
        notif["internal_api_from"] = env.get("EMAIL_SENDER_ADDRESS", "")
        if not notif["internal_api_url"]:
            notes.append("EMAIL_SENDER_TYPE=internal_api 인데 EMAIL_API_URL 이 "
                         "없습니다 — notifications.internal_api_url 을 채우세요 "
                         "(없으면 스키마 오류로 채택이 막힙니다)")
    elif provider not in ("mock", "ses", "smtp", "internal_api"):
        notes.append(f"EMAIL_SENDER_TYPE={provider} 는 스키마에 없는 값 — mock 으로 두고 확인하세요")

    # features — 스키마 키를 전부 기록한다. 라이브 신호를 못 읽은 기능은
    # false 로 명시해야 캡처 결과가 "기능 목록" 역할을 한다(키가 아예
    # 빠지면 사용자는 그 옵션이 존재하는지 알 수 없다).
    def _env_map(svc: str) -> dict:
        e = _dmap(services.get(svc)).get("environment")
        if isinstance(e, list):
            # compose 의 list 형식 — 비-str 항목(environment: [123])은 건너뛴다
            return dict(x.split("=", 1) for x in e
                        if isinstance(x, str) and "=" in x)
        return _dmap(e)

    api_env = _env_map("admin-api")
    ui_env = _env_map("admin-ui")
    # SERVICE_KEYS_ENABLED — 렌더러는 .env(env_file)에만 쓰고 environment: 블록은
    # override 라 api_env 우선. 둘 다 부재면 앱 기본 false(fail-closed)가
    # 진리다 — 보안 플래그는 "못 읽으면 꺼짐" 쪽으로 기록해야 채택이 안전하다.
    sk_env = api_env.get("SERVICE_KEYS_ENABLED")
    if sk_env is None:
        sk_env = env.get("SERVICE_KEYS_ENABLED")
    if sk_env is None:
        if unknown is not None:
            unknown.add("features.service_keys")
        notes.append(
            "SERVICE_KEYS_ENABLED 를 못 읽었습니다 — false 기록. 앱 기본은 "
            "false(구 이미지에서 env 없이 켜져 있던 배포라면 true 로 바로잡고 "
            "환경에 env 를 명시하세요)")
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
        "service_keys": _is_true_env(sk_env),
        # alerting 은 compose 미지원 — 스키마가 비기본 키를 거부하므로 기본값
        # 블록만 기록한다 (키 존재를 사용자에게 보이되 채택은 무효가 아님).
        "alerting": {"enabled": False, "emails": [], "log_shipping": False},
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

    if env_missing:
        # .env 가 없으면 env 기반 항목은 "못 읽음"이다 — compose environment
        # 블록 값만으로 추론한 false/mock 기록이 채택 apply 에서 실제 설정을
        # 내리지 않게 unknown 표기 (다른 소스에서 값을 확인한 키는 제외).
        notes.append("⚠ .env 파일이 없습니다 — env 기반 항목(기능 플래그/알림/"
                     "OIDC/region)은 추정입니다. 채택 전 실제 배포의 .env 를 "
                     "확인하세요")
        if unknown is not None:
            unknown.update({"aws.region", "features.web_search",
                            "features.body_logging",
                            "features.notifications.provider"})
            for _k in ("bi_insight", "pricing_lambda", "dev_login"):
                if not features.get(_k):
                    unknown.add(f"features.{_k}")
            if not oidc:
                unknown.add("oidc.issuer_url")

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
    if not env_dir.is_dir():
        # cwd 부재는 FileNotFoundError 로 'terraform 설치' 오진이 난다 — 먼저 확인
        raise CaptureError(f"{env_dir} 가 없습니다 — --env-dir 로 terraform 환경 "
                           "디렉토리를 지정하세요")
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


def _taskdef_env(region: str, task_arn: str,
                 container_name: str = "") -> dict[str, str]:
    td = _aws_json(["ecs", "describe-task-definition", "--task-definition", task_arn], region)
    try:
        defs = td["taskDefinition"]["containerDefinitions"]
        # 사이드카(firelens/otel 등)가 첫 번째 정의면 container[0] 이 앱이 아니다 —
        # 논리 서비스명과 같은 컨테이너를 우선 고른다.
        c = next((x for x in defs
                  if container_name and x.get("name") == container_name), defs[0])
        cenv = c.get("environment") or []
        return {e["name"]: e.get("value", "") for e in cenv
                if isinstance(e, dict) and "name" in e}
    except (KeyError, IndexError):
        return {}


def capture_ecs(env_dir: Path, region: str,
                unknown: set[str] | None = None) -> tuple[dict, list[str]]:
    notes: list[str] = []
    outputs = _tf_outputs(env_dir)

    cluster = outputs.get("ecs_cluster_name") or ""
    env_name = _strip(cluster, "llm-gateway-") or "captured"
    # 공유 env 디렉토리의 .terraform 캐시는 마지막 init 한 env 의 backend 를
    # 가리킨다 — 다른 env 의 state 를 읽으면 env 이름만 정직하고 값 전체가
    # 잘못된다. backend key( ecs/<env>/terraform.tfstate )와 대조한다.
    _bk = str(_tf_backend(env_dir).get("key", ""))
    if _bk and env_name != "captured" and not _bk.endswith(
            f"{env_name}/terraform.tfstate"):
        notes.append(
            f"⚠ 캐시된 terraform backend key '{_bk}' 가 env '{env_name}' 과 "
            "다릅니다 — outputs 는 다른 env 의 state 에서 읽혔습니다. "
            "이 캡처본의 infra/oidc/region 값을 실측과 대조하세요")
    admin_ui_url = outputs.get("admin_ui_url") or ""
    https = bool(outputs.get("https_enabled"))

    # 도메인 — admin_ui_url 의 호스트가 admin.<domain> 이면 도메인이 설정된
    # 것이다. https_enabled 로 게이트하면 "도메인은 선언됐지만 인증서 검증
    # 대기" 상태가 mode=none 으로 기록돼 채택 apply 가 도메인을 내려버린다.
    m = re.match(r"https://admin\.([^/]+)", admin_ui_url)
    if m:
        domain = {"mode": "route53-acm", "name": m.group(1)}
        if not https:
            notes.append("domain 은 선언됐지만 https_enabled=false — 인증서/"
                         "zone 미구성(검증 대기 포함)일 수 있습니다")
        zid = _r53_zone_id(domain["name"], region)
        if zid:
            domain["zone_id"] = zid
            notes.append(f"domain.zone_id={zid} 를 Route53 에서 매칭했습니다")
        else:
            notes.append("zone_id 매칭 실패 — Route53 콘솔에서 확인해 채우세요")
    else:
        domain = {"mode": "none"}

    # task def 에서 env 계약 역추출
    name_map = outputs.get("service_names") or {}
    if not isinstance(name_map, dict):
        notes.append("service_names output 이 맵이 아닙니다 — 서비스 이름 "
                     "역매핑을 건너뜁니다")
        name_map = {}
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
        sizing_env = _taskdef_env(region, api_arn, "admin-api")
        sender_type = sizing_env.get("EMAIL_SENDER_TYPE", "mock")
    except (CaptureError, KeyError, IndexError) as e:
        api_env_ok = False
        notes.append(f"admin-api task def 조회 실패 — env 기반 항목은 추정입니다: {e}")
        # task def env 에서 오는 기능키 전부 "못 읽음"이다 — false 로 두면
        # 채택 apply 가 켜진 기능을 끈다. 특히 body_logging=false 는
        # Firehose/S3 managed 리소스 destroy 라 반드시 unknown 표기.
        notes.append("service_keys/bi_insight/pricing_lambda/web_search/"
                     "body_logging/dev_login 은 읽기 실패로 false 기록 — 실제 값은 "
                     "ECS 콘솔/task def 에서 확인하세요")
        if unknown is not None:
            unknown.update({"features.service_keys", "features.bi_insight",
                            "features.pricing_lambda", "features.web_search",
                            "features.body_logging", "features.dev_login"})

    # 이미지 태그 + 레지스트리 — 서비스별 태그가 다르면(단일 서비스 hotfix 등)
    # 스키마가 표현 못 하므로 gateway-proxy 를 대표로 쓰고 차이는 노트로 남긴다.
    # 마지막 순회 서비스 태그를 쓰면 임의 서비스 태그가 전체에 고정된다.
    image_registry, tag_by_svc = "", {}
    try:
        _resp = _aws_json(["ecs", "describe-services", "--cluster", cluster,
                           "--services", *(service_names or ["gateway-proxy"])], region)
        gws = _resp["services"]
        # services 배열에 없는 항목은 failures(reason=MISSING)로 온다 — 조용히
        # 비는 sizing/태그 대신 어느 서비스가 부재인지 기록한다.
        _missing = [f.get("arn", "?").rsplit("/", 1)[-1]
                    for f in (_resp.get("failures") or [])]
        if _missing:
            notes.append(f"⚠ ECS 서비스 부재(MISSING): {', '.join(_missing)} — "
                         "해당 서비스의 sizing/태그는 캡처되지 않습니다")
        live_to_logical = {v: k for k, v in name_map.items()}
        for s in gws:
            td_arn = s.get("taskDefinition", "")
            td = _aws_json(["ecs", "describe-task-definition",
                            "--task-definition", td_arn], region)["taskDefinition"]
            logical = live_to_logical.get(s["serviceName"], s["serviceName"])
            defs = td["containerDefinitions"]
            # 사이드카가 첫 번째면 image/env 를 잘못 읽는다 — 논리명 컨테이너 우선
            cdef = next((c for c in defs
                         if c.get("name") == logical), defs[0])
            img = cdef.get("image", "")
            if ":" in img:
                tag_by_svc[logical] = img.rsplit(":", 1)[1]
                if "/" in img and not image_registry:
                    image_registry = img.rsplit("/", 1)[0]
            # deploy.sizing 키는 논리 서비스명 — 라이브명이 커스텀이면 역매핑
            sizing[logical] = {
                "cpu": _safe_int(td.get("cpu")),
                "memory": _safe_int(td.get("memory")),
                "desired_count": s.get("desiredCount", 0),
            }
        image_tag = tag_by_svc.get("gateway-proxy", "")
        if not image_tag and tag_by_svc:
            image_tag = next(iter(tag_by_svc.values()))
        diverged = {k: t for k, t in tag_by_svc.items() if t != image_tag}
        if diverged:
            notes.append(f"⚠ 서비스별 이미지 태그가 다릅니다: {tag_by_svc} — "
                         "ecs 스키마는 단일 images.tag 라 gateway-proxy 기준으로 "
                         "기록했습니다. 채택 apply 시 나머지 태그가 통일됩니다")
        # 라이브 오토스케일 상한 — 캡처가 버리면 render 가 desired×factor 로
        # 재계산해 수동으로 올려둔 max_count 가 내려간다.
        if sizing and cluster:
            try:
                targets = _aws_json(
                    ["application-autoscaling", "describe-scalable-targets",
                     "--service-namespace", "ecs"], region)
                for t in (targets.get("ScalableTargets") or []):
                    rid = str(t.get("ResourceId") or "")
                    # service/<cluster>/<service>
                    parts = rid.split("/")
                    if len(parts) >= 3 and parts[-2] == cluster:
                        logical = live_to_logical.get(parts[-1], parts[-1])
                        if logical in sizing:
                            for src, dst in (("MinCapacity", "min_count"),
                                             ("MaxCapacity", "max_count")):
                                if isinstance(t.get(src), int):
                                    sizing[logical][dst] = t[src]
            except CaptureError:
                pass
    except (CaptureError, KeyError, IndexError) as e:
        notes.append(f"서비스 상태 조회 실패 — sizing 은 비워둡니다: {e}")

    notif = {"provider": sender_type if sender_type in
             ("mock", "ses", "smtp", "internal_api") else "mock"}
    if sender_type not in ("mock", "ses", "smtp", "internal_api"):
        notes.append(f"EMAIL_SENDER_TYPE={sender_type} 는 스키마에 없는 값 — "
                     "provider 를 mock 으로 기록했으니 채택 전 확인하세요")
    if sender_type == "ses":
        notif["ses_from"] = sizing_env.get("EMAIL_SENDER_ADDRESS", "")
    elif sender_type == "smtp":
        notif.update({"smtp_host": sizing_env.get("SMTP_HOST", ""),
                      "smtp_port": _safe_int(sizing_env.get("SMTP_PORT"), 587),
                      "smtp_from": sizing_env.get("EMAIL_SENDER_ADDRESS", "")})
    elif sender_type == "internal_api":
        notif["internal_api_url"] = sizing_env.get("EMAIL_API_URL", "")
        notif["internal_api_from"] = sizing_env.get("EMAIL_SENDER_ADDRESS", "")
        if not notif["internal_api_url"]:
            notes.append("EMAIL_SENDER_TYPE=internal_api 인데 EMAIL_API_URL 이 "
                         "없습니다 — notifications.internal_api_url 을 채우세요")

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
                # EngineVersion 핀 생략은 parameter-group family 드리프트를
                # 만든다 (render 가 engine_version 키로만 방출) — 기존 env 의
                # 실측 버전을 기록해 다음 plan 이 family 를 바꾸지 않게 한다.
                eng_ver = str(c.get("EngineVersion") or "")
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
                if eng_ver:
                    infra_doc["db"]["engine_version"] = eng_ver
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

    # ALB SG 인바운드 → allowed_cidrs 역산 + Scheme → network.mode:
    # Route53 alias → ALB → SG 규칙. internal ALB 를 public 으로 기록하면
    # 채택 apply(alb_internal=false 렌더)가 인터넷 노출로 되돌린다.
    # domain 미설정 배포는 alias 경로가 없으므로 alb_dns_name output 으로도
    # 탐침한다 — 어느 쪽도 못 읽으면 public 기록이 아니라 unknown 처리.
    allowed_cidrs: list[str] = []
    net_mode = "public"
    alb_dns = ""
    if domain.get("zone_id") and domain.get("name"):
        alb_dns = _r53_alias_target(domain["zone_id"],
                                    f"admin.{domain['name']}", region) or ""
    if not alb_dns:
        alb_dns = str(outputs.get("alb_dns_name") or "")
    if alb_dns:
        allowed_cidrs, alb_scheme = _alb_ingress_cidrs(alb_dns, region)
        if alb_scheme == "internal":
            net_mode = "private"
            notes.append("ALB Scheme=internal — network.mode=private 로 기록합니다")
        if allowed_cidrs:
            notes.append(f"allowed_cidrs={allowed_cidrs} 를 ALB SG 에서 역산했습니다")
    else:
        notes.append("ALB 주소를 Route53/output 어느 쪽으로도 못 읽었습니다 — "
                     "network.mode=public 기록은 미검증입니다. internal ALB "
                     "배포라면 private 으로 바로잡으세요")
        if unknown is not None:
            unknown.add("network.mode")
    if not allowed_cidrs:
        notes.append("allowed_cidrs 를 ALB SG 에서 못 읽었습니다 — 키를 생략합니다 "
                     "(빈 리스트로 쓰면 apply 가 ALB 인그레스 규칙을 전부 지웁니다). "
                     "0.0.0.0/0 외 제한이 있거나 IPv6/PrefixList 규칙이면 수동 입력")

    tfstate: dict = {}
    bc = _tf_backend(env_dir)
    if bc.get("bucket"):
        tfstate["tfstate_bucket"] = bc["bucket"]
    if bc.get("dynamodb_table"):
        tfstate["tfstate_table"] = bc["dynamodb_table"]
    if not tfstate:
        notes.append("tfstate_bucket/table — .terraform/terraform.tfstate 가 없어 "
                     "못 읽었습니다 (terraform init 후 재캡처하거나 수동 입력)")

    tv = _tfvars(env_dir)
    if tv.get("bedrock_allowed_model_arns"):
        infra_doc["bedrock_allowed_model_arns"] = tv["bedrock_allowed_model_arns"]
    if isinstance(tv.get("cognito_groups"), list) and tv["cognito_groups"]:
        infra_doc["cognito_groups"] = [str(g) for g in tv["cognito_groups"]]
    models_profile = str(tv.get("models_profile") or "global")
    if not tv.get("models_profile"):
        notes.append("clients.models_profile 을 tfvars 에서 못 읽어 'global' 로 "
                     "둡니다 — regional/다른 프로필이면 채택 전 수정하세요")

    # render 가 무조건 다시 쓰는 보안/권한 키 — 캡처가 버리면 채택 apply 가
    # fail-closed/lockout 을 일으킨다 (allowed_iam_roles=[] → CLI VK 전면 403,
    # admin_groups=기본값 → 커스텀 관리자 그룹 권한 상실).
    allowed_roles = ([str(r) for r in tv["allowed_iam_roles"]]
                     if isinstance(tv.get("allowed_iam_roles"), list) else [])
    if allowed_roles:
        notes.append(f"aws.allowed_roles={allowed_roles} 를 tfvars 에서 복원했습니다")
    elif "allowed_iam_roles" in tv:
        notes.append("tfvars 의 allowed_iam_roles 가 list 가 아닙니다 — "
                     "aws.allowed_roles 를 수동 확인하세요")
    oidc_tfvars = {}
    if isinstance(tv.get("admin_groups"), list) and tv["admin_groups"]:
        oidc_tfvars["admin_groups"] = [str(g) for g in tv["admin_groups"]]
    for tfk, dk in (("oidc_group_prefix", "group_prefix"),
                    ("oidc_groups_claim", "groups_claim"),
                    ("oidc_reject_unmatched_groups", "reject_unmatched_groups")):
        if tv.get(tfk) not in (None, ""):
            oidc_tfvars[dk] = tv[tfk]

    # tfvars 유래 oidc 키 병합 — task-def env 유래 키(issuer/client_id)를 덮지 않는다
    for k, v2 in oidc_tfvars.items():
        oidc.setdefault(k, v2)

    doc = {
        "version": 1,
        "env": env_name,
        "aws": {"region": region,
                **({"allowed_roles": allowed_roles} if allowed_roles else {})},
        # 라이브 desired/cpu/memory 를 그대로 기록 — 채택 후 apply 가 task 수를
        # 프리셋으로 되돌리지 않고, DB 커넥션 예산 검증도 실제 수요로 계산된다
        "deploy": {"target": "ecs", "size_tier": tier, "sizing": sizing,
                   **tfstate},
        # allowed_cidrs 는 읽은 값이 있을 때만 쓴다 — 빈 리스트를 기록하면 채택
        # 후 apply 가 모듈 기본(0.0.0.0/0) 대신 ALB 인그레스 0개를 만든다
        "network": {"mode": net_mode,
                    **({"allowed_cidrs": allowed_cidrs} if allowed_cidrs else {})},
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
            # task def 읽기 실패(sizing_env={}) 또는 env 부재 — 앱 기본은
            # false(fail-closed)라 "못 읽으면 꺼짐"으로 기록한다. 보안 플래그를
            # true 로 추정해 채택 시 발급이 열리는 fail-open 방지.
            "service_keys": (api_env_ok
                             and _is_true_env(
                                 sizing_env.get("SERVICE_KEYS_ENABLED"))),
            # 생성 여부의 진리는 terraform output — ECS 는 tfvars 전량 재생성이라
            # 캡처 누락 시 다음 apply 가 module.alerting 을 통째로 destroy 한다.
            # emails 는 선언 tfvars(사람 소유)에 있으면 함께 보존한다.
            "alerting": {
                "enabled": bool(outputs.get("alerting_sns_topic_arn")),
                # 스칼라 할당도 유효한 HCL — str 을 list 로 감싸지 않으면
                # 문자를 쪼갠 쓰레기 emails 가 캡처된다
                **({"emails": [str(e) for e in (
                    tv["alert_emails"] if isinstance(
                        tv["alert_emails"], list)
                    else [tv["alert_emails"]])]}
                   if tv.get("alert_emails") else {}),
            },
        },
        "images": {"tag": image_tag,
                   # 이미지 URI 의 레지스트리 — 계정/리전 경계 신호. 비우면
                   # 채택본이 모듈 생성 ECR 로 리포지토리를 되돌린다
                   **({"registry": image_registry} if image_registry else {})},
        "clients": {"models_profile": models_profile},
    }
    if api_env_ok and sizing_env.get("SERVICE_KEYS_ENABLED") is None:
        if unknown is not None:
            unknown.add("features.service_keys")
        notes.append(
            "SERVICE_KEYS_ENABLED 를 못 읽었습니다 — false 기록. 앱 기본은 "
            "false(구 task def/이미지에서 env 없이 켜져 있던 배포라면 true 로 "
            "바로잡고 env 를 명시하세요)")
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


def _dmap(node) -> dict:
    """leaf 가 dict 가 아니면(str/list/int) {} — `x or {}` 는 truthy 스칼라를
    그대로 통과시켜 이후 .get 이 AttributeError 로 죽는 함정을 막는다."""
    return node if isinstance(node, dict) else {}


def _instance_items(items: list, release: str) -> list:
    """공유 ns 의 다른 릴리스가 이름 접미(-gateway 등)로 우리 것처럼 보인다 —
    doctor 와 같은 계약으로 app.kubernetes.io/instance == release 만 고르고,
    라벨 없는 레거시 채택은 타 릴리스 소유가 명시되지 않은 것만 폴백한다."""
    items = [i for i in items if isinstance(i, dict)]
    def _inst(i) -> str:
        return str((_dmap(_dmap(i.get("metadata")).get("labels")))
                   .get("app.kubernetes.io/instance") or "")
    ours = [i for i in items if _inst(i) == release]
    return ours or [i for i in items if not _inst(i)]


def _kube_context_cluster(context: str = "") -> str:
    """현재 kube context 가 가리키는 클러스터명.

    EKS kubeconfig 의 context.cluster 는 보통 arn:aws:eks:…:cluster/NAME 이라
    마지막 슬래시 뒤만 취한다 (평문 이름이면 그대로). 못 읽으면 ""."""
    argv = ["kubectl", "config", "view", "--minify",
            "-o", "jsonpath={.contexts[0].context.cluster}"]
    if context:
        argv += ["--context", context]
    try:
        r = _sp(argv, tool="kubectl", timeout=15)
    except CaptureError:
        return ""
    if r.returncode != 0:
        return ""
    return r.stdout.strip().rsplit("/", 1)[-1]


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


def _relpath(p: Path) -> str:
    """repo 루트 기준 상대경로 — 밖이면 원래 값 유지."""
    root = Path(__file__).resolve().parents[3]
    try:
        return str(p.resolve().relative_to(root))
    except ValueError:
        return str(p)


def _gen_root() -> Path:
    """render 가 managed overlay 를 쓰는 루트 — cli.GEN_ROOT 와 동일 경로."""
    return Path(__file__).resolve().parents[3] / "deployment" / "gen"


def _auto_env_dir(env: str) -> Path | None:
    """표준 명명(llm-gateway-<env> / <env>)으로 terraform env 디렉토리를 탐지."""
    envs = Path(__file__).resolve().parents[3] / "deployment" / \
        "terraform" / "environments"
    for cand in (f"llm-gateway-{env}", env):
        if (envs / cand).is_dir():
            return envs / cand
    return None


def _tf_backend(env_dir: Path) -> dict:
    """init 된 env 의 backend.config — .terraform/terraform.tfstate 로컬 복사본.
    backend.tf 가 partial config 라 bucket/table 은 init 시 주입값이 여기만 남는다."""
    try:
        state = json.loads(
            (env_dir / ".terraform" / "terraform.tfstate").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return dict((state.get("backend") or {}).get("config") or {})


def _tfvars(env_dir: Path, extra_files: list[Path] | None = None) -> dict[str, object]:
    """env tfvars 의 스칼라/문자열 리스트 — 캡처 스키마 키용 부분집합 파서.
    terraform.tfvars + *.auto.tfvars + extra_files(gen managed overlay 등,
    뒤가 이김)를 terraform 자동 로드 순서로 병합한다 — NDS 가 managed 키를
    gen overlay 에 쓰고 사람이 auto.tfvars 에 선언할 수 있어 한 파일만
    보면 유실된다.
    map/object 값은 _tfvars_clean 이 본문째 제거해 스킵된다
    (우리가 읽는 키는 전부 str|list[str]|number|bool)."""
    texts: list[str] = []
    for f in ([env_dir / "terraform.tfvars",
               *sorted(env_dir.glob("*.auto.tfvars")),
               *(extra_files or [])]):
        try:
            texts.append(f.read_text())
        except OSError:
            continue
    if not texts:
        return {}
    clean = _tfvars_clean("\n".join(texts))
    out: dict[str, object] = {}
    for m in re.finditer(
            # \s 는 개행도 매칭해 "key =\nother_key" 가 other_key 를 삼킨다 —
            # = 주변은 같은 줄 공백만 (s 플래그는 멀티라인 리스트용)
            r'(?ms)^[ \t]*([A-Za-z_]\w*)[ \t]*=[ \t]*(\[.*?\]|"(?:[^"\\]|\\.)*"|[^\s#{]+)',
            clean):
        key, raw = m.group(1), m.group(2).strip()
        if raw.startswith("["):
            raw = re.sub(r',\s*([\]])', r'\1', raw)          # trailing comma
            try:
                v = json.loads(raw)
                if isinstance(v, list):
                    out[key] = v
            except json.JSONDecodeError:
                pass
        elif raw.startswith('"'):
            try:
                out[key] = json.loads(raw)
            except json.JSONDecodeError:
                out[key] = raw.strip('"')
        elif re.fullmatch(r'-?\d+(\.\d+)?', raw):
            out[key] = float(raw) if '.' in raw else int(raw)
        elif raw in ("true", "false"):
            out[key] = raw == "true"
    return out


def _r53_zone_id(domain: str, region: str) -> str:
    """domain.name 의 가장 긴 접미사 일치 public hosted zone id.

    list-hosted-zones-by-name --dns-name X 는 정렬 순서상 X 위치부터 반환한다 —
    같은 이름보다 짧은 부모 zone(nds.remu.one, remu.one)은 결과에 없다.
    라벨을 하나씩 떼며 각 접미사로 조회해, 처음 매칭된(=가장 긴) zone 을 쓴다."""
    labels = domain.split(".")
    suffixes = [".".join(labels[k:]) for k in range(len(labels) - 1)]
    for suffix in suffixes:
        try:
            r = _aws_json(["route53", "list-hosted-zones-by-name",
                           "--dns-name", suffix], region)
        except CaptureError:
            return ""
        for z in r.get("HostedZones", []):
            zname = (z.get("Name") or "").rstrip(".")
            if (z.get("Config") or {}).get("PrivateZone"):
                continue
            # 이 접미사를 이름으로 쓰는 zone 자체가 있을 때만 — 반환은
            # suffix "이후" 위치라 다음 알파벳 zone 이 섞일 수 있다
            if zname == suffix:
                return (z.get("Id") or "").rsplit("/", 1)[-1]
    return ""


def _r53_alias_target(zone_id: str, host: str, region: str) -> str:
    """hosted zone 에서 host 의 A/AAAA alias 대상 DNS (예 dualstack.<alb>.elb...)."""
    try:
        r = _aws_json(
            ["route53", "list-resource-record-sets",
             "--hosted-zone-id", zone_id,
             "--query", f"ResourceRecordSets[?Name=='{host}.']"],
            region)
    except CaptureError:
        return ""
    for rec in r if isinstance(r, list) else []:
        tgt = ((rec.get("AliasTarget") or {}).get("DNSName") or "")
        if tgt:
            # lstrip("dualstack.") 은 문자셋 스트립이라 "dualstack.llm-…" 가
            # "m-…" 로 망가진다 — 접두어만 제거한다
            return tgt.removeprefix("dualstack.").rstrip(".")
    return ""


def _alb_ingress_cidrs(alb_dns: str, region: str) -> tuple[list[str], str]:
    """ALB DNS 이름으로 LB 를 찾아 (SG 인바운드 CIDR, scheme) 을 모은다.

    모듈이 여는 포트는 모드마다 다르다 — https_ready=[80,443], http_mode=
    [8000,8080,3000], http=[80] (ecs-gateway/main.tf alb_ingress_ports). 세
    모드 포트를 전부 보며, IpProtocol -1(all traffic) 규칙의 IpRanges 도
    인입 허용으로 간주한다. scheme 은 "internet-facing"|"internal"|"" —
    network.mode 역산에 쓴다 (internal ALB 를 public 으로 기록하면 채택
    apply 가 인터넷 노출 ALB 로 되돌린다)."""
    try:
        lbs = _aws_json(["elbv2", "describe-load-balancers"], region)
        sg_ids: list[str] = []
        scheme = ""
        for lb in lbs.get("LoadBalancers", []):
            if (lb.get("DNSName") or "") == alb_dns:
                sg_ids = lb.get("SecurityGroups") or []
                scheme = str(lb.get("Scheme") or "")
                break
        if not sg_ids:
            return [], scheme
        sgs = _aws_json(["ec2", "describe-security-groups",
                         "--group-ids", *sg_ids], region)
    except CaptureError:
        return [], ""
    cidrs: list[str] = []
    for sg in sgs.get("SecurityGroups", []):
        for p in sg.get("IpPermissions", []):
            if (p.get("IpProtocol") != "-1"
                    and p.get("FromPort") not in (80, 443, 8000, 8080, 3000)):
                continue
            for rng in p.get("IpRanges", []):
                c = rng.get("CidrIp")
                if c and c not in cidrs:
                    cidrs.append(c)
    # 미커버 규칙: Ipv6Ranges/PrefixListIds 는 읽지 않는다 — 호출부가 cidrs
    # 비어 있을 때 수동 확인 notes 를 낸다.
    return cidrs, scheme


def capture_eks(namespace: str = "llm-gateway", release: str = "llm-gateway",
                context: str = "", env_dir: Path | None = None,
                unknown: set[str] | None = None) -> tuple[dict, list[str]]:
    notes: list[str] = []
    v = _helm_values(release, namespace, context)

    env = str(_val(v, "global", "environment") or release)
    region = str(_val(v, "aws", "region"))
    if env_dir is None:
        found = _auto_env_dir(env)
        if found:
            env_dir = found
            notes.append(f"tf_env_dir 을 자동 탐지했습니다: {found.name}")
    genv = _dmap(_val(v, "gatewayProxy", "env", default={}))

    # ── 라이브 대조용 수집 ─────────────────────────────────────────────────
    live = _kubectl_json("ingress", namespace, context)
    deploys = _kubectl_json("deployments", namespace, context)

    # ── ingress → domain / allowed_cidrs ───────────────────────────────────
    ing = _dmap(v.get("ingress"))
    ann = dict(_dmap(ing.get("annotations")))
    cidrs_raw = ann.get("alb.ingress.kubernetes.io/inbound-cidrs", "")
    cidrs = [c.strip() for c in str(cidrs_raw).split(",") if c.strip()]

    gw_host = str(_val(ing, "gateway", "host"))
    cert_arn = ann.get("alb.ingress.kubernetes.io/certificate-arn", "")
    if cert_arn or str(_val(ing, "gateway", "tls", "enabled")) == "True":
        domain = {"mode": "route53-acm", "name": _domain_base(gw_host, env)}
        _capture_hosts(domain, ing, notes)
    elif gw_host:
        domain = {"mode": "route53-acm", "name": _domain_base(gw_host, env)}
        _capture_hosts(domain, ing, notes)
        notes.append("ingress 에 인증서가 없어 HTTP 일 수 있습니다 — domain.mode 확인 필요")
    else:
        domain = {"mode": "none"}
        notes.append("ingress.host 비어 있음 — ALB DNS 직접 접근 모드로 추정")
    if gw_host and not domain.get("name"):
        # gateway.<base>/gateway-<env>.<base> 규약에 안 맞는 호스트명(api.corp.io
        # 등)은 베이스 추출이 안 돼 name 이 비는데, route53-acm 은 name 필수라
        # 캡처 yaml 이 스키마 검증을 못 넘긴다 — none 으로 내리고 실측 호스트는
        # notes 로 남긴다 (hosts 는 name 없으면 render 가 무시하는 dead config).
        domain["mode"] = "none"
        _live = {k: str(_val(ing, ck, "host"))
                 for k, ck in _HOST_KEY_MAP.items()}
        notes.append(f"gateway host({gw_host})에서 베이스 도메인을 추출하지 "
                     "못했습니다 — domain.mode=none 으로 기록합니다. 정식 "
                     f"도메인이면 name/zone_id + hosts({_live})를 채우세요")

    # 라이브 ingress 와 values 의 host 가 다른지 (kubectl 패치 드리프트).
    # 공유 ns 의 다른 릴리스는 instance 라벨로 걸러낸다 — 타 릴리스의
    # <other>-gateway ingress 를 우리 것으로 읽으면 채택 yaml 이 scheme/cidr
    # 까지 외부 배포 것으로 뒤집힌다.
    live_hosts = []
    live_schemes: dict[str, str] = {}
    live_cidr_map: dict[str, str] = {}
    for item in _instance_items(live.get("items", []), release):
        anns = _dmap(_dmap(item.get("metadata")).get("annotations"))
        iname = str(_dmap(item.get("metadata")).get("name") or "")
        rules = _dmap(item.get("spec")).get("rules") or []
        for rule in rules:
            if isinstance(rule, dict):
                live_hosts.append((iname, rule.get("host", "")))
        scheme = anns.get("alb.ingress.kubernetes.io/scheme")
        if scheme:
            live_schemes[iname] = scheme
        live_cidrs = anns.get("alb.ingress.kubernetes.io/inbound-cidrs")
        if live_cidrs:
            live_cidr_map[iname] = live_cidrs
            if live_cidrs != cidrs_raw:
                notes.append(f"⚠ ingress/{iname} 의 live inbound-cidrs "
                             f"({live_cidrs})가 helm values 와 다릅니다 — "
                             "kubectl 패치 드리프트. 라이브 값으로 캡처합니다")
    # 시행 중인 값은 helm values 가 아니라 라이브 어노테이션이다 — live 가 더
    # 좁은 제한을 패치해 둔 경우 values 값 채택은 ALB 를 다시 연다.
    if live_cidr_map:
        live_set = {c for v2 in live_cidr_map.values()
                    for c in str(v2).split(",") if c.strip()}
        if live_set and live_set != set(cidrs):
            cidrs = sorted(live_set)

    def _scheme(suffix: str, values_sec: str) -> str:
        for nm, sc in live_schemes.items():
            if nm.endswith(f"-{suffix}"):
                return sc
        sec_anns = _dmap(_val(ing, values_sec, "annotations", default={}))
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
    for item in _instance_items(deploys.get("items", []), release):
        name = str(_dmap(item.get("metadata")).get("name") or "")
        svc_key = next((s for s in helm_keys if name.endswith(f"-{s}")), "")
        if not svc_key:
            continue
        containers = _dmap(_dmap(item.get("spec")).get("template"))
        containers = _dmap(containers.get("spec")).get("containers") or []
        # 앱 컨테이너는 차트가 svc 이름으로 명명한다 — 컨테이너를 무차별 순회하면
        # otel-collector 같은 사이드카 이미지 태그가 앱 태그를 덮어쓴다.
        pick = next((c for c in containers
                     if isinstance(c, dict) and c.get("name") == svc_key), None)
        if pick is None and containers:
            pick = containers[0]
        img = str((pick or {}).get("image") or "") if isinstance(pick, dict) else ""
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
        # 캡처는 현재 상태 기준 — 라이브 > values 명시 > 차트 폴백 순.
        # migration 은 hook Job(deployment 에 없음) + 독립 버전 라인(headNNNN)
        # 이라 appVersion 폴백을 하면 존재하지 않는 migration 태그가 고정돼
        # 채택 apply 의 migration Job 이 ImagePull 실패한다.
        tags[svc] = live_tag or declared or (
            "" if svc == "migration" else app_version)
    unique = {t for t in tags.values() if t}
    tag = tags.get("gateway-proxy", "")
    per_service = ({helm_keys[s]: t for s, t in tags.items() if t}
                   if len(unique) > 1 else {})

    # ── features ────────────────────────────────────────────────────────────
    email = _dmap(_val(v, "notificationWorker", "email", default={}))
    provider = str(email.get("provider", "mock"))
    notif = {"provider": provider}
    if provider == "ses":
        notif["ses_from"] = str(_val(email, "ses", "fromAddress"))
    elif provider == "smtp":
        smtp = _dmap(_val(email, "smtp", default={}))
        notif.update({
            "smtp_host": str(smtp.get("host", "")),
            "smtp_port": smtp.get("port", 587),
            "smtp_from": str(smtp.get("fromAddress", "")),
            "smtp_credentials_secret": str(smtp.get("credentialsSecretName", "")),
        })
    elif provider == "internal_api":
        ia = _dmap(_val(email, "internalApi", default={}))
        notif.update({
            "internal_api_url": str(ia.get("url", "")),
            "internal_api_from": str(ia.get("fromAddress", "")),
        })
    elif provider != "mock":
        notes.append(f"email.provider={provider} 는 스키마에 없는 값 — "
                     "notifications.provider 를 직접 맞추세요")

    otel_mode = str(_val(v, "observability", "otel", "mode"))
    aenv = _dmap(_val(v, "adminApi", "env", default={}))
    # 스키마 키를 전부 기록한다 — 못 읽은 기능은 false 로 명시. 키가 빠지면
    # 캡처된 yaml 을 보는 사용자가 그 옵션의 존재 자체를 모른다.
    features = {
        "notifications": notif,
        "bi_insight": bool(aenv.get("AGENTCORE_RUNTIME_ARN")),
        "pricing_lambda": bool(aenv.get("LITELLM_PRICING_LAMBDA")),
        "web_search": _is_true_env(genv.get("WEB_SEARCH_ENABLED")),
        "observability": otel_mode not in ("", "disabled", "none"),
        "dev_login": False,
        # env 부재는 앱 기본 false(fail-closed) — 명시 truthy 일 때만 켠다.
        # values 에 손으로 false(비인용 bool)를 박아도 yaml 파서가 False 로
        # 넘기니 _is_true_env 의 문자열 정규화로 안전하다.
        "service_keys": _is_true_env(aenv.get("SERVICE_KEYS_ENABLED")),
    }
    # env 부재 — 차트 기본이 --all 에 합류하므로 부재는 구 차트/구 이미지 배포.
    # false 를 기록하되 실측이 아님을 unknown 에 표시해 absorb 오덮어쓰기 방지.
    if aenv.get("SERVICE_KEYS_ENABLED") is None and unknown is not None:
        unknown.add("features.service_keys")
    # body_logging 은 eks managed overlay 가 "켜기"만 소유하므로 false 를
    # 박지 않는다 — 박으면 validate 마다 수동 비활성화 경고가 난다.
    if genv.get("FIREHOSE_STREAM_NAME"):
        features["body_logging"] = True
    else:
        notes.append("body_logging 미감지(FIREHOSE_STREAM_NAME 없음) — "
                     "키를 생략했습니다. 켜려면 features.body_logging: true")

    # alerting — log_shipping 은 helm values(관측 네임스페이스 라우터)에서,
    # enabled/emails 는 terraform output·env tfvars 에서 아래 블록이 채운다.
    # 콘솔 수동 구독은 terraform state 밖이라 캡처 대상이 아니다.
    features["alerting"] = {
        "log_shipping": _is_true_env(
            _val(v, "observability", "fargateLogging", "enabled")),
    }

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
        ui_env = _dmap(_val(v, "adminUi", "env", default={}))
        oidc = {
            "issuer_url": issuer,
            "client_id": str(ui_env.get("OIDC_CLIENT_ID", "")),
            "authorize_url": str(ui_env.get("OIDC_AUTHORIZE_URL", "")),
            "token_url": str(ui_env.get("OIDC_TOKEN_URL", "")),
            "provider_name": str(_val(v, "adminApi", "oidc", "providerName", default="oidc:cognito")),
            "required_group": str(_val(v, "adminApi", "oidc", "requiredGroup") or
                                  _val(v, "gatewayProxy", "oidc", "requiredGroup")),
        }
        # render 가 무조건 다시 쓰는 키(audience=client_id, groupsClaim=파생)를
        # 못 읽으면 채택 apply 가 라이브 JWT 검증 규칙을 바꾼다 — 라이브 값 기록.
        for vk_, dk in (("audience", "audience"),
                        ("groupsClaim", "groups_claim"),
                        ("groupPrefix", "group_prefix"),
                        ("rejectUnmatchedGroups", "reject_unmatched_groups")):
            lv = _val(v, "adminApi", "oidc", vk_)
            if lv not in (None, ""):
                oidc[dk] = lv
        ab = _dmap(_val(v, "adminApi", "adminBootstrap", default={}))
        if isinstance(ab.get("groups"), list) and ab["groups"]:
            oidc["admin_groups"] = [str(g) for g in ab["groups"]]
        if isinstance(ab.get("emails"), list) and ab["emails"]:
            oidc["admin_emails"] = [str(x) for x in ab["emails"]]
        vk_ttl = _val(v, "adminApi", "oidc", "vkTtlHours")
        if vk_ttl not in (None, ""):
            oidc["vk_ttl_hours"] = vk_ttl
        if not ui_env.get("OIDC_CLIENT_ID"):
            notes.append("adminUi.env.OIDC_CLIENT_ID 를 못 읽었습니다 — 확인해 채우세요")
    else:
        notes.append("OIDC issuer 없음 — admin-ui 는 dev-login 모드일 수 있습니다")

    # ── terraform outputs (env_dir 있으면) — region/cognito 보강 ─────────────
    outs: dict = {}
    if env_dir and env_dir.exists():
        try:
            outs = _tf_outputs(env_dir)
            # env_dir 이 정말 이 클러스터의 env 인지 — 같은 env 이름(dev)으로
            # 다른 계정/리전의 디렉토리를 고르면 tfstate·tfvars·outputs 전부
            # 다른 환경 값이 섞인다. 둘 다 읽혔는데 다르면 채택을 거부한다.
            live_cluster = _kube_context_cluster(context)
            tf_cluster = str(outs.get("cluster_name") or "")
            if live_cluster and tf_cluster and live_cluster != tf_cluster:
                notes.append(f"⚠ tf_env_dir 의 cluster_name={tf_cluster} 가 kube "
                             f"context 클러스터({live_cluster})와 다릅니다 — 다른 "
                             "환경의 env 디렉토리라 terraform 유래 값을 채택하지 "
                             "않습니다")
                env_dir, outs = None, {}
            elif live_cluster and tf_cluster:
                notes.append(f"tf_env_dir={env_dir.name} 이 라이브 클러스터"
                             f"({live_cluster})와 일치합니다")
            else:
                notes.append("kube context 클러스터명을 못 읽어 tf_env_dir 검증은 "
                             "건너뛰었습니다 — 채택 전 terraform 값을 대조하세요")
            if not region:
                region = str(outs.get("aws_region", "") or outs.get("region", ""))
            if outs.get("alerting_sns_topic_arn"):
                features["alerting"]["enabled"] = True
                # emails 는 env tfvars(사람 선언)나 gen managed overlay(NDS
                # 소유)에서 복원 — 안 잡으면 채택 yaml 의 emails=[] 가 다음
                # apply 에 구독 전량 삭제. 콘솔 수동 구독은 state 밖이라 미대상.
                _gen_tfvars = _gen_root() / env / "eks" / "terraform.tfvars"
                _tv = _tfvars(env_dir, extra_files=[_gen_tfvars])
                if _tv.get("alert_emails"):
                    _ae = _tv["alert_emails"]
                    features["alerting"]["emails"] = [
                        str(e) for e in (_ae if isinstance(_ae, list)
                                         else [_ae])]
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
            # output 을 못 읽으면 이 env_dir 이 정말 이 클러스터의 것인지 검증이
            # 안 된다 — 아래 블록이 같은 디렉토리의 backend/tfvars 를 읽어
            # 다른 env 의 tfstate·infra 값을 캡처본에 싣는 것을 차단한다.
            notes.append(f"{env_dir} 의 terraform output 을 못 읽었습니다 — "
                         "env_dir 검증이 불가하므로 tfstate_bucket/infra 등 "
                         "terraform 유래 값은 기록하지 않습니다. env_dir 이 "
                         "맞는지(init 여부) 확인 후 재캡처하거나 수동 입력하세요")
            env_dir = None
    if "enabled" not in features["alerting"]:
        # eks 는 overlay 가 "켜기"만 소유하므로 미확인 시 키를 비워둔다 —
        # enabled 미기록은 다음 apply 가 alerting 을 켜지도 끄지도 않는다.
        notes.append("alerting.enabled 는 terraform output(alerting_sns_topic_arn)"
                     "으로만 확인 가능 — 읽지 못해 생략했습니다. "
                     "활성이었다면 features.alerting.enabled: true 를 직접 기록하세요")

    # ── 역산 가능 항목 복원 — zone_id / tfstate / infra / eks 고유 자원 ──────
    tfstate: dict = {}
    infra: dict = {}
    if env_dir and env_dir.exists():
        bc = _tf_backend(env_dir)
        if bc.get("bucket"):
            tfstate["tfstate_bucket"] = bc["bucket"]
        if bc.get("dynamodb_table"):
            tfstate["tfstate_table"] = bc["dynamodb_table"]
        tv = _tfvars(env_dir, extra_files=[
            _gen_root() / env / "eks" / "terraform.tfvars"])
        for tfkey, dockey in (("bedrock_allowed_model_arns",
                               "bedrock_allowed_model_arns"),
                              ("mantle_regions", "mantle_regions"),
                              ("cognito_domain_suffix", "cognito_domain_suffix")):
            if tv.get(tfkey):
                infra[dockey] = tv[tfkey]
        if infra:
            notes.append(f"infra.{', '.join(infra)} 를 tfvars 에서 복원했습니다")

    if domain.get("name") and domain.get("mode") == "route53-acm":
        zid = _r53_zone_id(domain["name"], region)
        if zid:
            domain["zone_id"] = zid
            notes.append(f"domain.zone_id={zid} 를 Route53 에서 매칭했습니다")
        else:
            notes.append("zone_id 매칭 실패 — hosted zone 을 Route53 에서 확인해 채우세요")

    # eks 고유 자원 — 스키마에 표현이 없어도 실체를 notes 로 기록해 수동 채택을 돕는다
    try:
        es_items = _kubectl_json("externalsecrets", namespace, context).get("items", [])
        if es_items:
            names = sorted(n for n in (
                str(_dmap(i.get("metadata")).get("name") or "")
                for i in es_items if isinstance(i, dict)) if n)
            notes.append(f"ESO ExternalSecret 감지: {', '.join(names)} — "
                         "시크릿 백엔드(SM) 값은 의도적으로 캡처하지 않습니다")
    except CaptureError:
        pass  # CRD 없음 = ESO 미사용
    cluster = str(outs.get("cluster_name") or "")
    if cluster:
        try:
            profs = _aws_json(["eks", "list-fargate-profiles",
                               "--cluster-name", cluster],
                              region).get("fargateProfileNames") or []
            if profs:
                notes.append(f"Fargate profile({cluster}): {', '.join(sorted(profs))} — "
                             "terraform env 가 소유, gateway.yaml 표현 없음")
        except CaptureError:
            pass
    if outs.get("rds_proxy_enabled"):
        notes.append("RDS Proxy 사용 중 — module.aurora 소유라 캡처본에 기록하지 않습니다 "
                     "(application_db_endpoint 는 proxy 경유)")

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

    # ── deploy 용량 — 라이브 HPA/pool 역산. 실측이 밴드와 정확히 일치하면
    #    capacity_tier(의도 선언)로, 아니면 명시 sizing 으로 기록한다 ────────
    sizing: dict = {}
    asc = _dmap(_val(v, "gatewayProxy", "autoscaling", default={}))
    gp: dict = {}
    # enabled=false + replicaCount 고정 운영(loadtest 등)도 helm values 에는
    # 차트 기본 min/max(2/20)가 남아 있다 — 그대로 역산하면 채택 후 apply 가
    # 꺼둔 HPA 를 다시 켠다. 고정 replica 는 스키마가 표현 못 하므로 notes 만.
    asg_enabled = asc.get("enabled")
    if asg_enabled is False:
        notes.append("gatewayProxy.autoscaling.enabled=false — 고정 replica "
                     "운영으로 간주하고 sizing 을 기록하지 않습니다 (스키마에 "
                     "HPA off 표현이 없습니다 — env values 가 소유)")
    else:
        for src, dst in (("minReplicas", "min_replicas"),
                         ("maxReplicas", "max_replicas")):
            if str(asc.get(src, "")).isdigit():
                gp[dst] = int(asc[src])
        btp = str(genv.get("BEDROCK_THREAD_POOL_SIZE", ""))
        if btp.isdigit():
            gp["bedrock_thread_pool_size"] = int(btp)
        if gp:
            sizing["gateway-proxy"] = gp
    pool = str(_val(v, "redis", "poolSize"))
    if pool.isdigit():
        sizing["redis"] = {"pool_size": int(pool)}

    capacity_tier = ""
    if sizing:
        from .tiers import EKS_CAPACITY_TIERS
        for name, band in EKS_CAPACITY_TIERS.items():
            b = band["gateway-proxy"]
            if (gp.get("min_replicas") == b["min_replicas"]
                    and gp.get("max_replicas") == b["max_replicas"]
                    and gp.get("bedrock_thread_pool_size", 128)
                    == b.get("bedrock_thread_pool_size", 128)
                    and sizing.get("redis", {}).get("pool_size", 150)
                    == band["redis"]["pool_size"]):
                capacity_tier = name
                break
        if capacity_tier:
            notes.append(f"라이브 용량이 capacity_tier={capacity_tier!r} 밴드와 "
                         "일치합니다 — 밴드 키로 기록했습니다")
        else:
            notes.append("라이브 HPA/redis pool 을 deploy.sizing 으로 기록했습니다 — "
                         "밴드와 일치하면 capacity_tier 로 바꿔도 됩니다")
    else:
        notes.append("⚠ autoscaling/redis.poolSize 를 helm values 에서 못 읽었습니다")

    notes.append("size_tier 는 eks=t3 으로 둡니다 — eks 에서는 문서용 키라 라이브 "
                 "신호가 없어 의도적으로 역산하지 않습니다 (스펙은 HPA/requests 참조)")

    # allowedAwsAccounts 가 계정 경계(VK 발급 대상 role 의 계정 고정)를 잡는
    # 보안 키다 — 캡처가 버리면 다음 apply 가 제한을 풀어 타 계정 role 도 허용된다.
    # 스키마는 단일 account_id 만 표현하므로 첫 계정을 기록하되, 다중 계정은
    # 나머지가 유실됨을 노트로 남긴다 (리스트 외 값은 방어적으로 무시).
    _allowed = _dmap(v.get("aws")).get("allowedAwsAccounts")
    _acct = ""
    if isinstance(_allowed, list) and _allowed:
        _acct = str(_allowed[0])
        if len(_allowed) > 1:
            notes.append(
                f"allowedAwsAccounts 에 계정 {len(_allowed)}개가 있습니다: "
                f"{_allowed} — 스키마는 account_id 1개만 표현해 첫 항목만 기록합니다. "
                "다계정 허용이 의도면 env values 로 관리하세요")
    elif _allowed:
        notes.append(f"allowedAwsAccounts 가 list 가 아닙니다({_allowed!r}) — "
                     "account_id 기록을 건너뜁니다")

    doc = {
        "version": 1,
        "env": env,
        "aws": {"region": region,
                **({"account_id": _acct} if _acct else {})},
        # 실제 릴리스/namespace/env_dir 를 기록 — 기본값이 아닌 배포도
        # 채택 후 apply 가 같은 대상을 가리키도록
        "deploy": {"target": "eks", "size_tier": "t3",
                   "release": release, "namespace": namespace,
                   # repo 상대경로로 기록 — 절대경로는 다른 개발자 환경에서 깨진다
                   "tf_env_dir": _relpath(env_dir) if env_dir else "",
                   **tfstate,
                   **({"capacity_tier": capacity_tier} if capacity_tier
                      else {"sizing": sizing} if sizing else {})},
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
    if infra:
        doc["infra"] = infra
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
                 captured_raw: dict,
                 unknown_keys: set[str] | None = None) -> list[tuple[str, object]]:
    """캡처본을 통째로 채택(cp)하면 삭제되는 yaml 전용 키 — (path, declared_value).

    "못 읽은 키" 판정은 **원본** 캡처 doc 으로 한다. 정규화본(`asdict(from_dict)`)
    은 스키마 기본값("", False, 0)으로 빈칸을 채워, 캡처가 못 읽은 문자열 키
    (tfstate, oidc.client_secret — 캡처 자체가 방출하지 않음)와 "실제로 빈
    값이었던 키"를 구별하지 못한다. 원본 기준: 경로 부재 또는 ""/None 이면
    읽기 불가 — cp 는 물론 interactive 의 absorb 도 선언값을 지운다.
    []/{} 는 캡처가 명시적으로 방출하는 "읽은 빈 값"이라 absorb 대상으로 둔다
    (예: ecs allowed_cidrs — 라이브가 실제로 비어 있으면 채택 가능해야 한다).
    비어있는 선언값 자체는 지워져도 무의미하므로 제외한다.

    unknown_keys: 캡처가 "읽지 못해 기본값을 넣은" 키(예: env 부재로 false 를
    기록한 features.service_keys). 값이 들어있어도 실측이 아니라 unknown 으로
    취급 — 없으면 absorb 가 기본값으로 선언값을 조용히 덮는다.
    """
    unknown = unknown_keys or set()
    out = []
    for k, dv, _cv in diffs:
        if dv in (None, [], {}, ""):
            continue
        if k in unknown:
            out.append((k, dv))
            continue
        rv = _lookup(captured_raw, k)
        if rv is _MISSING or rv is None or rv == "":
            out.append((k, dv))
    return out
