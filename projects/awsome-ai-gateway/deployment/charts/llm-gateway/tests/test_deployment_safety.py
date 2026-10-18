# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R24-N14 — 이번 라운드의 배포 안전 계약을 렌더로 고정한다.

코드 테스트로는 잡을 수 없는 "차트가 어떻게 주입하는가" 계약들:

- M1: gateway-proxy 종료 예산. SIGKILL 이 lifespan drain 보다 먼저 오면 진행 중
  스트림의 부분 사용량과 버퍼 drain 이 유실된다. grace >= preStop(15) +
  UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT + lifespan(~40) + 여유(5) 여야 한다.
- S1: ExternalSecret 은 Orphan — Owner 면 매 upgrade 마다 Secret 삭제/재생성 창에
  시작하는 pod 가 CreateContainerConfigError 가 된다.
- S2: 정적 1레플리카(autoscaling.minReplicas<=1 포함)에는 PDB 를 렌더하지 않는다
  — minAvailable PDB 는 허용 중단 0 으로 노드 패치/drain 을 영구 차단한다.
- M2: internal_api_token 매핑 존재 — ExternalSecret 은 속성 하나라도 없으면
  Secret 전체 sync 가 실패한다(문서가 빠뜨렸던 키, 계약은 차트가 요구하는 쪽).
- S10: HTTPS 리스너면 SECURE_COOKIES=true — ssl-redirect 전의 평문 요청에 세션
  쿠키가 실리는 것을 막는다(ALB certificate-arn 은 tls.enabled 를 쓰지 않는다).

⚠️ ``helm`` 바이너리 필요 — 없으면 skip, CI 에서는 실패(test_admin_ui_idp_env_parity
   와 같은 규약).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml 이 필요하다(렌더 결과 파싱)")

CHART = Path(__file__).resolve().parents[1]
DEV_VALUES = CHART / "values-eks-fargate-dev.yaml"
PROD_VALUES = CHART / "values-eks-fargate-prod.yaml"
LOADTEST_VALUES = CHART / "values-eks-fargate-prod-loadtest.yaml"


def _lifespan_drain_budget() -> int:
    """gateway-proxy main.py 의 lifespan 종료 drain 예산을 소스에서 읽는다 —
    하드코딩하면 코드가 바뀌어도 테스트가 조용히 녹는다(R24-N14r2)."""
    import re
    src = (CHART.parents[2] / "gateway-proxy" / "src" / "app" / "main.py").read_text()
    m = re.search(r"_shutdown_deadline\s*=\s*\S+\s*\+\s*([\d.]+)", src)
    assert m, "main.py 에 _shutdown_deadline 예산 상수를 찾지 못했다"
    return int(float(m.group(1)))


def _helm() -> str:
    exe = shutil.which("helm")
    if exe is None:
        if os.environ.get("CI"):
            raise AssertionError("CI 인데 helm 이 없다 — 배포 안전 계약이 미검증으로 지나간다.")
        pytest.skip("helm 바이너리가 없다")
    return exe


def _render(*args: str) -> list[dict]:
    cmd = [_helm(), "template", "safety-test", str(CHART)]
    for a in args:
        cmd += ["-f", str(CHART / a)] if a.endswith(".yaml") else ["--set", a]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template 실패:\n{proc.stderr[-2000:]}"
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    assert len(docs) > 5, f"렌더 결과가 {len(docs)}개뿐이다 — 차트 경로/값을 확인"
    return docs


def _deployment(docs: list[dict], fragment: str) -> dict:
    for d in docs:
        if d.get("kind") == "Deployment" and fragment in d["metadata"]["name"]:
            return d
    raise AssertionError(f"{fragment} Deployment 를 렌더 결과에서 찾지 못했다")


def _container_env(dep: dict) -> dict[str, str | None]:
    containers = dep["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value") for e in containers[0].get("env", [])}


def _external_secrets(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("kind") == "ExternalSecret"]


# --------------------------------------------------------------------------
# M1 — gateway-proxy 종료 예산 산식
# --------------------------------------------------------------------------

@pytest.mark.parametrize("values", [
    "values.yaml", "values-eks-fargate-dev.yaml",
    "values-eks-fargate-prod.yaml", "values-eks-fargate-prod-loadtest.yaml",
])
def test_gateway_grace_covers_prestop_plus_uvicorn_plus_lifespan(values: str):
    docs = _render(values)
    dep = _deployment(docs, "gateway-proxy")
    spec = dep["spec"]["template"]["spec"]
    grace = spec["terminationGracePeriodSeconds"]

    prestop = 0
    lifecycle = spec["containers"][0].get("lifecycle") or {}
    cmd = (((lifecycle.get("preStop") or {}).get("exec") or {}).get("command")) or []
    joined = " ".join(str(c) for c in cmd)
    if "sleep" in joined:
        prestop = int(joined.split("sleep")[-1].strip().strip("; "))
    assert prestop == 15, f"preStop 이 {prestop}s — 산식 주석과 어긋남"

    env = _container_env(dep)
    graceful = int(env["UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT"])
    lifespan_budget = _lifespan_drain_budget()
    assert grace >= prestop + graceful + lifespan_budget + 5, (
        f"terminationGracePeriodSeconds({grace}) < "
        f"preStop({prestop}) + graceful({graceful}) + lifespan({lifespan_budget}) + 5 — "
        "SIGKILL 이 lifespan drain 보다 먼저 와 부분 사용량·버퍼가 유실된다"
    )


def test_gateway_graceful_timeout_not_below_stream_idle():
    """graceful 상한이 stream idleTimeout 보다 작으면 정상 스트림이 롤아웃마다 절단."""
    for values, idle in (("values-eks-fargate-dev.yaml", 240), ("values-eks-fargate-prod.yaml", 540)):
        docs = _render(values)
        env = _container_env(_deployment(docs, "gateway-proxy"))
        graceful = int(env["UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT"])
        assert graceful >= idle, f"{values}: graceful({graceful}) < idleTimeout({idle})"


def _chat_drain_budget() -> int:
    """admin-api main.py 의 drain_chat_bg_tasks timeout 을 소스에서 읽는다."""
    import re
    src = (CHART.parents[2] / "admin-api" / "src" / "app" / "main.py").read_text()
    m = re.search(r"drain_chat_bg_tasks\(\s*timeout\s*=\s*([\d.]+)", src)
    assert m, "main.py 에 drain_chat_bg_tasks timeout 을 찾지 못했다"
    return int(float(m.group(1)))


@pytest.mark.parametrize("values", [
    "values.yaml", "values-eks-fargate-dev.yaml",
    "values-eks-fargate-prod.yaml", "values-eks-fargate-prod-loadtest.yaml",
])
def test_admin_api_grace_covers_prestop_plus_uvicorn_plus_chat_drain(values: str):
    """R2-S3 — uvicorn graceful 상한 없이 SSE 가 열려 있으면 lifespan(drain) 진입 전
    SIGKILL 되어 채팅 부분 답변이 유실된다. grace 는 preStop + uvicorn 상한 +
    drain timeout + 여유를 커버해야 한다."""
    docs = _render(values)
    dep = _deployment(docs, "admin-api")
    spec = dep["spec"]["template"]["spec"]
    grace = spec["terminationGracePeriodSeconds"]

    prestop = 0
    lifecycle = spec["containers"][0].get("lifecycle") or {}
    cmd = (((lifecycle.get("preStop") or {}).get("exec") or {}).get("command")) or []
    joined = " ".join(str(c) for c in cmd)
    if "sleep" in joined:
        prestop = int(joined.split("sleep")[-1].strip().strip("; "))
    assert prestop == 10, f"admin-api preStop 이 {prestop}s — 산식 주석과 어긋남"

    env = _container_env(dep)
    graceful = int(env["UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT"])
    assert graceful > 0, "graceful 상한이 없으면 lifespan 진입이 무한 대기된다"
    drain = _chat_drain_budget()
    # R3-N7 — 산식에 audit flush 예산(10s)이 빠져 있었다. values 주석의
    # 산식(preStop+graceful+drain+audit+여유)과 테스트 산식을 일치시킨다.
    audit_flush = 10
    assert grace >= prestop + graceful + drain + audit_flush + 5, (
        f"{values}: terminationGracePeriodSeconds({grace}) < "
        f"preStop({prestop}) + graceful({graceful}) + drain({drain}) "
        f"+ audit({audit_flush}) + 5 — "
        "SIGKILL 이 채팅 drain 보다 먼저 와 부분 답변이 유실된다"
    )


# --------------------------------------------------------------------------
# S1 — ExternalSecret 은 Orphan (upgrade 마다 Secret 삭제/재생성 금지)
# --------------------------------------------------------------------------

def test_external_secrets_use_orphan_creation_policy():
    docs = _render("values-eks-fargate-dev.yaml")
    ess = _external_secrets(docs)
    assert len(ess) >= 3, f"ExternalSecret 이 {len(ess)}개뿐 — app/db/redis 를 렌더해야 한다"
    for es in ess:
        policy = es["spec"]["target"]["creationPolicy"]
        assert policy == "Orphan", (
            f"{es['metadata']['name']}: creationPolicy={policy} — Owner 면 upgrade 때마다 "
            "Secret 이 삭제/재생성돼 그 창의 신규 pod 가 CreateContainerConfigError 가 된다"
        )


# --------------------------------------------------------------------------
# M2 — internal_api_token 매핑 존재 (빠뜨리면 ES 전체 sync 실패)
# --------------------------------------------------------------------------

def test_app_external_secret_maps_internal_api_token():
    docs = _render("values-eks-fargate-dev.yaml")
    app_es = next(e for e in _external_secrets(docs) if e["metadata"]["name"].endswith("-app"))
    keys = {d["secretKey"] for d in app_es["spec"]["data"]}
    assert "internal_api_token" in keys
    # 계약의 다른 필수 키도 같이 확인 — 어느 하나라도 빠지면 같은 실패
    assert {"virtual_key_encryption_key", "nextauth_secret", "jwt_jwks_cache_key"} <= keys


# --------------------------------------------------------------------------
# S2 — 정적 1레플리카에는 PDB 를 렌더하지 않는다
# --------------------------------------------------------------------------

def _pdb_names(docs: list[dict]) -> set[str]:
    return {d["metadata"]["name"] for d in docs if d.get("kind") == "PodDisruptionBudget"}


def test_pdb_not_rendered_for_single_replica_services():
    # dev values: admin-api/admin-ui/워커 전부 replica 1 + autoscaling off → PDB 0 개.
    docs = _render("values-eks-fargate-dev.yaml")
    assert _pdb_names(docs) == set(), (
        f"dev 에 PDB 렌더됨: {_pdb_names(docs)} — minAvailable PDB 는 허용 중단 0 이라 "
        "노드 패치/drain 을 영구 차단한다(라이브 dev 에서 실측됨)"
    )


def test_pdb_rendered_when_replicas_or_hpa_min_gt1():
    docs = _render("values-eks-fargate-prod.yaml")
    names = _pdb_names(docs)
    # prod: gateway-proxy min 3 → PDB 있어야 한다.
    # ⚠️ "gateway" in n 은 모든 PDB 이름(llm-gateway-*)에 매치돼 무력하다 —
    # 정확한 서픽스로 본다 (R24-N16r3).
    assert any(n.endswith("-gateway-proxy") for n in names), f"prod gateway PDB 없음: {names}"


# --------------------------------------------------------------------------
# S10 — HTTPS 리스너면 SECURE_COOKIES=true (ALB cert 는 tls.enabled 미사용)
# --------------------------------------------------------------------------

def test_secure_cookies_on_when_https_listener_configured():
    docs = _render("values-eks-fargate-prod.yaml")
    env = _container_env(_deployment(docs, "admin-ui"))
    assert env.get("SECURE_COOKIES") == "true", (
        "prod 는 HTTPS 리스너가 기본인데 SECURE_COOKIES=false — ssl-redirect 전의 "
        "평문 HTTP 요청에 세션 쿠키가 실린다"
    )


def test_secure_cookies_off_only_for_plain_http():
    docs = _render("values-eks-fargate-dev.yaml")  # 방식 A — HTTP 전용
    env = _container_env(_deployment(docs, "admin-ui"))
    assert env.get("SECURE_COOKIES") == "false"


@pytest.mark.parametrize("key,suffix", [
    ("adminApi", "admin-api"), ("adminUi", "admin-ui"),
    ("gatewayProxy", "gateway-proxy"),
    ("notificationWorker", "notification-worker"),
    ("costRecorderWorker", "cost-recorder-worker"),
])
def test_pdb_not_rendered_when_hpa_min_le_min_available(key: str, suffix: str):
    """autoscaling 켜지면 replicaCount 는 무시된다 — minReplicas<=minAvailable 이면
    PDB 렌더 시 허용 중단 0 (R24-N1r2). replicaCount=3 + HPA min=1 조합으로 검증."""
    docs = _render(
        "values-eks-fargate-prod.yaml",
        f"{key}.autoscaling.enabled=true",
        f"{key}.autoscaling.minReplicas=1",
        # pdb.enabled 까지 명시 — 기본이 false 인 서비스는 음성 테스트가 무의미해진다
        f"{key}.podDisruptionBudget.enabled=true",
    )
    names = _pdb_names(docs)
    assert not any(n.endswith(f"-{suffix}") for n in names), (
        f"HPA min=1 인데 {suffix} PDB 렌더됨: {names} — 스케일다운 중 중단 허용 0"
    )


def test_pdb_rendered_when_hpa_min_gt_min_available():
    """양성 대조 — minReplicas=2 > minAvailable=1 이면 PDB 가 렌더돼야 한다
    (가드가 무조건 false 이면 위 음성 테스트가 통과만 한다)."""
    docs = _render(
        "values-eks-fargate-prod.yaml",
        "adminApi.autoscaling.enabled=true",
        "adminApi.autoscaling.minReplicas=2",
        "adminApi.podDisruptionBudget.enabled=true",
        "adminApi.podDisruptionBudget.minAvailable=1",
    )
    names = _pdb_names(docs)
    assert any(n.endswith("-admin-api") for n in names), (
        f"HPA min=2 인데 admin-api PDB 없음: {names}"
    )


# --------------------------------------------------------------------------
# S3r2 — admin-ui 유효 HTTPS 는 Ingress 와 같은 merge 로 판정
# --------------------------------------------------------------------------

def _ingress(docs: list[dict], fragment: str) -> dict:
    for d in docs:
        if d.get("kind") == "Ingress" and fragment in d["metadata"]["name"]:
            return d
    raise AssertionError(f"{fragment} Ingress 없음")


def test_secure_cookies_respect_per_ingress_override():
    """adminUi.annotations 로 HTTP 전용 override 해도 공통 HTTPS 어노테이션이
    SECURE_COOKIES=true 를 강요하면 안 된다 — HTTP 에서 Secure 쿠키는 버려져
    로그인이 깨진다(R24-S3r2)."""
    docs = _render(
        "values-eks-fargate-prod.yaml",
        'ingress.adminUi.annotations.alb\\.ingress\\.kubernetes\\.io/listen-ports=[{"HTTP":80}]',
    )
    env = _container_env(_deployment(docs, "admin-ui"))
    assert env.get("SECURE_COOKIES") == "false", (
        "admin-ui Ingress 는 HTTP 전용인데 SECURE_COOKIES=true — 브라우저가 버린다"
    )


def test_nextauth_url_scheme_follows_admin_ui_https():
    docs = _render("values-eks-fargate-prod.yaml")
    env = _container_env(_deployment(docs, "admin-ui"))
    assert env["SECURE_COOKIES"] == "true"
    assert env["NEXTAUTH_URL"].startswith("https://"), (
        f"HTTPS 리스너인데 NEXTAUTH_URL={env['NEXTAUTH_URL']} — "
        "콜백 URL/쿠키 스킴이 어긋난다"
    )


# --------------------------------------------------------------------------
# S4r2 — prod ALB deregistration 지연 ≥ preStop + uvicorn graceful
# --------------------------------------------------------------------------

def test_prod_gateway_deregistration_covers_shutdown():
    docs = _render("values-eks-fargate-prod.yaml")
    ing = _ingress(docs, "gateway")
    ann = ing["metadata"]["annotations"]
    tga = ann.get("alb.ingress.kubernetes.io/target-group-attributes", "")
    import re as _re
    m = _re.search(r"deregistration_delay\.timeout_seconds=(\d+)", tga)
    assert m, "prod gateway ingress 에 deregistration_delay 미설정 — 기본 300s 가 긴 스트림을 절단한다"
    dereg = int(m.group(1))

    dep = _deployment(docs, "gateway-proxy")
    env = _container_env(dep)
    graceful = int(env["UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT"])
    assert dereg >= 15 + graceful, (
        f"deregistration_delay({dereg}) < preStop(15)+graceful({graceful}) — "
        "롤아웃 시 ALB 가 uvicorn 보다 먼저 스트림을 끊는다"
    )
