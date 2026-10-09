# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""doctor — 배포 상태·드리프트 점검.

compose backend: 산출물 존재 → 서비스 기동/헬스 → .env 완결성 → 마이그레이션 head.
반환 항목은 (severity, message) — HIGH 는 즉시 조치, WARN 은 확인 권고."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .schema import GatewayConfig
from .render import common

def _migration_head() -> str:
    """repo 의 db/versions 에서 최신 revision 을 읽는다 — 하드코드된 head 는
    마이그레이션 추가 때마다 정상 배포를 오진하므로."""
    versions = Path(__file__).resolve().parents[3] / "db" / "versions"
    try:
        nums = [int(f.name.split("_", 1)[0])
                for f in versions.glob("0*.py") if f.name.split("_", 1)[0].isdigit()]
        return f"{max(nums):04d}" if nums else ""
    except OSError:
        return ""


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
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]} 명령을 못 찾았습니다 — 설치/PATH 확인 필요")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{argv[0]} 응답 없음({timeout}s)")


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
    # compose v2 의 --format json 은 버전에 따라 NDJSON(줄당 객체) 또는
    # JSON 배열 — 둘 다 받는다
    rows: list[dict] = []
    try:
        parsed = json.loads(r.stdout)
        if isinstance(parsed, list):
            rows = [x for x in parsed if isinstance(x, dict)]
        elif isinstance(parsed, dict):
            rows = [parsed]
    except json.JSONDecodeError:
        for line in r.stdout.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    states: dict[str, str] = {}
    for row in rows:
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

    # 4. 마이그레이션 head — DB 에서 직접 확인, 기대값은 repo 의 versions 에서
    r = _run(compose_argv + ["exec", "-T",
              "postgres", "psql", "-U", "gateway", "-d", "gateway",
              "-tAc", "SELECT version_num FROM alembic_version"])
    expected = _migration_head()
    if r.returncode == 0:
        head = r.stdout.strip()
        if expected and head == expected:
            rep.add("OK", "migration", f"alembic head = {head}")
        elif head:
            exp = f" (기대 {expected})" if expected else " (repo versions 조회 불가)"
            rep.add("HIGH", "migration", f"alembic head = {head}{exp}")
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
    #    신선한 checkout 에선 backend init 이 먼저 필요하다
    backend = out_dir / "backend.hcl"
    if backend.exists():
        # .terraform 존재 여부로 생략하면 backend.hcl 변경(bucket/table 바꿈)이
        # plan 에 반영되지 않는다 — init 은 멱등이므로 항상 실행한다
        _run(["terraform", "init", "-input=false", "-reconfigure",
              f"-backend-config={backend}"], cwd=env_dir, timeout=180)
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


# ==============================================================================
# eks backend doctor
#   산출물 → kubectl/helm 접근 → release 상태 → deployment readiness →
#   migration hook job → 관리 키 drift (이미지 태그·CIDR·리전)
# ==============================================================================

EKS_DEPLOYMENTS = ("gateway-proxy", "admin-api", "admin-ui", "scheduler",
                   "cost-recorder-worker", "notification-worker")


def _eks_meta(gen_dir: Path, rep: DoctorReport) -> dict:
    p = gen_dir / "eks" / "deploy.yaml"
    if not p.exists():
        rep.add("HIGH", "artifacts", "gen/<env>/eks/deploy.yaml 없음 — `deploy render` 먼저 실행")
        return {}
    rep.add("OK", "artifacts", "render 산출물 존재")
    return yaml.safe_load(p.read_text()) or {}


def _minor_tuple(v: str) -> tuple[int, int]:
    m = re.match(r"(\d+)\.(\d+)", v)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _check_cluster_version(rep: DoctorReport, meta: dict, repo_root: Path,
                           ctx: list[str]) -> None:
    """라이브 서버 버전 vs tfvars 의 eks_cluster_version skew.

    eks terraform env 의 정책은 "선언값 = 라이브"(variables.tf 주석) — 선언이
    라이브보다 낮으면 다음 terraform apply 가 다운그레이드를 시도해 막히고,
    높으면 예고 없이 컨트롤플레인 업그레이드를 수행한다. addon_versions 핀도
    같은 방향으로 어긋난다.
    """
    declared = ""
    env_dir = repo_root / meta["env_dir"] if meta.get("env_dir") else None
    tfvars = env_dir / "terraform.tfvars" if env_dir else None
    if tfvars and tfvars.exists():
        m = re.search(r'eks_cluster_version\s*=\s*"(\d+\.\d+)"',
                      tfvars.read_text())
        declared = m.group(1) if m else ""

    live = ""
    r = _run(["kubectl", "version", "-o", "json", *ctx], timeout=20)
    if r.returncode == 0:
        try:
            gv = (json.loads(r.stdout).get("serverVersion") or {}).get(
                "gitVersion", "")
            m = re.match(r"v?(\d+\.\d+)", gv)
            live = m.group(1) if m else ""
        except json.JSONDecodeError:
            pass

    if not declared or not live:
        rep.add("WARN", "k8s-version",
                f"버전 비교 불가 (tfvars 선언={declared or '?'}, "
                f"live={live or '?'})")
        return
    if declared == live:
        rep.add("OK", "k8s-version", f"클러스터 {live} = tfvars 선언")
    elif _minor_tuple(declared) < _minor_tuple(live):
        rep.add("HIGH", "k8s-version",
                f"라이브 {live} > tfvars 선언 {declared} — 다음 terraform apply 가 "
                "다운그레이드를 시도해 막힙니다. tfvars 의 eks_cluster_version 을 "
                f"{live} 로 올리고 eks_addon_versions 도 그 버전용으로 갱신하세요")
    else:
        rep.add("WARN", "k8s-version",
                f"라이브 {live} < tfvars 선언 {declared} — 다음 terraform apply 가 "
                "컨트롤플레인 업그레이드를 수행합니다 (의도된 홉이면 진행)")


def check_eks(cfg: GatewayConfig, gen_dir: Path, repo_root: Path,
              context: str = "") -> DoctorReport:
    rep = DoctorReport()
    meta = _eks_meta(gen_dir, rep)
    if not meta:
        return rep
    ns, rel = meta.get("namespace", "llm-gateway"), meta.get("release", "llm-gateway")
    ctx = ["--context", context] if context else []
    kctx = ["--kube-context", context] if context else []

    # 1. 클러스터 접근
    r = _run(["kubectl", "get", "ns", ns, *ctx], timeout=30)
    if r.returncode != 0:
        rep.add("HIGH", "cluster",
                f"kubectl 접근 실패 (ns={ns}): {r.stderr.strip()[:150]} — context 확인")
        return rep
    rep.add("OK", "cluster", f"namespace {ns} 접근 가능")

    # 1.5 클러스터 버전 skew — 선언 ≠ 라이브면 다음 terraform 이 막히거나
    #     예고 없이 업그레이드한다 (다운그레이드 커밋은 과거 실제 사고)
    _check_cluster_version(rep, meta, repo_root, ctx)

    # 2. helm release
    r = _run(["helm", "status", rel, "-n", ns, "-o", "json", *kctx])
    if r.returncode != 0:
        rep.add("HIGH", "release", f"helm release '{rel}' 없음 — 배포되지 않았거나 이름/namespace 다름")
        return rep
    try:
        st = json.loads(r.stdout)
        info = st.get("info") or {}
        status, rev = info.get("status", "?"), st.get("version", "?")
        lvl = "OK" if status == "deployed" else "HIGH"
        rep.add(lvl, "release", f"{rel} status={status} revision={rev}")
    except json.JSONDecodeError:
        rep.add("WARN", "release", "helm status 파싱 실패")

    # 3. deployment readiness + 라이브 이미지 태그
    r = _run(["kubectl", "get", "deployments", "-n", ns, "-o", "json", *ctx])
    if r.returncode == 0:
        try:
            items = json.loads(r.stdout).get("items", [])
        except json.JSONDecodeError:
            items = []
        # 배포명은 <release>-<svc> — 기본 release 면 llm-gateway- 를 떼고,
        # 커스텀 release 면 그 프리픽스를 뗀다
        by_name = {}
        for i in items:
            n = i["metadata"]["name"]
            for p in (f"{rel}-", "llm-gateway-"):
                if n.startswith(p):
                    n = n[len(p):]
                    break
            by_name[n] = i
        for name in EKS_DEPLOYMENTS:
            d = by_name.get(name) or by_name.get(f"{rel}-{name}")
            if not d:
                rep.add("WARN", "deploy", f"{name} deployment 없음")
                continue
            spec = d.get("spec") or {}
            stt = d.get("status") or {}
            want = spec.get("replicas", 1)
            ready = stt.get("readyReplicas", 0)
            img = ""
            try:
                img = d["spec"]["template"]["spec"]["containers"][0]["image"]
                tag = img.rsplit(":", 1)[-1]
            except (KeyError, IndexError):
                tag = ""
            msg = f"{name} {ready}/{want} ready"
            if cfg.images.tag and tag and tag != cfg.images.tag:
                rep.add("WARN", "deploy", f"{msg} — tag {tag} ≠ yaml {cfg.images.tag}")
            elif ready and ready >= want:
                rep.add("OK", "deploy", msg)
            else:
                rep.add("HIGH", "deploy", msg)

    # 4. migration hook Job — 가장 최근 것의 상태
    r = _run(["kubectl", "get", "jobs", "-n", ns, "-o", "json", *ctx])
    if r.returncode == 0:
        try:
            jobs = [j for j in json.loads(r.stdout).get("items", [])
                    if "migration" in j["metadata"]["name"]]
            job = max(jobs, key=lambda j: j["metadata"].get("creationTimestamp", "")) if jobs else None
        except (json.JSONDecodeError, ValueError, KeyError):
            job = None
        if job is None:
            rep.add("WARN", "migration", "migration Job 을 못 찾았습니다")
        else:
            conds = (job.get("status") or {}).get("conditions") or []
            done = any(c.get("type") == "Complete" and c.get("status") == "True" for c in conds)
            failed = any(c.get("type") == "Failed" and c.get("status") == "True" for c in conds)
            name = job["metadata"]["name"]
            if done:
                rep.add("OK", "migration", f"{name} Complete")
            elif failed:
                rep.add("HIGH", "migration", f"{name} Failed — kubectl logs job/{name}")
            else:
                rep.add("WARN", "migration", f"{name} 진행 중/상태 불명")

    # 5. values drift — 관리 키만 대조 (전체 values 는 차트가 소유)
    r = _run(["helm", "get", "values", rel, "-n", ns, "--all", "-o", "yaml", *kctx])
    if r.returncode == 0:
        live = yaml.safe_load(r.stdout) or {}
        drifts = []
        if cfg.images.tag:
            t = (((live.get("gatewayProxy") or {}).get("image") or {}).get("tag"))
            if t and t != cfg.images.tag:
                drifts.append(f"gatewayProxy.image.tag={t}≠{cfg.images.tag}")
        cidr = (((live.get("ingress") or {}).get("annotations") or {})
                .get("alb.ingress.kubernetes.io/inbound-cidrs", ""))
        want_cidrs = ",".join(cfg.network.allowed_cidrs)
        if cfg.network.allowed_cidrs and cidr and cidr != want_cidrs:
            drifts.append(f"inbound-cidrs={cidr}≠{want_cidrs}")
        live_region = (live.get("aws") or {}).get("region")
        if live_region and live_region != cfg.aws.region:
            drifts.append(f"aws.region={live_region}≠{cfg.aws.region}")
        rep.add("WARN" if drifts else "OK", "drift",
                "; ".join(drifts) if drifts else "관리 키가 release values 와 일치")

    # 6. terraform plan drift — env_dir 이 있고 state 가 있으면
    env_dir = repo_root / meta["env_dir"] if meta.get("env_dir") else None
    if env_dir and env_dir.exists():
        r = _run(["terraform", "plan", "-detailed-exitcode", "-input=false"],
                 cwd=env_dir, timeout=300)
        if r.returncode == 0:
            rep.add("OK", "tf-drift", "terraform plan no-op")
        elif r.returncode == 2:
            changed = [ln for ln in r.stdout.splitlines()
                       if ln.startswith(("  # ", "Plan:"))][-5:]
            rep.add("WARN", "tf-drift", "인프라 드리프트 — " + " / ".join(changed)[:200])
        else:
            rep.add("WARN", "tf-drift", "terraform plan 불가(init/state 없음?) — 인프라는 별도 확인")

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
