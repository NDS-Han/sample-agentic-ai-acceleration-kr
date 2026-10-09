# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""deploy CLI — init / validate / render / doctor.

  deploy init      대화형으로 gateway.yaml 생성
  deploy validate  gateway.yaml 스키마·조합 검증
  deploy render    산출물 생성 (compose: gen/<env>/ 에 compose+.env+Caddyfile)
  deploy doctor    배포 상태·드리프트 점검
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


def _unwrap(v):
    if v is None:
        raise Cancelled
    return v


def cmd_init(args) -> int:
    import questionary
    from questionary import Choice

    out_path = Path(args.out) if args.out else DEFAULT_CONFIG
    if out_path.exists() and not args.force:
        console.print(f"[yellow]{out_path} 가 이미 있습니다 — --force 로 덮어씁니다.")
        return 1

    console.print(Panel("LLM Gateway 배포 설정 마법사 — 답하면 gateway.yaml 을 만듭니다"))

    env = _unwrap(questionary.text("환경 이름 (소문자·숫자·하이픈, 예: acme-small)").ask())
    region = _unwrap(questionary.text("AWS 리전", default="ap-northeast-2").ask())

    target = _unwrap(questionary.select(
        "배포 대상",
        choices=[
            Choice("compose — 단일 호스트 Docker (평가·최소비용)", "compose"),
            Choice("ecs — ECS Fargate (소규모~표준 권장)", "ecs"),
            Choice("eks — EKS Fargate (대규모·기존 프로덕션)", "eks"),
        ]).ask())

    tier_choices = [
        Choice(f"{t.name}: {t.label}  [{t.users}, {t.monthly_cost_usd}]", t.name)
        for t in tiers.TIERS.values() if t.backend == target
    ]
    size_tier = _unwrap(questionary.select("규모 티어", choices=tier_choices).ask())

    domain_mode = _unwrap(questionary.select(
        "HTTPS 도메인",
        choices=[
            Choice("none — 도메인 없이 시작 (HTTP, Cowork 불가)", "none"),
            Choice("route53-acm — 도메인 확정 (자동 TLS)", "route53-acm"),
            Choice("cloudfront-temp — 임시 https (EKS 전용)", "cloudfront-temp"),
        ]).ask())
    domain_name, zone_id = "", ""
    if domain_mode == "route53-acm":
        domain_name = _unwrap(questionary.text("베이스 도메인 (예: example.com → gateway./admin./admin-api. 자동)").ask())
        if target in ("ecs", "eks"):
            zone_id = _unwrap(questionary.text("Route53 hosted zone ID (예: Z0123456ABCD)").ask())

    notif = _unwrap(questionary.select(
        "알림 provider",
        choices=[Choice("mock — 발송 안 함(기본)", "mock"),
                 Choice("ses — Amazon SES", "ses"),
                 Choice("smtp — 외부 SMTP", "smtp")]).ask())
    notif_extra = {}
    if notif == "ses":
        notif_extra["ses_from"] = _unwrap(questionary.text("SES 발신 주소").ask())
    elif notif == "smtp":
        notif_extra["smtp_host"] = _unwrap(questionary.text("SMTP 호스트").ask())
        notif_extra["smtp_from"] = _unwrap(questionary.text("발신 주소").ask())

    features = _unwrap(questionary.checkbox(
        "추가 기능 (기본 전부 off)",
        choices=[
            Choice("web_search — AgentCore 웹검색 (us-east-1, 별도 프로비저닝)", "web_search"),
            Choice("body_logging — 요청 본문 S3 로깅", "body_logging"),
            Choice("bi_insight — BI 어시스턴트 (AgentCore Runtime)", "bi_insight"),
            Choice("pricing_lambda — LiteLLM 단가 조회 Lambda", "pricing_lambda"),
            Choice("observability — OTel/Prometheus/Grafana (compose)", "observability"),
        ]).ask())

    oidc = {}
    if _unwrap(questionary.confirm("OIDC 로그인(Cognito 등)을 설정합니까?", default=True).ask()):
        oidc["issuer_url"] = _unwrap(questionary.text("OIDC issuer URL").ask())
        oidc["client_id"] = _unwrap(questionary.text("OIDC client id (admin-ui SSO)").ask())
        oidc["authorize_url"] = _unwrap(questionary.text("authorize URL").ask())
        oidc["token_url"] = _unwrap(questionary.text("token URL").ask())

    images = {}
    if target in ("ecs", "eks"):
        images["registry"] = _unwrap(questionary.text(
            "외부 이미지 registry (비우면 ECR repo 자동 생성 — ecs)", default="").ask())
        images["tag"] = _unwrap(questionary.text("이미지 태그 (명시적 핀 필수)").ask())

    deploy_extra = {}
    if target in ("ecs", "eks"):
        deploy_extra["tfstate_bucket"] = _unwrap(questionary.text(
            "Terraform state S3 bucket (비우면 llm-gateway-tfstate-<account>)", default="").ask())
        deploy_extra["tfstate_table"] = _unwrap(questionary.text(
            "Terraform lock DynamoDB table (없으면 비움)", default="").ask())

    allowed = _unwrap(questionary.text(
        "접근 허용 CIDR (쉼표, 비우면 전체 허용 — 경고 대상)", default="").ask())

    doc = {
        "version": 1,
        "env": env,
        "aws": {"region": region},
        "deploy": {"target": target, "size_tier": size_tier, **deploy_extra},
        "network": {"mode": "public",
                    "allowed_cidrs": [c.strip() for c in allowed.split(",") if c.strip()]},
        "domain": {"mode": domain_mode, "name": domain_name, "zone_id": zone_id},
        "features": {
            "notifications": {"provider": notif, **notif_extra},
            "web_search": "web_search" in features,
            "body_logging": "body_logging" in features,
            "bi_insight": "bi_insight" in features,
            "pricing_lambda": "pricing_lambda" in features,
            "observability": "observability" in features,
        },
        "clients": {"models_profile": "global"},
    }
    if oidc:
        doc["oidc"] = oidc
    if images:
        doc["images"] = images

    # 작성 전 스키마 검증 — 못 쓰는 파일을 만들지 않는다
    try:
        cfg = schema.from_dict(doc)
    except schema.SchemaError as exc:
        console.print(f"[red]입력 조합이 유효하지 않습니다:\n{exc}")
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "# gateway.yaml — 이 배포의 source of truth.\n"
        "# 변경 → `deploy render` → 백엔드별 apply 절차. 수동 편집한 인프라는 doctor 가 감지합니다.\n"
        + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    console.print(f"[green]생성: {out_path}")
    for w in cfg.warnings():
        console.print(f"[yellow]⚠ {w}")
    console.print(f"다음: [bold]deploy render --config {out_path}")
    return 0


def cmd_validate(args) -> int:
    try:
        cfg = schema.load(Path(args.config))
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
    table.add_row("db", f"{t.db_mode} {t.db_spec}")
    table.add_row("cache", f"{t.cache_mode} {t.cache_spec}")
    table.add_row("ha", t.ha)
    table.add_row("monthly(est)", t.monthly_cost_usd)
    console.print(table)
    return 0


def cmd_render(args) -> int:
    try:
        cfg = schema.load(Path(args.config))
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

    console.print(f"[red]deploy.target={cfg.deploy.target} 렌더러는 아직 구현 전입니다 (compose, ecs 지원).")
    return 1


def cmd_apply(args) -> int:
    """render → 기동 → doctor 한 단계로 (backend 별 실행기)."""
    try:
        cfg = schema.load(Path(args.config))
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
    r = subprocess.run(argv)
    if r.returncode != 0:
        console.print("[red]compose up 실패 — 로그를 확인하십시오")
        return 1

    # 기동 직후 doctor — 완료된 것과 아직 준비 중인 것을 구분해 보고
    from . import doctor as doc
    rep = doc.check_compose(cfg, out)
    console.print(doc.format_report(cfg, rep))
    return 0 if rep.worst != "HIGH" else 1


def cmd_doctor(args) -> int:
    from . import doctor as doc
    try:
        cfg = schema.load(Path(args.config))
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
    console.print("[yellow]이 backend 의 doctor 는 아직 구현 전입니다.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="deploy", description="gateway.yaml 기반 LLM Gateway 배포 도구")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="대화형으로 gateway.yaml 생성")
    sp.add_argument("--out", default="")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("validate", help="gateway.yaml 검증")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("render", help="배포 산출물 생성")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.set_defaults(fn=cmd_render)

    sp = sub.add_parser("apply", help="render + 기동 + doctor (compose)")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.add_argument("--build", action="store_true", help="이미지를 강제로 다시 빌드")
    sp.set_defaults(fn=cmd_apply)

    sp = sub.add_parser("doctor", help="배포 상태·드리프트 점검")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.set_defaults(fn=cmd_doctor)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except Cancelled:
        console.print("\n[yellow]취소됨")
        return 130


if __name__ == "__main__":
    sys.exit(main())
