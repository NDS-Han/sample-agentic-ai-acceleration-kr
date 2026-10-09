# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""doctor — 배포 상태·드리프트 점검.

compose backend: 산출물 존재 → 서비스 기동/헬스 → .env 완결성 → 마이그레이션 head.
반환 항목은 (severity, message) — HIGH 는 즉시 조치, WARN 은 확인 권고."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .schema import GatewayConfig
from .render import common

MIGRATION_HEAD = "0040"
EXPECTED_SERVICES = (
    "postgres", "redis", "migration", "gateway-proxy", "admin-api",
    "admin-ui", "scheduler", "cost-recorder-worker",
    "notification-worker", "caddy",
)
REQUIRED_ENV = ("POSTGRES_PASSWORD", "VIRTUAL_KEY_ENCRYPTION_KEY",
                "NEXTAUTH_SECRET", "INTERNAL_API_TOKEN")


@dataclass
class Finding:
    level: str          # OK | WARN | HIGH
    check: str
    message: str


@dataclass
class DoctorReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, check: str, message: str) -> None:
        self.findings.append(Finding(level, check, message))

    @property
    def worst(self) -> str:
        for lv in ("HIGH", "WARN"):
            if any(f.level == lv for f in self.findings):
                return lv
        return "OK"


def _run(argv: list[str], cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def check_compose(cfg: GatewayConfig, out_dir: Path) -> DoctorReport:
    rep = DoctorReport()

    # 1. 산출물 존재
    for name in ("docker-compose.yml", ".env", "Caddyfile"):
        if not (out_dir / name).exists():
            rep.add("HIGH", "artifacts", f"{name} 없음 — `deploy render` 먼저 실행")
            return rep
    rep.add("OK", "artifacts", "render 산출물 존재")

    # 2. .env 완결성 — 비어 있는 필수 시크릿 탐지 (값 자체는 출력하지 않음)
    env = common.parse_env_file(out_dir / ".env")
    missing = [k for k in REQUIRED_ENV if not env.get(k)]
    if missing:
        rep.add("HIGH", "secrets", f".env 에 빈 필수 키: {', '.join(missing)}")
    else:
        rep.add("OK", "secrets", "필수 시크릿 존재")
    if env.get("VIRTUAL_KEY_ENCRYPTION_KEY", "").startswith("0" * 20):
        rep.add("HIGH", "secrets", "VIRTUAL_KEY_ENCRYPTION_KEY 가 placeholder(전부 0)")

    # 3. compose ps -a — 완료 후 종료된 run-once(migration) 도 본다
    compose_file = out_dir / "docker-compose.yml"
    compose_argv = ["docker", "compose", "--env-file", str(out_dir / ".env"), "-f", str(compose_file)]
    r = _run(compose_argv + ["ps", "-a", "--format", "json"])
    if r.returncode != 0:
        rep.add("WARN", "services", f"docker compose ps 실패: {r.stderr.strip()[:200]}")
        return rep
    states: dict[str, str] = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = row.get("Service") or row.get("Name", "")
        health = row.get("Health") or ""
        state = row.get("State", "")
        states[name] = health or state

    if not states:
        rep.add("WARN", "services", "기동 중인 서비스가 없습니다 — `docker compose up -d` 필요")
        return rep
    for svc in EXPECTED_SERVICES:
        state = states.get(svc)
        if state is None:
            rep.add("HIGH", "services", f"{svc} 컨테이너 없음")
        elif svc == "migration" and "exited" in state:
            rep.add("OK", "services", f"migration 완료 ({state})")
        elif state in ("healthy", "running"):
            rep.add("OK", "services", f"{svc} {state}")
        elif "exited" in state:
            rep.add("HIGH", "services", f"{svc} 종료됨 ({state})")
        else:
            rep.add("WARN", "services", f"{svc} 상태: {state}")

    # 4. 마이그레이션 head — DB 에서 직접 확인
    r = _run(compose_argv + ["exec", "-T",
              "postgres", "psql", "-U", "gateway", "-d", "gateway",
              "-tAc", "SELECT version_num FROM alembic_version"])
    if r.returncode == 0:
        head = r.stdout.strip()
        if head == MIGRATION_HEAD:
            rep.add("OK", "migration", f"alembic head = {head}")
        elif head:
            rep.add("HIGH", "migration", f"alembic head = {head} (기대 {MIGRATION_HEAD})")
        else:
            rep.add("WARN", "migration", "alembic_version 행 없음 — migration 미실행?")
    else:
        rep.add("WARN", "migration", f"psql 실패: {r.stderr.strip()[:120]}")

    # 5. 드리프트 — gateway.yaml 대비 .env 결정적 키
    from .render.compose import desired_env
    desired = desired_env(cfg)
    diffs = [k for k, v in desired.items() if env.get(k) not in (None, "", v)]
    if diffs:
        rep.add("WARN", "drift", f".env 가 gateway.yaml 과 다른 키: {', '.join(diffs)} (수동 편집?)")
    else:
        rep.add("OK", "drift", ".env 가 gateway.yaml 과 일치")

    # 6. Caddyfile 문법 — 컨테이너가 살아 있으면 내부 검증
    if states.get("caddy") in ("healthy", "running", ""):
        r = _run(compose_argv + ["exec", "-T",
                  "caddy", "caddy", "validate", "--config", "/etc/caddy/Caddyfile"])
        rep.add("OK" if r.returncode == 0 else "WARN", "caddy",
                "Caddyfile 유효" if r.returncode == 0 else f"Caddyfile 오류: {r.stderr.strip()[:120]}")

    return rep


# ==============================================================================
# ecs backend doctor
#   산출물 → AWS 자격 → terraform plan(드리프트) → 서비스 running/desired →
#   migration task exit → 엔드포인트 응답
# ==============================================================================

ECS_ENV_DIR = Path("deployment/terraform/environments/gateway-ecs")
ECS_SERVICES = ("gateway-proxy", "admin-api", "admin-ui", "scheduler",
                "cost-recorder-worker", "notification-worker")


def _tf_outputs(env_dir: Path) -> dict:
    r = _run(["terraform", "output", "-json"], cwd=env_dir, timeout=120)
    if r.returncode != 0:
        return {}
    try:
        return {k: v.get("value") for k, v in json.loads(r.stdout).items()}
    except (json.JSONDecodeError, AttributeError):
        return {}


def _aws(argv: list[str], region: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return _run(["aws", *argv, "--region", region, "--output", "json"], timeout=timeout)


def check_ecs(cfg: GatewayConfig, gen_dir: Path, repo_root: Path) -> DoctorReport:
    rep = DoctorReport()
    out_dir = gen_dir / "ecs"
    env_dir = repo_root / ECS_ENV_DIR
    var_file = out_dir / "terraform.tfvars"

    # 1. 산출물
    for name in ("terraform.tfvars", "backend.hcl"):
        if not (out_dir / name).exists():
            rep.add("HIGH", "artifacts", f"{name} 없음 — `deploy render` 먼저 실행")
            return rep
    rep.add("OK", "artifacts", "render 산출물 존재")

    # 2. AWS 자격
    r = _aws(["sts", "get-caller-identity"], cfg.aws.region)
    if r.returncode != 0:
        rep.add("HIGH", "aws", f"AWS 자격증명 없음/만료: {r.stderr.strip()[:150]}")
        return rep
    rep.add("OK", "aws", "AWS 자격증명 유효")

    # 3. terraform plan — tfvars·라이브 인프라 드리프트 통합 탐지.
    #    refresh 를 켜서 콘솔/CLI 수동변경까지 잡는다 (수 초~수십 초 소요).
    r = _run(["terraform", "plan", "-detailed-exitcode", "-input=false",
              f"-var-file={var_file}"],
             cwd=env_dir, timeout=300)
    if r.returncode == 0:
        rep.add("OK", "drift", "terraform plan no-op — gateway.yaml 과 라이브 인프라 일치")
    elif r.returncode == 2:
        # diff 가 있으면 요약만 — 전체 plan 출력은 너무 김
        changed = [ln for ln in r.stdout.splitlines()
                   if ln.startswith(("  # ", "Plan:"))][-5:]
        rep.add("WARN", "drift", "인프라 드리프트 감지 — " + " / ".join(changed)[:200])
    else:
        rep.add("WARN", "drift",
                f"terraform plan 실행 불가(init 필요?): {r.stderr.strip()[:150]}")

    # 4. 서비스 상태
    outputs = _tf_outputs(env_dir)
    cluster = outputs.get("ecs_cluster_name")
    if not cluster:
        rep.add("HIGH", "services", "terraform output 에 클러스터 없음 — apply 됐는지 확인")
        return rep
    names = list((outputs.get("service_names") or {}).values()) or list(ECS_SERVICES)
    r = _aws(["ecs", "describe-services", "--cluster", cluster, "--services", *names],
             cfg.aws.region)
    if r.returncode != 0:
        rep.add("WARN", "services", f"describe-services 실패: {r.stderr.strip()[:150]}")
    else:
        try:
            svcs = json.loads(r.stdout).get("services", [])
        except json.JSONDecodeError:
            svcs = []
        by_name = {s["serviceName"]: s for s in svcs}
        for name in ECS_SERVICES:
            s = by_name.get(name)
            if s is None:
                rep.add("HIGH", "services", f"{name} 서비스 없음")
                continue
            running, desired = s.get("runningCount", 0), s.get("desiredCount", 0)
            deps = s.get("deployments") or []
            rolling = len([d for d in deps if d.get("rolloutState") != "COMPLETED"])
            if running >= desired and desired > 0 and rolling == 0:
                rep.add("OK", "services", f"{name} {running}/{desired} running")
            elif rolling:
                rep.add("WARN", "services", f"{name} 배포 진행 중 ({running}/{desired})")
            else:
                rep.add("HIGH", "services", f"{name} {running}/{desired} running")

    # 5. migration task — taskArns 는 시간순을 보장하지 않으므로 stoppedAt 이
    #    가장 최근인 STOPPED task 를 찾아 exitCode 를 본다
    fam = (outputs.get("migration_task_definition_arn") or "").split("/")[-1]
    if fam:
        r = _aws(["ecs", "list-tasks", "--cluster", cluster, "--family", fam,
                  "--desired-status", "STOPPED"], cfg.aws.region)
        try:
            arns = json.loads(r.stdout).get("taskArns", [])
        except json.JSONDecodeError:
            arns = []
        if not arns:
            rep.add("HIGH", "migration", "migration task 실행 기록 없음")
        else:
            r = _aws(["ecs", "describe-tasks", "--cluster", cluster,
                      "--tasks", *arns[:100]], cfg.aws.region)
            try:
                tasks = json.loads(r.stdout).get("tasks", [])
                task = max(tasks, key=lambda t: t.get("stoppedAt", ""))
                code = (task.get("containers") or [{}])[0].get("exitCode")
            except (json.JSONDecodeError, ValueError, IndexError, KeyError):
                code = None
            if code == 0:
                rep.add("OK", "migration", "마지막 migration task exitCode=0")
            else:
                rep.add("HIGH", "migration",
                        f"마지막 migration task 비정상 (exitCode={code})")

    # 6. 엔드포인트 — ALB DNS 로 gateway health 확인 (도메인 없으면 http)
    import urllib.request
    base = outputs.get("gateway_url") or (
        f"http://{outputs['alb_dns_name']}" if outputs.get("alb_dns_name") else "")
    if base:
        for path, expect in (("/health", "gateway-proxy"),):
            try:
                code = urllib.request.urlopen(base + path, timeout=10).status
            except Exception as exc:  # URLError 등 — 원인 요약만
                code = str(exc)[:80]
            rep.add("OK" if code == 200 else "HIGH", "endpoint",
                    f"{expect} {base}{path} → {code}")

    return rep


def format_report(cfg: GatewayConfig, rep: DoctorReport) -> str:
    icon = {"OK": "✓", "WARN": "!", "HIGH": "✗"}
    lines = [f"doctor — {cfg.env} ({cfg.deploy.target}/{cfg.deploy.size_tier})", ""]
    for f in rep.findings:
        lines.append(f"  {icon[f.level]} [{f.level:4}] {f.check}: {f.message}")
    lines.append("")
    lines.append({"OK": "모든 점검 통과",
                  "WARN": "경고 항목 있음 — 확인 권장",
                  "HIGH": "즉시 조치 필요 항목 있음"}[rep.worst])
    return "\n".join(lines)
