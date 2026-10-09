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
