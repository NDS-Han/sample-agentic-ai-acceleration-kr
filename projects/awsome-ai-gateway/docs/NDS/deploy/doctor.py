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
    exits: dict[str, int] = {}
    for row in rows:
        name = row.get("Service") or row.get("Name", "")
        health = row.get("Health") or ""
        state = row.get("State", "")
        states[name] = health or state
        try:
            exits[name] = int(row.get("ExitCode", 0))
        except (TypeError, ValueError):
            exits[name] = 0

    if not states:
        rep.add("WARN", "services", "기동 중인 서비스가 없습니다 — `docker compose up -d` 필요")
        return rep
    for svc in EXPECTED_SERVICES:
        state = states.get(svc)
        if state is None:
            rep.add("HIGH", "services", f"{svc} 컨테이너 없음")
        elif svc == "migration" and "exited" in state:
            # "exited" 만 보면 ExitCode 1 인 실패 마이그레이션이 완료로 보인다
            if exits.get(svc) == 0:
                rep.add("OK", "services", f"migration 완료 ({state})")
            else:
                rep.add("HIGH", "services",
                        f"migration 실패 (exit={exits.get(svc)}) — 로그를 확인하세요")
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
    # 값이 다른 키뿐 아니라 비어 있거나 삭제된 키도 drift 다 —
    # WEB_SEARCH_ENABLED 를 지우면 기능이 조용히 꺼진다.
    diffs = [k for k, v in desired.items()
             if env.get(k) != v and (v != "" or env.get(k) not in (None, ""))]
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


def _tf_drift_plan_argv(gen_dir: Path) -> list[str]:
    """apply 와 동일하게 managed overlay(gen/<env>/eks/terraform.tfvars)를
    -var-file 로 준다 — NDS 소유 키(enable_notification_ses 등)를 빠뜨리면
    env tfvars 기본값으로 평가돼 선언된 모듈이 destroy 로 오진된다."""
    argv = ["terraform", "plan", "-detailed-exitcode", "-input=false",
            "-no-color", "-lock=false"]
    var_file = gen_dir / "eks" / "terraform.tfvars"
    if var_file.exists():
        argv.append(f"-var-file={var_file}")
    # apply 가 발견한 ALB dimension 도 같이 넘긴다 — 빠뜨리면 alb_* 변수가
    # [] 로 평가돼 이미 만든 ALB 알람이 destroy drift 로 오진된다.
    # apply 가 쓴 그 파일을 읽기 전용 재사용 (AWS 호출 없음).
    alb_dims = gen_dir / "eks" / "alerts-dims.tfvars"
    if alb_dims.exists():
        argv.append(f"-var-file={alb_dims}")
    return argv


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
        ini = _run(["terraform", "init", "-input=false", "-reconfigure",
                    f"-backend-config={backend}"], cwd=env_dir, timeout=180)
        if ini.returncode != 0:
            # init 실패 상태로 plan/output 을 읽으면 이전 init 한 env 의
            # 캐시 backend 로 다른 env 의 건강을 보고한다 — 여기서 중단
            rep.add("WARN", "drift",
                    f"terraform init 실패 — stale backend 로 다른 env 를 "
                    f"볼 수 있으므로 나머지 점검 생략: {ini.stderr.strip()[:150]}")
            return rep
    # 읽기 전용 점검이 state lock 을 잡으면 운영 apply 와 충돌한다
    r = _run(["terraform", "plan", "-detailed-exitcode", "-input=false",
              "-no-color", "-lock=false", f"-var-file={var_file}"],
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
    name_map = outputs.get("service_names") or {}
    names = list(name_map.values()) or list(ECS_SERVICES)
    r = _aws(["ecs", "describe-services", "--cluster", cluster, "--services", *names],
             cfg.aws.region)
    if r.returncode != 0:
        rep.add("WARN", "services", f"describe-services 실패: {r.stderr.strip()[:150]}")
    else:
        try:
            svcs = json.loads(r.stdout).get("services", [])
        except json.JSONDecodeError:
            svcs = []
        # live 서비스명은 service_names output 이 정본 — 커스텀 명명 배포에서
        # 논리명으로 by_name 을 찾으면 전부 "서비스 없음" 으로 오진한다.
        by_name = {s["serviceName"]: s for s in svcs}
        for name in ECS_SERVICES:
            s = by_name.get(name_map.get(name, name))
            if s is None:
                rep.add("HIGH", "services", f"{name} 서비스 없음")
                continue
            running, desired = s.get("runningCount", 0), s.get("desiredCount", 0)
            deps = s.get("deployments") or []
            failed = [d for d in deps if d.get("rolloutState") == "FAILED"]
            rolling = len([d for d in deps
                           if d.get("rolloutState") not in ("COMPLETED", "FAILED")])
            if running >= desired and desired > 0 and rolling == 0 and not failed:
                rep.add("OK", "services", f"{name} {running}/{desired} running")
            elif failed:
                rep.add("WARN", "services",
                        f"{name} 마지막 배포 실패 — circuit-breaker 롤백 "
                        f"({running}/{desired})")
            elif rolling:
                rep.add("WARN", "services", f"{name} 배포 진행 중 ({running}/{desired})")
            else:
                rep.add("HIGH", "services", f"{name} {running}/{desired} running")

    # 5. migration task — taskArns 는 시간순을 보장하지 않으므로 stoppedAt 이
    #    가장 최근인 STOPPED task 를 찾아 exitCode 를 본다
    # ARN 의 family 는 "name:rev" 형태 — rev 를 붙인 채 --family 로 물으면
    # 다른 revision 으로 실행된 task 가 빠져 "실행 기록 없음" 오진이 난다.
    fam = (outputs.get("migration_task_definition_arn") or "") \
        .split("/")[-1].split(":")[0]
    if fam:
        r = _aws(["ecs", "list-tasks", "--cluster", cluster, "--family", fam,
                  "--desired-status", "STOPPED"], cfg.aws.region)
        if r.returncode != 0:
            rep.add("WARN", "migration",
                    f"list-tasks 실패 — migration 이력을 확인할 수 없습니다: "
                    f"{r.stderr.strip()[:150]}")
            fam = ""
        else:
            try:
                arns = json.loads(r.stdout).get("taskArns", [])
            except json.JSONDecodeError:
                arns = []
        if fam and not arns:
            rep.add("HIGH", "migration", "migration task 실행 기록 없음")
        elif fam:
            # describe-tasks 는 호출당 100개 제한 — 절단된 부분집합의
            # max(stoppedAt) 이 진짜 최신이 아닐 수 있어 전부 배치 조회한다.
            tasks = []
            describe_failed = False
            for i in range(0, len(arns), 100):
                r = _aws(["ecs", "describe-tasks", "--cluster", cluster,
                          "--tasks", *arns[i:i + 100]], cfg.aws.region)
                if r.returncode != 0:
                    describe_failed = True
                    break
                try:
                    tasks += json.loads(r.stdout).get("tasks", [])
                except json.JSONDecodeError:
                    describe_failed = True
                    break
            if describe_failed:
                rep.add("WARN", "migration",
                        f"describe-tasks 실패: {r.stderr.strip()[:150]}")
                tasks = []
            if not tasks:
                rep.add("WARN", "migration",
                        "describe-tasks 결과가 비어 있습니다 — 이력 확인 불가")
            else:
                try:
                    task = max(tasks, key=lambda t: t.get("stoppedAt", ""))
                    code = (task.get("containers") or [{}])[0].get("exitCode")
                except (ValueError, IndexError, KeyError, AttributeError):
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

# deployment 이름(kebab) → images.tags / helm values 키(camel)
_KEBAB_TO_CAMEL = {
    "gateway-proxy": "gatewayProxy", "admin-api": "adminApi",
    "admin-ui": "adminUi", "scheduler": "scheduler",
    "notification-worker": "notificationWorker",
    "cost-recorder-worker": "costRecorderWorker",
}


def _d(node) -> dict:
    """leaf 가 dict 가 아니면 {} — `x or {}` 는 truthy 스칼라를 통과시켜
    이후 .get 이 AttributeError 로 죽는 함정을 막는다."""
    return node if isinstance(node, dict) else {}


def _expected_tag(cfg: GatewayConfig, kebab_svc: str) -> str:
    """해당 서비스의 기대 태그 — images.tags 핀이 images.tag 기본값을 덮는다
    (render/eks.py 와 같은 규칙). 어느 쪽도 없으면 '' (검사 생략)."""
    return (cfg.images.tags or {}).get(_KEBAB_TO_CAMEL.get(kebab_svc, kebab_svc),
                                       cfg.images.tag)


def _eks_meta(gen_dir: Path, rep: DoctorReport) -> dict:
    p = gen_dir / "eks" / "deploy.yaml"
    if not p.exists():
        rep.add("HIGH", "artifacts", "gen/<env>/eks/deploy.yaml 없음 — `deploy render` 먼저 실행")
        return {}
    rep.add("OK", "artifacts", "render 산출물 존재")
    try:
        meta = yaml.safe_load(p.read_text())
    except (yaml.YAMLError, OSError) as exc:
        rep.add("HIGH", "artifacts", f"deploy.yaml 파싱 실패: {exc}")
        return {}
    if not isinstance(meta, dict):
        rep.add("HIGH", "artifacts", "deploy.yaml 최상위가 mapping 이 아닙니다")
        return {}
    return meta


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
    if env_dir and env_dir.exists():
        from .render.common import tfvars_clean
        # 전 채널(terraform.tfvars + .json + *.auto.tfvars{,.json})을 terraform
        # 로드 순서로 읽는다 — auto.tfvars 만 선언된 버전을 못 보면 variables.tf
        # 기본값과 라이브를 비교해 허위 다운그레이드 경고를 낸다.
        from .render.eks import _env_tfvars_texts
        for text in _env_tfvars_texts(env_dir):
            # ^ 앵커 + 주석 제거 — `# eks_cluster_version=`/`legacy_eks_cluster_version=`
            # 같은 죽은 선언이 라이브와 무관한 허위 다운그레이드 경고를 만든다.
            # terraform 은 last-wins — 채널 순서대로 돌며 마지막 매치를 취한다.
            m = re.search(r'(?m)^\s*eks_cluster_version\s*=\s*"?(\d+\.\d+)"?',
                          tfvars_clean(text))
            if m:
                declared = m.group(1)
    if not declared and env_dir:
        # 미선언이면 variables.tf 기본값이 실제 적용값 — 비교 대상은 그쪽이다.
        varf = env_dir / "variables.tf"
        if varf.exists():
            try:
                m = re.search(
                    r'variable\s+"eks_cluster_version"[^}]*default\s*=\s*"(\d+\.\d+)"',
                    varf.read_text(), re.S)
            except OSError:
                m = None
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

    # 2. helm release — 바이너리 부재(rc=127, _run 이 변환)와 release 부재를
    #    구분한다: helm 없는 콜드 머신에서 "release 없음" 으로 오진하면 엉뚱한
    #    조치를 유도한다.
    r = _run(["helm", "status", rel, "-n", ns, "-o", "json", *kctx])
    if r.returncode != 0:
        if r.returncode == 127:
            rep.add("HIGH", "release", f"helm CLI 실행 불가: {r.stderr.strip()[:120]}")
        else:
            rep.add("HIGH", "release",
                    f"helm release '{rel}' 없음 — 배포되지 않았거나 "
                    f"이름/namespace 다름: {r.stderr.strip()[:120]}")
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
            all_items = json.loads(r.stdout).get("items", [])
        except json.JSONDecodeError:
            all_items = []
        # 공유 ns 의 다른 릴리스가 이름 접두로 우리 것처럼 보인다 — instance
        # 라벨로 우리 release 만 고른다 (라벨 없는 레거시 채택 배포는 폴백).
        def _inst_of(i) -> str:
            return str(_d(_d(i.get("metadata")).get("labels"))
                       .get("app.kubernetes.io/instance") or "")
        all_items = [i for i in all_items if isinstance(i, dict)]
        ours = [i for i in all_items if _inst_of(i) == rel]
        # 폴백(레거시 무라벨 채택)에서도 다른 릴리스 소유가 명시된 아이템은
        # 제외한다 — instance=<다른 release> 인 외부 deployment 가 component
        # 라벨만 같으면 by_name 을 덮어써 우리 것으로 오흡수된다.
        items = ours or [i for i in all_items if not _inst_of(i)]
        # 서비스 식별은 component 라벨 우선 — 이름 프리픽스는 fullnameOverride
        # 면 어긋나 전부 "deployment 없음" 으로 오진된다. 라벨 없는 수동 배포는
        # 이름 접두(<release>-<svc>) 폴백. setdefault — kubectl 은 이름순
        # 반환이라 동일 component 를 주장하는 중복 아이템이 있으면 첫 것이
        # 우리 release 쪽일 가능성이 높다(덮어쓰기 방지).
        by_name = {}
        for i in items:
            if not isinstance(i, dict):
                continue
            labels = _d(_d(i.get("metadata")).get("labels"))
            comp = labels.get("app.kubernetes.io/component")
            n = comp or str(_d(i.get("metadata")).get("name") or "")
            if not comp:
                for p in (f"{rel}-", "llm-gateway-"):
                    if n.startswith(p):
                        n = n[len(p):]
                        break
            by_name.setdefault(n, i)

        # 앱 컨테이너는 차트가 컴포넌트명으로 명명 — containers[0] 무조건은
        # 사이드카(otel-collector 등) 첫 정의 시 이미지/env 를 잘못 읽는다.
        def _app_container(dep: dict, svc: str) -> dict:
            try:
                cs = (dep["spec"]["template"]["spec"].get("containers") or [])
                return (next((c for c in cs if c.get("name") == svc), None)
                        or (cs[0] if cs else {}))
            except (KeyError, TypeError, AttributeError):
                return {}

        for name in EKS_DEPLOYMENTS:
            d = by_name.get(name) or by_name.get(f"{rel}-{name}")
            if not d:
                rep.add("WARN", "deploy", f"{name} deployment 없음")
                continue
            spec = _d(d.get("spec"))
            stt = _d(d.get("status"))
            want = spec.get("replicas", 1)
            ready = stt.get("readyReplicas", 0)
            img = str(_app_container(d, name).get("image") or "")
            # digest 핀(@sha256:)은 태그 비교 불가 — ':' 파싱이 해시 조각을
            # 태그로 오인해 거짓 drift 를 낸다
            tag = ("" if "@" in img else
                   img.rsplit(":", 1)[-1] if ":" in img else "")
            msg = f"{name} {ready}/{want} ready"
            expected = _expected_tag(cfg, name)
            # readiness 를 태그 drift 보다 먼저 — 다운된 서비스가 drift 경고로
            # 가려져 HIGH 를 놓치면 안 된다 (drift 는 별도 finding)
            if ready and ready >= want:
                rep.add("OK", "deploy", msg)
            else:
                rep.add("HIGH", "deploy", msg)
            if expected and tag and tag != expected:
                rep.add("WARN", "deploy", f"{name} tag {tag} ≠ yaml {expected} — drift")

        # 3.5 bi_insight — 선언됐는데 ARN 미주입이면 /chat 은 503 graceful-off,
        #     CHAT_ENABLED 미설정이면 UI 가 'not deployed' 를 띄운다.
        #     deployments JSON 를 재사용해 env 주입 상태를 본다.
        if getattr(cfg.features, "bi_insight", False):
            def _env_of(svc: str) -> dict:
                d = by_name.get(svc) or by_name.get(f"{rel}-{svc}")
                if not d:
                    return {}
                try:
                    return {
                        e["name"]: e.get("value", "")
                        for e in (_app_container(d, svc).get("env") or [])
                        if isinstance(e, dict)
                    }
                except (KeyError, IndexError, TypeError):
                    return {}
            arn = _env_of("admin-api").get("AGENTCORE_RUNTIME_ARN", "")
            if arn:
                rep.add("OK", "bi_insight",
                        f"Runtime 주입됨 ({arn.rsplit('/', 1)[-1][:24]}…, "
                        f"region={_env_of('admin-api').get('AGENTCORE_REGION', '?')})")
            else:
                rep.add("WARN", "bi_insight",
                        "bi_insight 선언됐으나 admin-api 에 AGENTCORE_RUNTIME_ARN "
                        "미주입 — /chat 은 503 입니다. apply 의 provisioner "
                        "로그를 확인하세요")
            if _env_of("admin-ui").get("CHAT_ENABLED") != "true":
                rep.add("WARN", "bi_insight",
                        "admin-ui CHAT_ENABLED 미설정 — /chat 페이지가 "
                        "'not deployed' 로 표시됩니다")
    else:
        # rc!=0 이면 readiness/env 점검 전체가 스킵된다 — 조용한 '정상' 리포트로
        # 다운된 서비스를 덮지 않게 미검증임을 기록한다
        rep.add("WARN", "deploy",
                f"kubectl get deployments 실패 — readiness 미검증: "
                f"{r.stderr.strip()[:150]}")

    # 4. migration hook Job — 가장 최근 것의 상태
    r = _run(["kubectl", "get", "jobs", "-n", ns, "-o", "json", *ctx])
    if r.returncode == 0:
        try:
            all_jobs = [j for j in json.loads(r.stdout).get("items", [])
                        if isinstance(j, dict)]
            ours = [j for j in all_jobs if _inst_of(j) == rel]
            # deployments 와 같은 규칙 — 무라벨 폴백에서도 타 릴리스 소유가
            # 명시된 Job 은 우리 migration 결과로 오흡수하지 않는다.
            unowned = [j for j in all_jobs if not _inst_of(j)]
            jobs = [j for j in (ours or unowned)
                    if "migration" in str(_d(j.get("metadata")).get("name", ""))]
            job = max(jobs, key=lambda j: _d(j.get("metadata"))
                      .get("creationTimestamp", "")) if jobs else None
        except (json.JSONDecodeError, ValueError, KeyError):
            job = None
        if job is None:
            # 차트 기본 hookDeletePolicy(before-hook-creation,hook-succeeded)는
            # 성공한 Job 을 지운다 — Job 부재는 정상. 마지막 upgrade 결과를
            # helm history 로 대체 확인한다 (Job 이 남는 실패는 위에서 잡힌다).
            h = _run(["helm", "history", rel, "-n", ns, "-o", "json", *kctx])
            last = ""
            if h.returncode == 0:
                try:
                    hist = json.loads(h.stdout)
                    last = (hist[-1].get("status") or "") if hist else ""
                except (json.JSONDecodeError, IndexError, KeyError):
                    last = ""
            if last == "deployed":
                rep.add("OK", "migration",
                        "Job 정리됨(hook-succeeded) — 마지막 upgrade deployed")
            elif last:
                rep.add("HIGH", "migration",
                        f"Job 없음 + 마지막 release status={last} — "
                        "migration 실패가 롤백됐을 수 있습니다. "
                        f"`helm history {rel} -n {ns}` 확인")
            else:
                rep.add("WARN", "migration", "migration Job 을 못 찾았습니다")
        else:
            conds = _d(job.get("status")).get("conditions") or []
            done = any(_d(c).get("type") == "Complete"
                       and _d(c).get("status") == "True" for c in conds)
            failed = any(_d(c).get("type") == "Failed"
                         and _d(c).get("status") == "True" for c in conds)
            name = str(_d(job.get("metadata")).get("name", "?"))
            if done:
                rep.add("OK", "migration", f"{name} Complete")
            elif failed:
                rep.add("HIGH", "migration", f"{name} Failed — kubectl logs job/{name}")
            else:
                rep.add("WARN", "migration", f"{name} 진행 중/상태 불명")
    else:
        rep.add("WARN", "migration",
                f"kubectl get jobs 실패 — migration 상태 미검증: "
                f"{r.stderr.strip()[:150]}")

    # 5. values drift — 관리 키만 대조 (전체 values 는 차트가 소유)
    r = _run(["helm", "get", "values", rel, "-n", ns, "--all", "-o", "yaml", *kctx])
    if r.returncode == 0:
        live = yaml.safe_load(r.stdout) or {}
        if not isinstance(live, dict):
            live = {}  # 비매핑 values 페이로드 — 아래 .get 체인 방어
        drifts = []
        for kebab, camel in _KEBAB_TO_CAMEL.items():
            expected = _expected_tag(cfg, kebab)
            if not expected:
                continue
            t = _d(_d(live.get(camel)).get("image")).get("tag")
            if t and t != expected:
                drifts.append(f"{camel}.image.tag={t}≠{expected}")

        # migration 이미지 ↔ repo alembic head — images.tag 는 migration 을
        # 바꾸지 않으므로(독립 버전 라인) 새 릴리스 배포 시 head 가 뒤처질 수 있다.
        # 태그에 head(\d+) 마커가 있을 때만 비교한다.
        mig_tag = str(_d(_d(live.get("migration")).get("image")).get("tag") or "")
        m = re.search(r"head(\d+)", mig_tag)
        repo_head = _migration_head()
        if m and repo_head:
            mig_head = f"{int(m.group(1)):04d}"
            if int(mig_head) < int(repo_head):
                rep.add("HIGH", "migration",
                        f"migration 이미지 head{mig_head} < repo head{repo_head} — "
                        f"배포해도 DB 가 {repo_head} 까지 안 올라갑니다. "
                        f"images.tags.migration 또는 env overlay 를 새 마이그레이션 "
                        f"이미지로 갱신하세요")
            elif int(mig_head) > int(repo_head):
                rep.add("WARN", "migration",
                        f"migration 이미지 head{mig_head} > repo head{repo_head} — "
                        f"checkout 이 배포된 환경보다 오래됐거나 잘못된 이미지 핀입니다")
        cidr = (_d(_d(live.get("ingress")).get("annotations"))
                .get("alb.ingress.kubernetes.io/inbound-cidrs", ""))
        want_cidrs = ",".join(cfg.network.allowed_cidrs)
        # 선언된 제한이 live 에 없으면 ALB 가 사실상 전체 공개 — 어노테이션
        # 부재(빈 문자열)도 drift 로 잡아야 침묵 개방을 놓치지 않는다
        # 같은 CIDR 집합을 순서만 다르게 쓴 것은 drift 가 아니다 —
        # 어노테이션 문자열과 선언을 집합으로 비교한다.
        if cfg.network.allowed_cidrs and (
                {c.strip() for c in str(cidr).split(",") if c.strip()}
                != set(cfg.network.allowed_cidrs)):
            drifts.append(
                f"inbound-cidrs={cidr or '(미설정 — 전체 개방)'}≠{want_cidrs}")
        live_region = _d(live.get("aws")).get("region")
        if live_region and live_region != cfg.aws.region:
            drifts.append(f"aws.region={live_region}≠{cfg.aws.region}")
        rep.add("WARN" if drifts else "OK", "drift",
                "; ".join(drifts) if drifts else "관리 키가 release values 와 일치")

        # 5b. executor-inflight HPA 전제 — Pods 메트릭이 렌더됐는데 adapter 가
        #     서빙하지 못하면 <unknown> 이 된다. 부분 미보고는 HPA 가 감쇠
        #     계산하지만 전 파드 미보고면 스케일 결정(다운 포함)이 정지한다 —
        #     관측 스택(rules.custom + k8sattributes)이 차트보다 먼저여야 한다.
        if (_d(_d(live.get("gatewayProxy")).get("autoscaling"))
                .get("executorInflightTarget") is not None):
            m = _run(["kubectl", "get", "--raw",
                      f"/apis/custom.metrics.k8s.io/v1beta1/namespaces/{ns}"
                      "/pods/*/gateway_bedrock_executor_inflight",
                      *ctx], timeout=30)
            if m.returncode != 0:
                rep.add("WARN", "hpa-metrics",
                        "executorInflightTarget 선언됐으나 custom.metrics API "
                        "조회 실패 — prometheus-adapter rules.custom + "
                        "otel-collector k8sattributes 배포가 차트보다 먼저 "
                        "필요합니다(전 파드 미보고 시 스케일다운 정지)")
            else:
                try:
                    items = json.loads(m.stdout).get("items", [])
                except json.JSONDecodeError:
                    items = None
                if items is None:
                    rep.add("WARN", "hpa-metrics",
                            "custom.metrics 응답 파싱 실패")
                elif items:
                    rep.add("OK", "hpa-metrics",
                            f"pods 메트릭 {len(items)} 시리즈 서빙 중")
                else:
                    rep.add("WARN", "hpa-metrics",
                            "메트릭 API는 있으나 시리즈 0 — collector "
                            "k8sattributes/워커 시리즈 미도착")
    else:
        rep.add("WARN", "drift",
                f"helm get values 실패 — 태그/CIDR/HPA drift 미검증: "
                f"{r.stderr.strip()[:150]}")

    # 6. terraform plan drift — env_dir 이 있고 state 가 있으면.
    #    apply 와 동일하게 managed overlay(gen terraform.tfvars)를 -var-file 로
    #    준다 — NDS 소유 키(enable_notification_ses 등)를 빠뜨리면 선언이
    #    destroy 로 오진된다.
    env_dir = repo_root / meta["env_dir"] if meta.get("env_dir") else None
    if env_dir and env_dir.exists():
        # plan 은 캐시된 backend 에 붙는다 — 낡은 .terraform 이면 다른 env 의
        # state 로 drift 를 계산해 그대로 보고한다. 선언된 backend 로 먼저
        # reconfigure 한다 (check_ecs 와 같은 보호).
        from .eks_apply import _tf_init
        ini = _tf_init(cfg, env_dir)
        if ini.returncode != 0:
            rep.add("WARN", "tf-drift",
                    f"terraform init 실패 — drift 검사 생략 "
                    f"({(ini.stderr or '').strip()[:120]})")
            return rep
        r = _run(_tf_drift_plan_argv(gen_dir), cwd=env_dir, timeout=300)
        if r.returncode == 0:
            rep.add("OK", "tf-drift", "terraform plan no-op")
        elif r.returncode == 2:
            changed = [ln for ln in r.stdout.splitlines()
                       if ln.startswith(("  # ", "Plan:"))][-5:]
            rep.add("WARN", "tf-drift", "인프라 드리프트 — " + " / ".join(changed)[:200])
        else:
            rep.add("WARN", "tf-drift", "terraform plan 불가(init/state 없음?) — 인프라는 별도 확인")

        # 6b. plain-view 위험 경고 — managed overlay 없이(사람 소유 env tfvars 만)
        #     plan 하면 NDS 소유 토글(SES IRSA/body_logging)이 destroy 로 뜬다.
        #     doctor 가 managed view 만 OK 라고 보여주는 동안 운영자의 plain
        #     terraform apply 가 모듈을 지울 수 있어 별도로 감시한다(R24-H2).
        try:
            from .eks_apply import _infra_targets
        except ImportError:  # pragma: no cover - 패키지/스크립트 호출 양용
            from eks_apply import _infra_targets  # type: ignore
        managed = [t[len("-target="):] for t in _infra_targets(cfg)]
        if managed:
            plain = _run(
                ["terraform", "plan", "-input=false", "-no-color",
                 "-detailed-exitcode", "-lock=false"], cwd=env_dir,
                timeout=300)
            if plain.returncode == 2:
                # plain plan 의 삭제 대상 중 NDS 소유 모듈이 있으면 경고.
                hits = [m for m in managed
                        if re.search(rf"module\.{re.escape(m.split('.', 2)[-1])}\S* "
                                     r"(will be destroyed|must be replaced)",
                                     plain.stdout)]
                if hits:
                    rep.add(
                        "WARN", "tf-drift-plain",
                        "plain terraform apply(사람 경로)가 NDS 소유 모듈을 "
                        f"삭제합니다: {', '.join(hits)} — managed overlay 없는 "
                        "apply 는 실행하지 마세요")
        return rep

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
