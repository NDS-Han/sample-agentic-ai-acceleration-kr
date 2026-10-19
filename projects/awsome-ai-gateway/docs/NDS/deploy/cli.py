# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""deploy CLI — init / configure / validate / render / bootstrap / apply / doctor / client-values / teardown.

  deploy init           대화형으로 gateway.yaml 생성
  deploy configure      기존 설정을 기본값으로 다시 물어보고 저장(+배포)
  deploy validate       gateway.yaml 스키마·조합 검증
  deploy render         산출물 생성 (gen/<env>/ 아래 backend 별 파일)
  deploy bootstrap      [최초 1회] 신규 설치 — 인프라·시크릿·이미지·Cognito 전체
  deploy apply          업데이트/동기화 — render → 확인 → 실제 배포 → doctor
  deploy doctor         배포 상태·드리프트 점검 (--capture: 기존 배포 → yaml 역생성)
  deploy client-values  직원에게 전달할 OIDC/URL env 4줄 출력
  deploy teardown       배포 삭제 — backend 별 소유 범위만
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import schema, tiers
from .render import compose as compose_render
from .render import ecs as ecs_render

console = Console()

REPO_ROOT = Path(__file__).resolve().parents[3]  # docs/NDS/deploy/cli.py → 프로젝트 루트
DEFAULT_CONFIG = REPO_ROOT / "deployment" / "gateway.yaml"
GEN_ROOT = REPO_ROOT / "deployment" / "gen"


class Cancelled(Exception):
    pass


def _questionary():
    """questionary 가 없으면(런타임 의존 미설치) 설치법을 안내하고 종료."""
    try:
        import questionary
        return questionary
    except ImportError:
        from .capture import CaptureError
        raise CaptureError(
            "questionary 가 없습니다 — `pip install -r docs/NDS/deploy/requirements.txt` "
            "또는 docs/NDS/.venv 생성 후 사용하십시오.")


def _unwrap(v):
    if v is None:
        raise Cancelled
    return v


def _load_config(args):
    """gateway.yaml 로드 — 없으면 근처 캡처본/힌트를 찾아 다음 명령을 안내."""
    path = Path(args.config)
    if not path.exists():
        captured = sorted(path.parent.glob("gateway.captured-*.yaml"))
        hint = ""
        if captured:
            hint = ("\n캡처본이 있습니다 — 채택하거나 바로 쓸 수 있습니다:\n"
                    + "\n".join(f"  ./deploy validate --config {c}" for c in captured[:3])
                    + f"\n  cp {captured[0]} {path}   # 채택")
        console.print(f"[red]{path} 가 없습니다."
                      f"\n새 배포: ./deploy init   |   기존 배포 온보딩: ./deploy doctor --capture"
                      + hint)
        raise SystemExit(1)
    return schema.load(path)


def _aws_account_id(existing: str = "") -> str:
    """기존 값 우선 — 없으면 aws sts 로 조회. 실패 시 "" (backend.hcl 이 placeholder 로 안내)."""
    if existing:
        return existing
    import subprocess
    try:
        r = subprocess.run(["aws", "sts", "get-caller-identity",
                            "--query", "Account", "--output", "text"],
                           capture_output=True, text=True, timeout=15)
        acct = r.stdout.strip()
        if r.returncode == 0 and acct.isdigit():
            console.print(f"  [cyan]ⓘ aws.account_id 를 sts 로 조회했습니다: {acct}")
            return acct
    except (OSError, subprocess.TimeoutExpired):
        pass  # FileNotFoundError(미설치)/PermissionError(실행불가) 등 — sts 조회는 선택사항
    return ""


def _g(d: dict, *path, default=""):
    cur = d or {}
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def _collect_doc(existing: dict | None = None) -> dict:
    """대화형으로 gateway.yaml doc 을 만든다. existing 이 있으면 각 질문의
    기본값으로 현재 설정을 넣는다 (configure = init 의 prefill 버전)."""
    questionary = _questionary()
    from questionary import Choice
    ex = existing or {}

    env = _unwrap(questionary.text(
        "환경 이름 (소문자·숫자·하이픈, 예: acme-small)",
        default=str(_g(ex, "env")) or None).ask())
    region = _unwrap(questionary.text(
        "AWS 리전", default=str(_g(ex, "aws", "region")) or "ap-northeast-2").ask())

    target_default = _g(ex, "deploy", "target")
    target = _unwrap(questionary.select(
        "배포 대상",
        choices=[
            Choice("compose — 단일 호스트 Docker (평가·최소비용)", "compose"),
            Choice("ecs — ECS Fargate (소규모~표준 권장)", "ecs"),
            Choice("eks — EKS Fargate (대규모·기존 프로덕션)", "eks"),
        ],
        default=target_default or "compose").ask())

    tier_choices = [
        Choice(f"{t.name}: {t.label}  [{t.users}, {t.monthly_cost_usd}]", t.name)
        for t in tiers.TIERS.values() if t.backend == target
    ]
    prev_tier = _g(ex, "deploy", "size_tier")
    tier_vals = {c.value for c in tier_choices}
    size_tier = _unwrap(questionary.select(
        "규모 티어", choices=tier_choices,
        default=prev_tier if prev_tier in tier_vals else tier_choices[0].value).ask())
    same_target = target == _g(ex, "deploy", "target")

    domain_mode = _unwrap(questionary.select(
        "HTTPS 도메인",
        choices=[
            Choice("none — 도메인 없이 시작 (HTTP, Cowork 불가)", "none"),
            Choice("route53-acm — 도메인 확정 (자동 TLS)", "route53-acm"),
            Choice("cloudfront-temp — 임시 https (EKS 전용)", "cloudfront-temp"),
        ], default=_g(ex, "domain", "mode") or "none").ask())
    domain_name, zone_id = "", ""
    if domain_mode == "route53-acm":
        domain_name = _unwrap(questionary.text(
            "베이스 도메인 (예: example.com → gateway./admin./admin-api. 자동)",
            default=str(_g(ex, "domain", "name")) or None).ask())
        if target == "ecs":  # eks 는 env values 의 기존 인증서를 씀
            zone_id = _unwrap(questionary.text(
                "Route53 hosted zone ID (예: Z0123456ABCD)",
                default=str(_g(ex, "domain", "zone_id")) or None).ask())

    prev_notif = _g(ex, "features", "notifications", default={}) or {}
    notif = _unwrap(questionary.select(
        "알림 provider",
        choices=[Choice("mock — 발송 안 함(기본)", "mock"),
                 Choice("ses — Amazon SES", "ses"),
                 Choice("smtp — 외부 SMTP", "smtp"),
                 Choice("internal_api — 사내 메일 API", "internal_api")],
        default=prev_notif.get("provider", "mock")).ask())
    notif_extra = {}
    if notif == "ses":
        notif_extra["ses_from"] = _unwrap(questionary.text(
            "SES 발신 주소", default=str(prev_notif.get("ses_from", "")) or None).ask())
    elif notif == "smtp":
        notif_extra["smtp_host"] = _unwrap(questionary.text(
            "SMTP 호스트", default=str(prev_notif.get("smtp_host", "")) or None).ask())
        notif_extra["smtp_from"] = _unwrap(questionary.text(
            "발신 주소", default=str(prev_notif.get("smtp_from", "")) or None).ask())
        # 인증 Secret 은 백엔드별 취급이 달라(eks: k8s Secret 이름) 질문하지 않고 보존
        if prev_notif.get("smtp_credentials_secret"):
            notif_extra["smtp_credentials_secret"] = prev_notif["smtp_credentials_secret"]
    elif notif == "internal_api":
        notif_extra["internal_api_url"] = _unwrap(questionary.text(
            "사내 메일 API URL",
            default=str(prev_notif.get("internal_api_url", "")) or None).ask())
        notif_extra["internal_api_from"] = _unwrap(questionary.text(
            "발신 주소 (비우면 차트 기본)",
            default=str(prev_notif.get("internal_api_from", "")) or None).ask())

    prev_feat = _g(ex, "features", default={}) or {}
    features = _unwrap(questionary.checkbox(
        "추가 기능 (기본 전부 off)",
        choices=[
            Choice("web_search — AgentCore 웹검색 (us-east-1, apply 시 자동 프로비저닝)", "web_search",
                   checked=bool(prev_feat.get("web_search"))),
            Choice("body_logging — 요청 본문 S3 로깅", "body_logging",
                   checked=bool(prev_feat.get("body_logging"))),
            Choice("bi_insight — BI 어시스턴트 (AgentCore Runtime)", "bi_insight",
                   checked=bool(prev_feat.get("bi_insight"))),
            Choice("pricing_lambda — LiteLLM 단가 조회 Lambda", "pricing_lambda",
                   checked=bool(prev_feat.get("pricing_lambda"))),
            Choice("observability — OTel/Prometheus/Grafana (compose)", "observability",
                   checked=bool(prev_feat.get("observability"))),
        ]).ask())

    prev_oidc = _g(ex, "oidc", default={}) or {}
    oidc = {}
    oidc_on = _unwrap(questionary.confirm(
        "OIDC 로그인(Cognito 등)을 설정합니까?", default=bool(prev_oidc)).ask())
    if not oidc_on and prev_oidc:
        if not _unwrap(questionary.confirm(
                "기존 OIDC 설정이 있습니다 — 제거합니까?", default=False).ask()):
            oidc_on = True
            oidc = dict(prev_oidc)
    if oidc_on and not oidc:
        oidc["issuer_url"] = _unwrap(questionary.text(
            "OIDC issuer URL", default=str(prev_oidc.get("issuer_url", "")) or None).ask())
        oidc["client_id"] = _unwrap(questionary.text(
            "OIDC client id (admin-ui SSO)", default=str(prev_oidc.get("client_id", "")) or None).ask())
        oidc["authorize_url"] = _unwrap(questionary.text(
            "authorize URL", default=str(prev_oidc.get("authorize_url", "")) or None).ask())
        oidc["token_url"] = _unwrap(questionary.text(
            "token URL", default=str(prev_oidc.get("token_url", "")) or None).ask())
        # 질문하지 않는 OIDC 필드는 기존 값을 보존 — configure 한 번에
        # required_group/audience/client_secret 가 조용히 사라지는 걸 막는다
        for k in ("audience", "client_secret", "required_group", "provider_name",
                  "vk_ttl_hours"):
            if prev_oidc.get(k) and k not in oidc:
                oidc[k] = prev_oidc[k]
        vk_ttl = _unwrap(questionary.text(
            "VK 수명(시간) — 비우면 차트 기본(1시간)",
            default=str(prev_oidc.get("vk_ttl_hours", "")) or None).ask())
        if vk_ttl:
            try:
                oidc["vk_ttl_hours"] = int(vk_ttl)
            except ValueError:
                console.print("[yellow]vk_ttl_hours 가 숫자가 아닙니다 — 기존 값 유지")

    images = {}
    if target in ("ecs", "eks"):
        # target 이 바뀌면 이전 backend 의 이미지/tfstate 는 프리필하지 않는다
        prev_img = (_g(ex, "images", default={}) or {}) if same_target else {}
        images["registry"] = _unwrap(questionary.text(
            "외부 이미지 registry (비우면 ECR 자동 추론)",
            default=str(prev_img.get("registry", "")) or None).ask())
        images["tag"] = _unwrap(questionary.text(
            "이미지 태그 (명시적 핀 필수)",
            default=str(prev_img.get("tag", "")) or None).ask())
        # 서비스별 tags 맵은 질문으로 다루지 않는다 — configure 재실행 시
        # 이전 값을 보존한다 (US-19 같은 멀티태그 릴리스가 지워지지 않게)
        if prev_img.get("tags"):
            images["tags"] = prev_img["tags"]

    deploy_extra = {}
    if target in ("ecs", "eks"):
        prev_dep = (_g(ex, "deploy", default={}) or {}) if same_target else {}
        deploy_extra["tfstate_bucket"] = _unwrap(questionary.text(
            "Terraform state S3 bucket (비우면 llm-gateway-tfstate-<account>)",
            default=str(prev_dep.get("tfstate_bucket", "")) or None).ask())
        deploy_extra["tfstate_table"] = _unwrap(questionary.text(
            "Terraform lock DynamoDB table (없으면 비움)",
            default=str(prev_dep.get("tfstate_table", "")) or None).ask())
        deploy_extra["release"] = prev_dep.get("release") or "llm-gateway"
        deploy_extra["namespace"] = prev_dep.get("namespace") or "llm-gateway"
        deploy_extra["tf_env_dir"] = prev_dep.get("tf_env_dir") or ""
        # 질문하지 않는 deploy 필드(sizing override 등)는 보존
        if prev_dep.get("sizing"):
            deploy_extra["sizing"] = prev_dep["sizing"]

    prev_cidrs = _g(ex, "network", "allowed_cidrs", default=[]) or []
    allowed = _unwrap(questionary.text(
        "접근 허용 CIDR (쉼표, 비우면 전체 허용 — 경고 대상)",
        default=",".join(str(c) for c in prev_cidrs)).ask())
    admin_internal = bool(_g(ex, "network", "admin_internal"))
    if target == "eks":
        admin_internal = _unwrap(questionary.confirm(
            "admin 콘솔(admin-ui/admin-api)만 internal ALB 로 내립니까? "
            "(gateway 는 public 유지 — S2S VPN/사내망 전제)",
            default=admin_internal).ask())

    # infra.* 는 블록 통째 보존이 원칙 (bootstrap wizard 가 기록한 azs/cognito/
    # bedrock 등). ecs 경로에만 DB/캐시 크기 고급 분기를 연다 — 나머지 target
    # 에서는 기존 선언을 그대로 둔다(schema 가 ecs 전용을 강제).
    infra_doc = dict(_g(ex, "infra", default={}) or {})
    if target == "ecs":
        prev_db = infra_doc.get("db") or {}
        prev_cache = infra_doc.get("cache") or {}
        t = tiers.preset(size_tier)
        adv = _unwrap(questionary.confirm(
            f"DB/캐시 크기를 직접 정하시겠습니까? (기본: 티어 {size_tier} 프리셋 — "
            f"Aurora {t.db_mode} {t.db_spec}, cache {t.cache_mode})",
            default=bool(prev_db or prev_cache)).ask())
        if adv:
            db_mode = _unwrap(questionary.select(
                "DB 모드",
                choices=[Choice("serverless — Aurora Serverless v2 (ACU 자동 스케일)",
                                "serverless"),
                         Choice("provisioned — 고정 인스턴스", "provisioned")],
                default=prev_db.get("mode")
                or ("serverless" if t.db_mode == "rds-serverless" else "provisioned")).ask())
            db_doc: dict = {"mode": db_mode}
            if db_mode == "serverless":
                lo = _unwrap(questionary.text(
                    "serverless min ACU (비용 하한 — 0.5 단위, 0=일시정지)",
                    default=str(prev_db.get("min_acu", t.db_spec.get("min_acu", 0.5)))).ask())
                hi = _unwrap(questionary.text(
                    "serverless max ACU (스케일 천장 — DB 커넥션 한도와 연동)",
                    default=str(prev_db.get("max_acu", t.db_spec.get("max_acu", 4.0)))).ask())
                try:
                    db_doc["min_acu"] = float(lo)
                    db_doc["max_acu"] = float(hi)
                except ValueError:
                    console.print("[yellow]ACU 가 숫자가 아닙니다 — 기존 값 유지")
                    db_doc.update({k: v for k, v in prev_db.items()
                                   if k in ("min_acu", "max_acu")})
            else:
                db_doc["instance_class"] = _unwrap(questionary.text(
                    "DB 인스턴스 클래스",
                    default=str(prev_db.get("instance_class") or "db.r7g.large")).ask())
            infra_doc["db"] = db_doc

            cache_mode = _unwrap(questionary.select(
                "ElastiCache 토폴로지",
                choices=[Choice("single — 단일 노드 (최소 비용)", "single"),
                         Choice("replicated — replica 1대 failover", "replicated"),
                         Choice("cluster — cluster mode", "cluster")],
                default=prev_cache.get("mode")
                or ("single" if t.cache_mode == "elasticache-single" else "replicated")).ask())
            cache_doc: dict = {
                "mode": cache_mode,
                "node_type": _unwrap(questionary.text(
                    "ElastiCache node type",
                    default=str(prev_cache.get("node_type")
                                or t.cache_spec.get("node_type", "cache.t4g.small"))).ask()),
            }
            # 토폴로지는 mode 와 맞는 키만 — mode 전환 시 이전 mode 의 값을
            # 남기면 terraform 에서 조용히 무시되므로 다시 묻는다
            if cache_mode == "cluster":
                ng = _unwrap(questionary.text(
                    "cluster 샤드 수 (num_node_groups, 1~90 — 용량·비용 배수)",
                    default=str(prev_cache.get(
                        "num_node_groups", 3))).ask())
                rp = _unwrap(questionary.text(
                    "샤드당 replica 수 (1~5, 2=zero-redundancy 제거)",
                    default=str(prev_cache.get(
                        "replicas_per_node_group", 1))).ask())
                try:
                    cache_doc["num_node_groups"] = int(ng)
                    cache_doc["replicas_per_node_group"] = int(rp)
                except ValueError:
                    console.print("[yellow]정수가 아닙니다 — 기존 값 유지")
                    for k in ("num_node_groups", "replicas_per_node_group"):
                        if prev_cache.get(k) is not None:
                            cache_doc[k] = prev_cache[k]
            else:
                nc = _unwrap(questionary.text(
                    "노드 수 (single=1, replicated=2 이상 — primary+replica)",
                    default=str(prev_cache.get(
                        "num_clusters", 2 if cache_mode == "replicated" else 1))).ask())
                try:
                    cache_doc["num_clusters"] = int(nc)
                except ValueError:
                    console.print("[yellow]정수가 아닙니다 — 기존 값 유지")
                    if prev_cache.get("num_clusters") is not None:
                        cache_doc["num_clusters"] = prev_cache["num_clusters"]
            infra_doc["cache"] = cache_doc
        else:
            # 고급 분기를 끈 경우에도 기존 선언은 지우지 않는다 — 명시적 제거만
            if (prev_db or prev_cache) and _unwrap(questionary.confirm(
                    "기존 infra.db/infra.cache 선언이 있습니다 — 제거합니까?",
                    default=False).ask()):
                infra_doc.pop("db", None)
                infra_doc.pop("cache", None)

    doc = {
        "version": 1,
        "env": env,
        "aws": {"region": region,
                # account_id 는 tfstate 추론에 쓰인다 — 기존 값 보존, 없으면
                # ecs/eks 경로에서 aws sts 로 자동 조회 (없으면 비워두고
                # backend.hcl 이 placeholder + 경고를 낸다)
                **({"account_id": _aws_account_id(str(_g(ex, "aws", "account_id")))}
                   if (_g(ex, "aws", "account_id") or target in ("ecs", "eks")) else {})},
        "deploy": {"target": target, "size_tier": size_tier, **deploy_extra},
        "network": {"mode": _g(ex, "network", "mode") or "public",
                    "allowed_cidrs": [c.strip() for c in allowed.split(",") if c.strip()],
                    "admin_internal": admin_internal},
        "domain": {"mode": domain_mode, "name": domain_name, "zone_id": zone_id},
        "features": {
            "notifications": {"provider": notif, **notif_extra},
            "web_search": "web_search" in features,
            "body_logging": "body_logging" in features,
            "bi_insight": "bi_insight" in features,
            "pricing_lambda": "pricing_lambda" in features,
            "observability": "observability" in features,
        },
        "clients": _g(ex, "clients", default={}) or {"models_profile": "global"},
    }
    if infra_doc:
        doc["infra"] = infra_doc
    if oidc:
        doc["oidc"] = oidc
    if images:
        doc["images"] = images
    return doc


def _write_config(out_path: Path, doc: dict) -> int:
    try:
        cfg = schema.from_dict(doc)
    except schema.SchemaError as exc:
        console.print(f"[red]입력 조합이 유효하지 않습니다:\n{exc}")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "# gateway.yaml — 이 배포의 source of truth.\n"
        "# 변경 → `deploy configure`(대화형) 또는 직접 편집 → render → apply.\n"
        + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    out_path.chmod(0o600)  # oidc.client_secret 등이 들어갈 수 있어 소유자 전용
    console.print(f"[green]저장: {out_path}")
    for w in cfg.warnings():
        console.print(f"[yellow]⚠ {w}")
    return 0


def cmd_init(args) -> int:
    out_path = Path(args.out) if args.out else DEFAULT_CONFIG
    if out_path.exists() and not args.force:
        console.print(f"[yellow]{out_path} 가 이미 있습니다 — 덮어쓰려면 --force 를 붙이세요.")
        return 1
    console.print(Panel("LLM Gateway 배포 설정 마법사 — 답하면 gateway.yaml 을 만듭니다"))
    doc = _collect_doc()
    if _write_config(out_path, doc):
        return 1
    console.print(f"다음: [bold]deploy render --config {out_path}")
    return 0


def cmd_configure(args) -> int:
    """기존 gateway.yaml 을 기본값으로 다시 물어보고, 원하면 바로 배포까지."""
    questionary = _questionary()
    cfg_path = Path(args.config)
    if cfg_path.name.startswith("gateway.captured-"):
        console.print(f"[yellow]{cfg_path.name} 은 캡처 산출물입니다 — "
                      f"검토본을 보존하기 위해 {DEFAULT_CONFIG} 에 씁니다")
        cfg_path = DEFAULT_CONFIG

    def _read_doc(p: Path) -> dict:
        try:
            return yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError as exc:
            raise schema.SchemaError(f"{p} 의 YAML이 유효하지 않습니다: {exc}")
        except UnicodeDecodeError as exc:
            raise schema.SchemaError(f"{p} 가 UTF-8 텍스트가 아닙니다: {exc}")

    existing: dict = {}
    if cfg_path.exists():
        existing = _read_doc(cfg_path)
        console.print(Panel(
            f"{cfg_path} 를 기본값으로 다시 설정합니다 — Enter 는 현재 값 유지"))
    else:
        captured = sorted(cfg_path.parent.glob("gateway.captured-*.yaml"))
        if captured:
            console.print(f"[yellow]{cfg_path} 없음 — 캡처본 {captured[0].name} 을 기본값으로 씁니다")
            existing = _read_doc(captured[0])
        else:
            console.print(f"[yellow]{cfg_path} 없음 — 새로 만듭니다 (init 과 동일)")

    doc = _collect_doc(existing)
    if _write_config(cfg_path, doc):
        return 1

    # 마지막에 배포까지 — 확인 게이트는 그대로 (plan 출력 후 y)
    if args.no_apply:
        console.print(f"다음: [bold]deploy render --config {cfg_path}")
        return 0
    go = _unwrap(questionary.confirm(
        "지금 배포 절차를 진행합니까? (render → plan 미리보기 → 확인 → apply)",
        default=True).ask())
    if not go:
        console.print(f"나중에: [bold]./deploy apply --config {cfg_path}")
        return 0
    import types
    apply_args = types.SimpleNamespace(config=str(cfg_path), build=False,
                                       plan=False, yes=False,
                                       context=getattr(args, "context", "") or "")
    return cmd_apply(apply_args)


def cmd_validate(args) -> int:
    try:
        cfg = _load_config(args)
    except schema.SchemaError as exc:
        console.print(f"[red]gateway.yaml 오류:\n{exc}")
        return 1
    console.print("[green]스키마 유효")
    for w in cfg.warnings():
        console.print(f"[yellow]⚠ {w}")
    t = tiers.preset(cfg.deploy.size_tier)
    table = Table(title=f"{cfg.env} — {t.label}")
    table.add_column("항목"); table.add_column("값")
    table.add_row("target / tier", f"{cfg.deploy.target} / {cfg.deploy.size_tier}")
    table.add_row("users", t.users)
    if cfg.deploy.target == "ecs":
        # infra.db/infra.cache override 가 반영된 실효 spec — tier 기본과
        # 다르면 그 사실을 표시한다
        db, cache = tiers.effective_db_spec(cfg), tiers.effective_cache_spec(cfg)
        db_over = (db["mode"] != ("serverless" if t.db_mode == "rds-serverless"
                                  else "provisioned")
                   or db["min_acu"] != t.db_spec.get("min_acu")
                   or db["max_acu"] != t.db_spec.get("max_acu")
                   or db["instance_class"] != t.db_spec.get("instance_class", ""))
        table.add_row("db (실효)", f"{db['mode']} {db}"
                                  + ("  [infra.db override]" if db_over else ""))
        _ctopo = (f"{cache['num_node_groups']}sh×"
                  f"{1 + cache['replicas_per_node_group']}nodes"
                  if cache["mode"] == "cluster"
                  else f"{cache['num_clusters']}node")
        table.add_row("cache (실효)",
                      f"{cache['mode']} {cache['node_type']} {_ctopo}")
    else:
        table.add_row("db", f"{t.db_mode} {t.db_spec}")
        table.add_row("cache", f"{t.cache_mode} {t.cache_spec}")
    table.add_row("ha", t.ha)
    table.add_row("monthly(est)", t.monthly_cost_usd)
    console.print(table)
    return 0


def cmd_render(args) -> int:
    try:
        cfg = _load_config(args)
    except schema.SchemaError as exc:
        console.print(f"[red]gateway.yaml 오류:\n{exc}")
        return 1
    for w in cfg.warnings():
        console.print(f"[yellow]⚠ {w}")

    if cfg.deploy.target == "compose":
        out = GEN_ROOT / cfg.env
        res = compose_render.render(cfg, out)
        console.print(f"[green]생성 완료 ({out}):")
        for f in res["files"]:
            console.print(f"  {f}")
        if res["filled"]:
            console.print(f"  [cyan]새로 생성된 시크릿: {', '.join(res['filled'])}")
        if res["preserved"]:
            console.print(f"  [yellow]기존 값과 달라 보존됨(수동 확인): {', '.join(res['preserved'])}")
        for n in res.get("notes", []):
            console.print(f"  [yellow]ⓘ {n}")
        console.print(
            f"\n기동: [bold]docker compose --env-file {out/'.env'} "
            f"-f {out/'docker-compose.yml'} up -d --build[/bold]\n"
            "  (--env-file 필수 — 없으면 ${POSTGRES_PASSWORD} 등이 repo 루트 .env 나\n"
            "   기본값으로 interpolate 되어 앱과 DB의 비밀번호가 어긋납니다)")
        return 0

    if cfg.deploy.target == "ecs":
        out = GEN_ROOT / cfg.env / "ecs"
        res = ecs_render.render(cfg, out)
        console.print(f"[green]생성 완료 ({out}):")
        for f in res["files"]:
            console.print(f"  {f}")
        for n in res.get("notes", []):
            console.print(f"  [yellow]ⓘ {n}")
        console.print(
            f"\n적용: [bold]terraform -chdir={res['env_dir']} init -backend-config={out/'backend.hcl'} "
            f"&& terraform -chdir={res['env_dir']} apply -var-file={out/'terraform.tfvars'}[/bold]\n"
            "  또는 ./deploy apply 가 이 절차를 실행합니다.")
        return 0

    if cfg.deploy.target == "eks":
        from .render import eks as eks_render
        out = GEN_ROOT / cfg.env / "eks"
        res = eks_render.render(cfg, out)
        console.print(f"[green]생성 완료 ({out}):")
        for f in res["files"]:
            console.print(f"  {f}")
        for n in res.get("notes", []):
            console.print(f"  [yellow]ⓘ {n}")
        console.print("\n적용: [bold]./deploy apply[/bold] (helm upgrade --install, 확인 게이트 있음)")
        return 0

    console.print(f"[red]deploy.target={cfg.deploy.target} 렌더러는 아직 구현 전입니다 (compose, ecs 지원).")
    return 1


def _confirm_apply(args, preview=None) -> bool:
    """실제 변경 전 확인 게이트. --yes/-y 또는 CI 가 아니면 preview 출력 후 y 확인."""
    if getattr(args, "yes", False):
        return True
    if preview:
        try:
            preview()
        except SystemExit as exc:
            console.print(f"[red]plan 실패 — 적용하지 않습니다: {exc}")
            return False
    if not sys.stdin.isatty():
        console.print("[red]대화형이 아닙니다 — 확인 없이 적용하려면 --yes 를 쓰십시오")
        return False
    try:
        ok = _unwrap(_questionary().confirm(
            "위 계획을 실제 환경에 적용합니까?", default=False).ask())
    except Exception as exc:  # questionary 미설치 등 — 적용 거부가 안전 쪽
        console.print(f"[red]확인 프롬프트 실패 — 적용하지 않습니다: {exc}")
        return False
    return bool(ok)


def cmd_apply(args) -> int:
    """render → 기동 → doctor 한 단계로 (backend 별 실행기)."""
    try:
        cfg = _load_config(args)
    except schema.SchemaError as exc:
        console.print(f"[red]gateway.yaml 오류:\n{exc}")
        return 1

    if cfg.deploy.target == "ecs":
        from . import ecs_apply, doctor as doc
        out = GEN_ROOT / cfg.env / "ecs"
        res = ecs_render.render(cfg, out)
        console.print(f"[green]render 완료 ({out})")
        for n in res.get("notes", []):
            console.print(f"  [yellow]ⓘ {n}")
        if args.plan:
            try:
                ecs_apply.terraform_plan(cfg, out, REPO_ROOT)
            except SystemExit as exc:
                console.print(f"[red]{exc}")
                return 1
            console.print("\n[cyan]plan 전용 — 아무것도 적용되지 않았습니다. 적용: ./deploy apply")
            return 0
        if not _confirm_apply(args, preview=lambda: ecs_apply.terraform_plan(cfg, out, REPO_ROOT)):
            return 130
        try:
            outputs = ecs_apply.apply(cfg, out, REPO_ROOT)
        except SystemExit as exc:
            console.print(f"[red]{exc}")
            return 1
        console.print("\n[green]배포 완료 — 엔드포인트:")
        for k in ("gateway_url", "admin_ui_url", "alb_dns_name"):
            if outputs.get(k):
                console.print(f"  {k}: {outputs[k]}")
        rep = doc.check_ecs(cfg, GEN_ROOT / cfg.env, REPO_ROOT)
        console.print(doc.format_report(cfg, rep))
        return 0 if rep.worst != "HIGH" else 1

    if cfg.deploy.target == "eks":
        from . import eks_apply, doctor as doc
        from .render import eks as eks_render
        context = getattr(args, "context", "") or ""
        out = GEN_ROOT / cfg.env / "eks"
        res = eks_render.render(cfg, out)
        console.print(f"[green]render 완료 ({out})")
        for n in res.get("notes", []):
            console.print(f"  [yellow]ⓘ {n}")
        if args.plan:
            try:
                eks_apply.plan(cfg, out, REPO_ROOT, context)
            except SystemExit as exc:
                console.print(f"[red]{exc}")
                return 1
            console.print("\n[cyan]plan 전용 — 적용되지 않았습니다. 적용: ./deploy apply")
            return 0
        if not _confirm_apply(args, preview=lambda: eks_apply.plan(cfg, out, REPO_ROOT, context)):
            return 130
        try:
            result = eks_apply.apply(cfg, out, REPO_ROOT, context)
        except SystemExit as exc:
            console.print(f"[red]{exc}")
            return 1
        console.print(f"\n[green]helm upgrade 완료 — {result['release']} @ ns={result['namespace']}")
        rep = doc.check_eks(cfg, GEN_ROOT / cfg.env, REPO_ROOT)
        console.print(doc.format_report(cfg, rep))
        return 0 if rep.worst != "HIGH" else 1

    if cfg.deploy.target != "compose":
        console.print(f"[red]deploy.target={cfg.deploy.target} 의 apply 는 아직 구현 전입니다.")
        return 1

    import subprocess
    out = GEN_ROOT / cfg.env
    res = compose_render.render(cfg, out)
    console.print(f"[green]render 완료 ({out})")
    for n in res.get("notes", []):
        console.print(f"  [yellow]ⓘ {n}")

    env_file = out / ".env"
    compose_file = out / "docker-compose.yml"
    argv = ["docker", "compose", "--env-file", str(env_file), "-f", str(compose_file), "up", "-d"]
    if args.build:
        argv.append("--build")
    console.print(f"[cyan]$ {' '.join(argv)}")
    if args.plan:
        if cfg.features.web_search:
            from . import websearch
            url = websearch.status_url(cfg.env, REPO_ROOT)
            console.print("[cyan]web_search: AgentCore Gateway "
                          f"{url or '없음 — apply 시 프로비저닝'}")
        console.print("[cyan]plan 전용 — 기동하지 않았습니다. 적용: ./deploy apply")
        return 0
    if not _confirm_apply(args):
        return 130
    if cfg.features.web_search:
        # install-guide §5-1/§5-2 자동화 — AgentCore Gateway(멱등) 생성 후 URL 을
        # .env 에 주입. merge_env 가 비관리 키를 보존하므로 다음 render 도 유지된다.
        from . import websearch
        url = websearch.ensure_gateway_url(cfg.env, REPO_ROOT)
        if url:
            websearch.upsert_env_file(env_file, {
                "AGENTCORE_GATEWAY_URL": url,
                "AGENTCORE_REGION": websearch.AGENTCORE_REGION,
                "AGENTCORE_TARGET_ID": websearch.TARGET_ID})
        else:
            console.print("[yellow]⚠ web_search URL 을 얻지 못했습니다 — "
                          "플래그만 켜진 채로 기동합니다")
    try:
        r = subprocess.run(argv)
    except FileNotFoundError:
        console.print("[red]docker 명령을 못 찾았습니다 — Docker(compose plugin) 설치 필요")
        return 1
    if r.returncode != 0:
        console.print("[red]compose up 실패 — 로그를 확인하십시오")
        return 1

    # 기동 직후 doctor — 완료된 것과 아직 준비 중인 것을 구분해 보고
    from . import doctor as doc
    rep = doc.check_compose(cfg, out)
    console.print(doc.format_report(cfg, rep))
    return 0 if rep.worst != "HIGH" else 1


def _confirm_destroy(env: str, args) -> bool:
    """삭제 확인 — y/n 이 아니라 환경 이름을 그대로 타이핑하게 한다."""
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        console.print("[red]대화형이 아닙니다 — 확인 없이 삭제하려면 --yes 를 쓰십시오")
        return False
    try:
        typed = _unwrap(_questionary().text(
            f"삭제를 확인하려면 환경 이름 '{env}' 를 그대로 입력하세요").ask())
    except Cancelled:
        raise
    except Exception as exc:
        console.print(f"[red]확인 실패 — 삭제하지 않습니다: {exc}")
        return False
    if typed != env:
        console.print("[yellow]입력이 환경 이름과 다릅니다 — 취소됨")
        return False
    return True


def _teardown_compose(cfg: schema.GatewayConfig, args) -> int:
    import subprocess
    out = GEN_ROOT / cfg.env
    compose_file = out / "docker-compose.yml"
    if not compose_file.exists():
        console.print(f"[red]{compose_file} 없음 — render 산출물이 없습니다 "
                      "(기동 중인 스택은 `docker compose ls` 로 확인)")
        return 1
    env_file = out / ".env"
    if not env_file.exists():
        console.print(f"[red]{env_file} 없음 — `deploy render` 후 재시도하거나, "
                      "수작업으로 올린 배포면 그 디렉토리에서 compose down 하십시오")
        return 1
    purge = getattr(args, "purge", False)
    console.print(Panel(
        f"삭제: compose 프로젝트 llm-gateway-{cfg.env} 의 컨테이너 + 네트워크\n"
        + ("[red]+ 볼륨 전부(--volumes) — postgres DB 데이터가 영구 삭제됩니다[/red]"
           if purge else
           "볼륨(pgdata 등)은 보존됩니다 — 함께 지우려면 --purge")
        + f"\n남는 것: {out}/ 산출물(.env 시크릿 포함), 빌드된 이미지",
        title=f"teardown — {cfg.env} (compose)", expand=False))
    if not _confirm_destroy(cfg.env, args):
        return 130
    argv = ["docker", "compose", "--env-file", str(env_file),
            "-f", str(compose_file), "down", "--remove-orphans"]
    if purge:
        argv.append("--volumes")
    console.print(f"[cyan]$ {' '.join(argv)}")
    try:
        r = subprocess.run(argv)
    except FileNotFoundError:
        console.print("[red]docker 명령을 못 찾았습니다 — Docker(compose plugin) 설치 필요")
        return 1
    if r.returncode != 0:
        console.print("[red]compose down 실패 — 로그를 확인하십시오")
        return 1
    if cfg.features.web_search:
        # env 규칙명의 AgentCore Gateway — 우리가 만든 외부 리소스라 함께 정리
        from . import websearch
        websearch.teardown(cfg.env, REPO_ROOT)
    console.print(f"[green]삭제 완료 — 산출물 디렉토리는 남아 있습니다: {out}")
    return 0


def _teardown_ecs(cfg: schema.GatewayConfig, args) -> int:
    from . import ecs_apply
    out = GEN_ROOT / cfg.env / "ecs"
    env_dir = REPO_ROOT / ecs_apply.ENV_DIR
    # destroy 도 required 변수(environment/aws_region/image_tag)가 필요 —
    # 현재 선언으로 tfvars 를 재생성한다
    ecs_render.render(cfg, out, extra_vars=ecs_apply.load_extra_vars(out))
    ecs_apply._run(["terraform", "init", "-input=false", "-reconfigure",
                    f"-backend-config={out / 'backend.hcl'}"], cwd=env_dir)
    console.print("[bold]삭제 미리보기 (terraform plan -destroy):[/bold]")
    ecs_apply._run(["terraform", "plan", "-destroy", "-input=false",
                    f"-var-file={out / 'terraform.tfvars'}"], cwd=env_dir)
    safeguards_on = tiers.preset(cfg.deploy.size_tier).ha != "none"
    console.print(Panel(
        "삭제: 위 plan 의 모든 AWS 리소스 — VPC/Aurora/ElastiCache/ALB/ECS/"
        "Cognito/Secrets Manager/ECR(repo+이미지)\n[red]이 배포의 데이터는 "
        "영구 삭제됩니다.[/red]"
        + ("\n+ AgentCore WebSearch Gateway(us-east-1) — terraform 관리 밖의 "
           "프로비저닝 리소스" if cfg.features.web_search else "")
        + (f"\n+ LiteLLM pricing Lambda llm-gateway-{cfg.env}-litellm-pricing"
           " (+role) — boto3 프로비저닝 리소스" if cfg.features.pricing_lambda
           else "")
        + ("\n[yellow]db_safeguards(deletion protection)가 켜져 있어 먼저 해제 "
           "apply 후 destroy 합니다 — Aurora 최종 스냅샷 없이 완전 삭제됩니다. "
           "보존이 필요하면 지금 중단하고 `aws rds create-db-cluster-snapshot` "
           "으로 수동 스냅샷을 먼저 만드세요.[/yellow]" if safeguards_on else ""),
        title=f"teardown — {cfg.env} (ecs)", expand=False))
    if not _confirm_destroy(cfg.env, args):
        return 130
    try:
        if safeguards_on:
            ecs_render.render(cfg, out, extra_vars={
                **ecs_apply.load_extra_vars(out), "db_safeguards": False})
            ecs_apply._run(["terraform", "apply", "-auto-approve", "-input=false",
                            f"-var-file={out / 'terraform.tfvars'}"], cwd=env_dir)
        ecs_apply._run(["terraform", "destroy", "-auto-approve", "-input=false",
                        f"-var-file={out / 'terraform.tfvars'}"], cwd=env_dir)
    except SystemExit:
        raise SystemExit("teardown 중간 실패 — 일부 리소스가 남았을 수 있습니다. 잔여는 "
                         f"`terraform -chdir={env_dir} plan -destroy` 로 확인하십시오")
    if cfg.features.web_search:
        # terraform 관리 밖의 프로비저닝 리소스 — 마지막에 정리
        from . import websearch
        websearch.teardown(cfg.env, REPO_ROOT)
    if cfg.features.pricing_lambda:
        # boto3 프로비저닝 리소스 — invoke 정책은 모듈 소유라 destroy 가 정리
        from . import pricinglambda
        pricinglambda.teardown(cfg, detach_invoke=False)
    console.print("[green]teardown 완료 — AWS 리소스가 삭제됐습니다")
    return 0


def _teardown_eks(cfg: schema.GatewayConfig, args) -> int:
    from . import eks_apply
    context = getattr(args, "context", "") or ""
    meta = eks_apply.load_meta(GEN_ROOT / cfg.env / "eks", REPO_ROOT)
    infra = getattr(args, "infra", False)
    env_dir = REPO_ROOT / meta["env_dir"] if meta.get("env_dir") else None
    if infra and (not env_dir or not env_dir.is_dir()):
        console.print(f"[red]terraform env 디렉토리가 없습니다: "
                      f"{meta.get('env_dir') or '(deploy.yaml 에 미기록)'} — "
                      "인프라 삭제는 해당 env 디렉토리에서 terraform destroy 하십시오")
        return 1

    lines = [f"삭제: helm release '{meta['release']}' (ns={meta['namespace']}) — 게이트웨이 앱"]
    if cfg.features.web_search:
        lines.append("+ AgentCore WebSearch Gateway(us-east-1) — helm/terraform "
                     "관리 밖의 프로비저닝 리소스")
    if cfg.features.pricing_lambda:
        lines.append(f"+ LiteLLM pricing Lambda llm-gateway-{cfg.env}-litellm-pricing"
                     " (+role, admin-api invoke 정책) — boto3 프로비저닝 리소스")
    if infra:
        lines.append(
            f"[red]+ 인프라 — terraform destroy {meta['env_dir']}:\n"
            "    EKS 클러스터·Aurora·ElastiCache·Cognito·Secrets Manager·VPC·"
            "IRSA·ALB controller 전부[/red]\n"
            "    ⚠️ Aurora deletion protection(prod/provisioned)이 켜져 있으면 destroy 가 "
            "실패합니다 — env 디렉토리 설정으로 먼저 해제하세요. 최종 스냅샷 여부도 그쪽이 결정합니다.\n"
            "남는 것: terraform state backend, Secrets 복구 유예기간(~7일), 수동 생성 리소스")
    else:
        lines.append(
            f"[yellow]남는 것: 인프라 전부 (EKS·Aurora·ElastiCache·Cognito·Secrets·IRSA) — "
            f"terraform env({meta.get('env_dir', '?')}) 소유. 함께 지우려면 --infra[/yellow]\n"
            "참고: uninstall 후 ns 에 k8s secrets/pvc 잔여물이 남을 수 있습니다.")
    console.print(Panel("\n".join(lines),
                        title=f"teardown — {cfg.env} (eks)", expand=False))

    if infra:
        # 삭제 대상을 먼저 보여준다 (read-only). eks env 의 backend.tf 는
        # partial config — bucket/table 은 최초 init 시 -backend-config 로 주입돼
        # .terraform 에 저장되므로 -reconfigure 를 쓰면 그 설정을 버려 실패한다
        try:
            eks_apply._run(["terraform", "init", "-input=false"], cwd=env_dir)
        except SystemExit:
            raise SystemExit(
                f"{env_dir} init 실패 — 이 env 는 partial backend 라 저장된 "
                ".terraform 이 없으면 init 할 수 없습니다. 최초 구성 때와 같은 "
                "-backend-config(bucket/dynamodb_table)로 먼저 init 하십시오")
        console.print("[bold]삭제 미리보기 (terraform plan -destroy):[/bold]")
        eks_apply._run(["terraform", "plan", "-destroy", "-input=false"], cwd=env_dir)

    if not _confirm_destroy(cfg.env, args):
        return 130

    ctx_args = ["--kube-context", context] if context else []
    r = eks_apply._run_quiet(
        ["helm", "status", meta["release"], "-n", meta["namespace"], *ctx_args],
        cwd=REPO_ROOT)
    uninstalled = False
    if r.returncode == 0:
        # 앱을 먼저 내려야 ALB controller 가 만든 ALB/타겟그룹이 정리되고
        # --infra 의 VPC destroy 가 의존성에 막히지 않는다
        eks_apply._run(["helm", "uninstall", meta["release"], "-n",
                        meta["namespace"], "--wait", *ctx_args], cwd=REPO_ROOT)
        uninstalled = True
    elif r.returncode == 127:
        # helm 부재를 release 부재와 혼동하면 앱을 안 내리고 인프라만 지워
        # ALB 잔여물이 destroy 를 멈추게 한다
        console.print("[red]helm 명령을 못 찾았습니다 — helm 설치 후 재시도하십시오")
        return 1
    elif "not found" in f"{r.stdout}{r.stderr}".lower():
        console.print("ⓘ release 가 없습니다 — helm 단계 생략")
    else:
        # 클러스터 접속 실패·잘못된 context 도 rc=1 — release 부재로 착각해
        # 건너뛰면 --infra 가 살아있는 앱 아래에서 인프라를 지운다
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        console.print(f"[red]helm status 실패 (rc={r.returncode})"
                      f"{' — ' + tail[-1] if tail else ''}\n"
                      "클러스터 접속·kubeconfig context(--context)를 확인하세요. "
                      "앱이 살아있는 상태로 인프라 삭제는 진행하지 않습니다.")
        return 1
    if cfg.features.web_search:
        # helm/terraform 관리 밖의 프로비저닝 리소스 — 멱등 teardown
        from . import websearch
        websearch.teardown(cfg.env, REPO_ROOT)
    if cfg.features.pricing_lambda:
        from . import pricinglambda
        admin_arn = ""
        if env_dir:
            admin_arn = eks_apply.tf_outputs(env_dir).get("admin_api_role_arn", "")
        pricinglambda.teardown(cfg, admin_role_arn=admin_arn)
    if not infra:
        if uninstalled:
            console.print("[green]helm uninstall 완료 — 인프라는 남아 있습니다 "
                          "(--infra 로 함께 삭제 가능)")
        return 0
    try:
        eks_apply._run(["terraform", "destroy", "-auto-approve", "-input=false"],
                       cwd=env_dir)
    except SystemExit:
        raise SystemExit("terraform destroy 가 중간에 실패했습니다 — 일부 리소스가 "
                         "남았을 수 있습니다. 잔여는 "
                         f"`terraform -chdir={env_dir} plan -destroy` 로 확인하십시오")
    console.print("[green]teardown 완료 — helm release 와 terraform env 인프라가 삭제됐습니다")
    return 0


def cmd_bootstrap(args) -> int:
    """신규 설치 — install-guide §3 전체를 한 명령으로.

    흐름: (yaml 없으면 init 통합 — 배포 방식 선택) → 대화형 인프라 wizard
    (tfstate 이름/AZ/Cognito suffix·groups/bedrock ARNs/web-search, 답변은
    gateway.yaml 의 infra.* 에 영속화) → backend 별 실행. 각 단계는 멱등 —
    실패 시 고친 뒤 bootstrap 을 다시 실행하면 이어간다."""
    from . import bootstrap

    # ① 초기 배포 상태 — gateway.yaml 이 없으면 배포 방식 선택부터 (init 통합)
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        if not sys.stdin.isatty() or getattr(args, "yes", False):
            console.print(f"[red]{cfg_path} 가 없습니다 — "
                          "./deploy init 또는 --config 를 지정하십시오")
            return 1
        console.print(Panel("초기 배포 상태입니다 — 배포 방식을 선택해 주세요"))
        if _write_config(cfg_path, _collect_doc()):
            return 1
    try:
        cfg = _load_config(args)
    except schema.SchemaError as exc:
        console.print(f"[red]gateway.yaml 오류:\n{exc}")
        return 1

    if cfg.deploy.target == "compose":
        console.print("[cyan]compose 는 신규=업데이트 동일 — apply 로 진행합니다")
        args.build = getattr(args, "build", False)   # bootstrap 파서엔 --build 없음
        return cmd_apply(args)

    if cfg.deploy.target not in ("ecs", "eks"):
        console.print(f"[red]deploy.target={cfg.deploy.target} 의 bootstrap 은 구현 전입니다.")
        return 1
    eks = cfg.deploy.target == "eks"

    # --plan 은 미리보기 — wizard(yaml 영속화 포함)와 AWS 변경 모두 생략
    interactive = (sys.stdin.isatty()
                   and not getattr(args, "yes", False) and not args.plan)

    def _tfstate_with_retry(bucket: str, table: str) -> tuple[str, str]:
        while True:
            try:
                return bootstrap.ensure_tfstate(
                    cfg, REPO_ROOT, eks=eks, bucket=bucket, table=table)
            except SystemExit as exc:
                if not sys.stdin.isatty() or getattr(args, "yes", False):
                    raise
                console.print(f"[red]tfstate 생성 실패 — {exc}")
                questionary = _questionary()
                bucket = _unwrap(questionary.text(
                    "TFSTATE_BUCKET (다른 이름으로 재시도)",
                    default=bucket).ask())

    if not eks:
        if args.plan:
            console.print("[cyan]ecs bootstrap 계획: ① tfstate bucket/table 보장 "
                          "→ ② apply(terraform 이 전부 생성 + 이미지 push)")
            return 0
        bucket = table = ""
        if interactive:
            ans = bootstrap.wizard_infra(cfg, eks=False)
            if ans is None:
                return 130
            bootstrap.persist_bootstrap_answers(cfg_path, ans)
            console.print(f"[green]설정 저장: {cfg_path} (infra.* 기록)")
            cfg = schema.load(cfg_path)
            bucket, table = ans["tfstate"]
        console.print(Panel(
            f"신규 설치 (ecs — {cfg.env}):\n"
            "① tfstate bucket/lock table 보장 (bootstrap-tfstate.sh)\n"
            "② apply — terraform 이 VPC/Aurora/ElastiCache/Cognito/Secrets/ECR "
            "생성 + 이미지 build/push + services",
            title=f"bootstrap — {cfg.env}", expand=False))
        if not _confirm_apply(args):
            return 130
        _tfstate_with_retry(bucket, table)
        return cmd_apply(args)

    # ── eks ──
    from .render import eks as eks_render
    from . import eks_apply, doctor as doc
    context = getattr(args, "context", "") or ""
    out = GEN_ROOT / cfg.env / "eks"

    # 로컬 파일만 생성 — 신규 env 는 tfvars 검토를 위해 여기서 멈춘다 (AWS 변경 전)
    env_target = bootstrap.scaffold_eks_env(cfg, REPO_ROOT, cfg.aws.region)

    # wizard 는 render 전에 — infra.* 답변이 managed overlay tfvars 로 들어간다
    bucket = table = ""
    if interactive:
        ans = bootstrap.wizard_infra(cfg, eks=True)
        if ans is None:
            return 130
        bootstrap.persist_bootstrap_answers(cfg_path, ans)
        console.print(f"[green]설정 저장: {cfg_path} (infra.* 기록)")
        cfg = schema.load(cfg_path)
        bucket, table = ans["tfstate"]

    res = eks_render.render(cfg, out)
    for n in res.get("notes", []):
        console.print(f"  [yellow]ⓘ {n}")
    if args.plan:
        console.print("[cyan]eks bootstrap 계획: ① tfstate 보장 → ② env "
                      f"terraform apply ({env_target}) → ③ SM 시크릿 시드 → "
                      "④ ECR 이미지 build/push → ⑤ helm apply → "
                      "⑥ Cognito admin 생성")
        return 0
    console.print(Panel(
        f"신규 설치 (eks — {cfg.env}):\n"
        "① tfstate bucket/lock table 보장\n"
        f"② terraform env apply — {env_target}\n"
        "   (EKS·Aurora·ElastiCache·Cognito·Secrets·IRSA — 수십 분)\n"
        "③ Secrets Manager 시드 (/app 랜덤 + /redis 복사 + /db 검증)\n"
        "④ ECR repo 생성 + 이미지 build/push\n"
        "⑤ helm apply (cmd_apply 와 동일)\n"
        "⑥ Cognito 첫 admin 사용자 생성 (adminBootstrap.emails[0])",
        title=f"bootstrap — {cfg.env} (eks)", expand=False))
    if not _confirm_apply(args):
        return 130

    backend = _tfstate_with_retry(bucket, table)
    outs = bootstrap.env_apply(env_target, out, backend)
    for n in bootstrap.seed_secrets(cfg, REPO_ROOT, outs):
        console.print(f"  ⓘ {n}")
    bootstrap.push_images_eks(cfg, outs, REPO_ROOT, cfg.aws.region)
    try:
        result = eks_apply.apply(cfg, out, REPO_ROOT, context)
    except SystemExit as exc:
        console.print(f"[red]{exc}")
        return 1
    console.print(f"\n[green]helm upgrade 완료 — {result['release']} "
                  f"@ ns={result['namespace']}")
    # helm 까지 끝난 최신 output 으로 온보딩 (ensure_infra 가 추가했을 수 있음)
    for n in bootstrap.onboard_admin(
            cfg, REPO_ROOT, eks_apply.tf_outputs(env_target), cfg.aws.region):
        console.print(f"  ⓘ {n}")
    rep = doc.check_eks(cfg, GEN_ROOT / cfg.env, REPO_ROOT)
    console.print(doc.format_report(cfg, rep))
    console.print("\n[cyan]다음 단계 (수동, install-guide §3-8/§4):\n"
                  "  · 앱 경로 스모크: bash deployment/scripts/smoke-test.sh"
                  f" --env {cfg.env}\n"
                  "  · 모델 alias/라우팅 프로필 시드: admin UI → models "
                  "(§4 — 운영 데이터라 bootstrap 이 만들지 않음)")
    return 0 if rep.worst != "HIGH" else 1


def cmd_teardown(args) -> int:
    """배포 삭제 — 각 backend 가 소유하는 범위만 지운다 (apply 와 같은 경계)."""
    try:
        cfg = _load_config(args)
    except schema.SchemaError as exc:
        console.print(f"[red]gateway.yaml 오류:\n{exc}")
        return 1
    if cfg.deploy.target == "compose":
        return _teardown_compose(cfg, args)
    if cfg.deploy.target == "ecs":
        return _teardown_ecs(cfg, args)
    if cfg.deploy.target == "eks":
        return _teardown_eks(cfg, args)
    console.print(f"[red]deploy.target={cfg.deploy.target} 의 teardown 은 구현 전입니다.")
    return 1


def _detect_gen_dir(args) -> tuple[Path, str]:
    """캡처 대상 산출물 디렉토리를 찾는다 — 명시 > 기존 config 의 env > gen/ 탐색.

    gen/ 밖 디렉토리(수작업 compose 프로젝트 등)도 --gen-dir 로 가리키면 된다.
    """
    if getattr(args, "gen_dir", ""):
        return Path(args.gen_dir), ""
    cfg_path = Path(args.config)
    if cfg_path.exists():
        try:
            env = schema.load(cfg_path).env
            return GEN_ROOT / env, env
        except schema.SchemaError:
            pass
    found = sorted([d for d in GEN_ROOT.iterdir() if d.is_dir()] if GEN_ROOT.exists() else [])
    if len(found) == 1:
        return found[0], found[0].name
    if found and sys.stdin.isatty():
        try:
            questionary = _questionary()
        except Exception:
            questionary = None  # 선택 UI 없으면 아래 힌트로 빠진다
        if questionary:
            pick = _unwrap(questionary.select(
                "캡처할 배포를 선택하세요",
                choices=[d.name for d in found]).ask())
            return GEN_ROOT / pick, pick
    from .capture import CaptureError
    hint = "\n".join(f"    --gen-dir {d}" for d in found) or "    (gen/ 에 산출물 없음)"
    raise CaptureError(
        "캡처 대상이 여러 개이거나 없습니다 — 대상을 지정하세요:\n" + hint +
        "\n  EKS 배포: --target eks [--namespace --release --context]"
        "\n  이 도구로 만들지 않은 배포는 --gen-dir <compose 파일이 있는 디렉토리>")


def _captured_to_yaml(path: Path, doc: dict, notes: list[str]) -> None:
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    if notes:
        body += "\n# --- capture notes ---\n" + "".join(f"# {n}\n" for n in notes)
    path.write_text(
        "# captured by deploy doctor --capture — 검토 후 gateway.yaml 로 채택하세요.\n"
        "# 빈 값과 notes 는 캡처가 못 읽은 부분입니다.\n" + body)
    path.chmod(0o600)


def _set_path(doc: dict, dotted: str, value) -> None:
    cur = doc
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def cmd_capture(args) -> int:
    """기존 배포 → gateway.yaml 후보 역생성 + 기존 config 와 diff."""
    from . import capture

    try:
        cfg_path = Path(args.config)
        existing: dict = {}
        if cfg_path.exists():
            try:
                existing = yaml.safe_load(cfg_path.read_text()) or {}
            except (yaml.YAMLError, UnicodeDecodeError) as exc:
                console.print(f"[yellow]{cfg_path} 읽기 실패 — 캡처본과 비교는 건너뜁니다: {exc}")
        existing_target = (existing.get("deploy") or {}).get("target", "")

        # eks/ecs 는 gen 디렉토리가 필요 없다 — helm·kubectl·terraform 이 정보원
        if args.target in ("eks", "ecs"):
            gen_dir, target = Path(""), args.target
        else:
            gen_dir, _ = _detect_gen_dir(args)
            target = args.target or existing_target or (
                "compose" if (gen_dir / "docker-compose.yml").exists()
                else "ecs" if (gen_dir / "ecs").is_dir() else "")
        if target == "compose":
            doc, notes = capture.capture_compose(gen_dir)
        elif target == "ecs":
            region = args.region or (existing.get("aws") or {}).get("region", "ap-northeast-2")
            env_dir = Path(args.env_dir) if getattr(args, "env_dir", "") else \
                REPO_ROOT / "deployment/terraform/environments/gateway-ecs"
            doc, notes = capture.capture_ecs(env_dir, region)
        elif target == "eks":
            env_dir = Path(args.env_dir) if getattr(args, "env_dir", "") else None
            doc, notes = capture.capture_eks(
                namespace=args.namespace or "llm-gateway",
                release=args.release or "llm-gateway",
                context=args.context or "",
                env_dir=env_dir)
        else:
            console.print(f"[red]캡처 대상을 판별 못 했습니다 — --target compose|ecs 또는 --gen-dir 지정")
            return 1

        # 캡처 결과가 스키마를 통과하는지 — 안 되는 필드는 사람이 채워야 함
        try:
            schema.from_dict(doc)
            console.print("[green]캡처 결과 스키마 유효")
        except schema.SchemaError as exc:
            console.print(f"[yellow]캡처 결과에 미완성 필드가 있습니다:\n{exc}")

        out_path = Path(args.out) if args.out else \
            cfg_path.parent / f"gateway.captured-{doc.get('env', 'env')}.yaml"
        if out_path.exists():
            # 생성물이라 덮어도 안전 — 대신 이전본을 백업해 둔다.
            # 캡처본엔 계정 ID·호스트 등이 들어 있어 원본(0600)과 같은 권한을 유지한다.
            backup = out_path.with_suffix(out_path.suffix + ".bak")
            backup.write_text(out_path.read_text())
            backup.chmod(0o600)
            console.print(f"[yellow]기존 캡처본을 {backup.name} 으로 백업하고 덮어씁니다")
        _captured_to_yaml(out_path, doc, notes)
        console.print(f"[green]캡처됨: {out_path}")
        for n in notes:
            console.print(f"  [yellow]ⓘ {n}")

        rel = out_path.relative_to(REPO_ROOT) if out_path.is_relative_to(REPO_ROOT) else out_path
        console.print(Panel(
            f"[bold]다음 단계[/bold]\n"
            f"  1. 검토 — 위 notes 의 빈칸/주의사항을 채우세요\n"
            f"       vi {rel}\n"
            f"  2. 채택 — gateway.yaml 로 승격하거나 --config 로 바로 사용\n"
            f"       cp {rel} deployment/gateway.yaml\n"
            f"  3. 검증 + 산출물 생성\n"
            f"       ./deploy render --config {rel}\n"
            f"  4. 변경 미리보기 (아무것도 안 바뀜)\n"
            f"       ./deploy apply --plan --config {rel}\n"
            f"  5. 적용 + 상태 확인\n"
            f"       ./deploy apply --config {rel}\n"
            f"       ./deploy doctor --config {rel}",
            title="capture 완료", expand=False))

        # 기존 config 와 diff → interactive 흡수
        if existing and cfg_path.exists():
            # 스키마로 정규화해 비교 — 미기재 키 vs 기본값 캡처는 차이가 아니다
            try:
                from dataclasses import asdict
                diffs = capture.diff_docs(
                    asdict(schema.from_dict(existing)), asdict(schema.from_dict(doc)))
            except schema.SchemaError:
                diffs = capture.diff_docs(existing, doc)
            if not diffs:
                console.print("\n[green]기존 gateway.yaml 과 일치 — 드리프트 없음")
                return 0
            table = Table(title="기존 gateway.yaml ←→ 배포 상태 차이")
            table.add_column("키"); table.add_column("yaml"); table.add_column("캡처(실제)")
            for k, dv, cv in diffs:
                table.add_row(k, str(dv), str(cv))
            console.print(table)
            if args.interactive:
                questionary = _questionary()
                from questionary import Choice
                adopted = 0
                for k, dv, cv in diffs:
                    c = _unwrap(questionary.select(f"{k}: yaml={dv} / 실제={cv}", choices=[
                        Choice("absorb — 실제 값을 yaml 에 채택", "absorb"),
                        Choice("keep — yaml 유지 (나중에 apply 로 되돌림)", "keep"),
                        Choice("skip — 지금은 건너뜀", "skip"),
                    ]).ask())
                    if c == "absorb" and cv is not None:
                        _set_path(existing, k, cv)
                        adopted += 1
                if adopted:
                    # 흡수 결과가 스키마를 통과해야 저장 — 깨진 yaml 을 쓰지 않는다
                    try:
                        schema.from_dict(existing)
                    except schema.SchemaError as exc:
                        console.print(f"[red]흡수 결과가 유효하지 않아 저장하지 않았습니다:\n{exc}")
                        return 1
                    cfg_path.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True))
                    console.print(f"[green]{adopted} 개 필드를 {cfg_path} 에 흡수했습니다 "
                                  f"(주석은 보존되지 않습니다 — 원본은 git 에서 확인)")
            else:
                console.print("[yellow]--interactive 로 각 항목의 채택/유지를 선택할 수 있습니다")
        return 0
    except capture.CaptureError as exc:
        console.print(f"[red]{exc}")
        return 1


def cmd_doctor(args) -> int:
    if getattr(args, "capture", False):
        return cmd_capture(args)
    from . import doctor as doc
    try:
        cfg = _load_config(args)
    except schema.SchemaError as exc:
        console.print(f"[red]gateway.yaml 오류:\n{exc}")
        return 1
    if cfg.deploy.target == "compose":
        rep = doc.check_compose(cfg, GEN_ROOT / cfg.env)
        console.print(doc.format_report(cfg, rep))
        return 0 if rep.worst != "HIGH" else 1
    if cfg.deploy.target == "ecs":
        rep = doc.check_ecs(cfg, GEN_ROOT / cfg.env, REPO_ROOT)
        console.print(doc.format_report(cfg, rep))
        return 0 if rep.worst != "HIGH" else 1
    if cfg.deploy.target == "eks":
        rep = doc.check_eks(cfg, GEN_ROOT / cfg.env, REPO_ROOT,
                            context=getattr(args, "context", ""))
        console.print(doc.format_report(cfg, rep))
        return 0 if rep.worst != "HIGH" else 1
    console.print("[yellow]이 backend 의 doctor 는 아직 구현 전입니다.")
    return 0


def _tf_env_dir(cfg) -> Path:
    if cfg.deploy.target == "ecs":
        return REPO_ROOT / "deployment" / "terraform" / "environments" / "gateway-ecs"
    rel = cfg.deploy.tf_env_dir or f"deployment/terraform/environments/llm-gateway-{cfg.env}"
    return REPO_ROOT / rel


def _tf_outputs_soft(env_dir: Path) -> dict:
    """terraform output best-effort — init/state 없으면 {} (값 추출은 선택 사항)."""
    if not env_dir.exists():
        return {}
    try:
        from .capture import _tf_outputs
        return _tf_outputs(env_dir)
    except Exception:
        return {}


def _eks_ingress_hosts(cfg) -> dict:
    """env overlay values 의 ingress host — eks 의 실제 서비스 주소."""
    from .render import eks as eks_render
    p = eks_render.env_values_file(cfg)
    if not p:
        return {}
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except Exception:
        return {}
    ing = data.get("ingress") or {}
    return {k: (ing.get(k) or {}).get("host", "")
            for k in ("gateway", "adminApi", "adminUi")}


def _public_ip() -> str:
    import subprocess
    try:
        r = subprocess.run(["curl", "-s", "--max-time", "4",
                            "https://checkip.amazonaws.com"],
                           capture_output=True, text=True, timeout=6)
        ip = r.stdout.strip()
        return ip if r.returncode == 0 and "." in ip else ""
    except Exception:
        return ""


def cmd_client_values(args) -> int:
    """직원에게 전달할 env 4줄 출력 — gateway.yaml + 라이브 산출물에서 추출."""
    cfg = _load_config(args)
    target = cfg.deploy.target
    notes: list[str] = []

    gw = adm = ""
    if target == "eks":
        hosts = _eks_ingress_hosts(cfg)
        gw_h = hosts.get("gateway") or (f"gateway.{cfg.domain.name}" if cfg.domain.name else "")
        adm_h = hosts.get("adminApi") or (f"admin-api.{cfg.domain.name}" if cfg.domain.name else "")
        gw = f"https://{gw_h}" if gw_h else ""
        adm = f"https://{adm_h}" if adm_h else ""
        if not gw_h:
            notes.append("eks ingress host 를 못 찾았습니다 — values-eks-*.local.yaml 의 "
                         "ingress.gateway.host 또는 `kubectl -n {ns} get ingress`로 확인")
    elif cfg.domain.name:
        gw = f"https://gateway.{cfg.domain.name}"
        adm = f"https://admin-api.{cfg.domain.name}"
    else:
        outs = _tf_outputs_soft(_tf_env_dir(cfg))
        dns = str(outs.get("alb_dns_name") or "")
        if target == "ecs" and dns:
            gw, adm = f"http://{dns}:8000", f"http://{dns}:8080"
        elif target == "compose":
            ip = _public_ip()
            if ip:
                gw, adm = f"http://{ip}:8000", f"http://{ip}:8080"
                notes.append("compose+도메인 없음: 공인 IP 자동 탐지 — 배포 EC2 외 머신에서 "
                             "실행했다면 출력 IP 대신 EC2 퍼블릭 IP 로 교체하세요")

    issuer, cid = cfg.oidc.issuer_url, cfg.oidc.client_id
    if not (issuer and cid) and target in ("ecs", "eks"):
        outs = _tf_outputs_soft(_tf_env_dir(cfg))
        issuer = issuer or str(outs.get("cognito_issuer_url") or "")
        cid = cid or str(outs.get("cognito_client_id") or "")
    if not (issuer and cid):
        notes.append("OIDC 값이 비어 있습니다 — gateway.yaml oidc: 가 비활성(dev-login)이거나 "
                     "terraform 미적용 상태입니다. 채운 뒤 다시 실행하세요")

    if cfg.domain.mode == "none":
        notes.append("domain.mode=none → http 주소: Cowork(Claude Desktop)는 https 만 "
                     "받으므로 이 설정으로는 연결 불가 (Claude Code·Codex는 됨)")
    if not cfg.network.allowed_cidrs:
        notes.append("network.allowed_cidrs 가 비어 있습니다 — 게이트웨이가 인터넷 전체에 "
                     "열려 있을 수 있으니 직원 IP 대역을 넣는 것을 권장")

    def _v(x):  # 채워진 값 또는 placeholder
        return x if x else "<TODO>"

    console.print(Panel.fit(
        "직원에게 전달할 블록 — docs/NDS/client-setup.md §0 의 export 4줄\n\n"
        f'export OIDC_ISSUER_URL="{_v(issuer)}"\n'
        f'export OIDC_CLIENT_ID="{_v(cid)}"\n'
        f'export ADMIN_API_URL="{_v(adm)}"\n'
        f'export ANTHROPIC_BASE_URL="{_v(gw)}"',
        title=f"client-values ({cfg.env} / {target})"))
    for n in notes:
        console.print(f"  [yellow]! {n}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="deploy", description="gateway.yaml 기반 LLM Gateway 배포 도구")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="[최초 1회] 대화형으로 gateway.yaml 생성")
    sp.add_argument("--out", default="")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("configure",
                        help="[일상 경로] 설정 변경 마법사 — 항목별 확인 → 저장 → 배포까지")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.add_argument("--no-apply", action="store_true",
                    help="설정만 저장하고 배포는 안 함")
    sp.add_argument("--context", default="",
                    help="eks: apply 시 쓸 kubeconfig context")
    sp.set_defaults(fn=cmd_configure)

    sp = sub.add_parser("validate", help="[고급] yaml 검증 — configure/apply 도 시작 시 검증하므로 생략 가능")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("render", help="[고급] 산출물만 생성 — apply 가 내부에서 render 하므로 생략 가능")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.set_defaults(fn=cmd_render)

    sp = sub.add_parser("apply", help="[파일 편집/CI 경로] yaml 그대로 배포 — render 내장 + 확인 + doctor")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.add_argument("--build", action="store_true", help="이미지를 강제로 다시 빌드 (compose)")
    sp.add_argument("--plan", action="store_true",
                    help="변경 계획만 보여주고 적용하지 않음")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="적용 전 확인 프롬프트 생략 (CI/자동화)")
    sp.add_argument("--context", default="",
                    help="eks: kubeconfig context (미지정 시 현재 context — "
                         "여러 클러스터가 있으면 명시 권장)")
    sp.set_defaults(fn=cmd_apply)

    sp = sub.add_parser("bootstrap",
                        help="[최초 1회] 신규 설치 — 인프라·시크릿·이미지·"
                             "Cognito 포함 전체 순서 (이후 업데이트는 apply)")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.add_argument("--plan", action="store_true",
                    help="단계 계획만 보여주고 실행하지 않음")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="확인 프롬프트 생략 (CI/자동화)")
    sp.add_argument("--context", default="",
                    help="eks: kubeconfig context (미지정 시 현재 context)")
    sp.set_defaults(fn=cmd_bootstrap)

    sp = sub.add_parser("doctor", help="[상태 확인] 배포 상태·드리프트 점검 (--capture: 기존 배포 → yaml)")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.add_argument("--capture", action="store_true",
                    help="기존 배포 상태를 읽어 gateway.yaml 후보를 역생성")
    sp.add_argument("--target", choices=["compose", "ecs", "eks"], default="",
                    help="capture 대상 backend (미지정 시 자동 판별)")
    sp.add_argument("--namespace", default="", help="eks capture 용 k8s namespace")
    sp.add_argument("--release", default="", help="eks capture 용 helm release 이름")
    sp.add_argument("--context", default="", help="eks capture 용 kubeconfig context")
    sp.add_argument("--gen-dir", default="", help="capture 할 산출물 디렉토리")
    sp.add_argument("--env-dir", default="",
                    help="capture 할 terraform 환경 디렉토리 (ecs, 기본 gateway-ecs)")
    sp.add_argument("--region", default="", help="capture 용 AWS 리전 (ecs)")
    sp.add_argument("--out", default="", help="capture 결과 쓸 경로")
    sp.add_argument("--interactive", action="store_true",
                    help="기존 config 와 다른 항목을 absorb/keep/skip 선택")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("client-values",
                        help="[직원 온보딩] 직원에게 전달할 env 4줄 출력 "
                             "(OIDC + 게이트웨이/admin-api URL)")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.set_defaults(fn=cmd_client_values)

    sp = sub.add_parser("teardown",
                        help="[위험] 배포 삭제 — backend 별 소유 범위만 지움 (확인: 환경 이름 입력)")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.add_argument("-y", "--yes", action="store_true",
                    help="확인 생략 (CI/스크립트용 — 주의)")
    sp.add_argument("--purge", action="store_true",
                    help="compose: 볼륨까지 삭제 — postgres DB 데이터 영구 삭제")
    sp.add_argument("--infra", action="store_true",
                    help="eks: terraform env 의 인프라까지 삭제 (EKS·Aurora·VPC 등 전부)")
    sp.add_argument("--context", default="", help="eks backend: kubeconfig context")
    sp.set_defaults(fn=cmd_teardown)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (Cancelled, KeyboardInterrupt):
        console.print("\n[yellow]취소됨")
        return 130
    except Exception as exc:
        # 사용자 대면용 오류는 traceback 없이 출력 — 나머지는 버그라 노출
        from .capture import CaptureError
        if isinstance(exc, (CaptureError, schema.SchemaError)):
            console.print(f"[red]{exc}")
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
