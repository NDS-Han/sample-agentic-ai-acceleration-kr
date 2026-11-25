# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""서비스별 DB pool override(<svc>.db.*) 렌더 계약 — rv-swe P1-3/P2-2.

- 각 서비스는 **자기가 읽는 env 이름**으로 받는다. cost/notification worker 는
  overflow 를 DB_POOL_OVERFLOW 로 읽는다(DB_MAX_OVERFLOW 로 렌더하면 조용히 무시된다
  — 이 wave 가 고친 버그). 워커 timeout/recycle 은 WORKER_DB_POOL_TIMEOUT/
  WORKER_DB_POOL_RECYCLE 이다(공통 DB_POOL_TIMEOUT/RECYCLE 은 워커가 읽지 않는다).
- db 를 선언하지 않으면 per-service env 가 없다 → prod 렌더 불변.
- maxOverflow: 0 은 "0" 으로 렌더된다(유효한 하드캡).
- null 은 미선언으로 본다(빈 value 로 렌더하면 pod 가 int("") 로 CrashLoop).
- 비정수/음수/poolSize 0/env 중복은 렌더 실패.

⚠️ ``helm`` 바이너리 필요 — 없으면 skip, CI 에서는 실패(test_deployment_safety 와 같은 규약).
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

# 차트 키 → (Deployment 이름 조각, 기대 env 이름: poolSize, maxOverflow, poolTimeout, poolRecycle)
_SERVICES = {
    "gatewayProxy": ("gateway-proxy",
                     ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "DB_POOL_RECYCLE")),
    "adminApi": ("admin-api",
                 ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "DB_POOL_RECYCLE")),
    "scheduler": ("scheduler",
                  ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "DB_POOL_RECYCLE")),
    "notificationWorker": ("notification-worker",
                           ("DB_POOL_SIZE", "DB_POOL_OVERFLOW",
                            "WORKER_DB_POOL_TIMEOUT", "WORKER_DB_POOL_RECYCLE")),
    "costRecorderWorker": ("cost-recorder-worker",
                           ("DB_POOL_SIZE", "DB_POOL_OVERFLOW",
                            "WORKER_DB_POOL_TIMEOUT", "WORKER_DB_POOL_RECYCLE")),
}
_ALL_POOL_ENV = {n for _, names in _SERVICES.values() for n in names}
_KEYS = ("poolSize", "maxOverflow", "poolTimeout", "poolRecycle")


def _helm() -> str:
    exe = shutil.which("helm")
    if exe is None:
        if os.environ.get("CI"):
            raise AssertionError("CI 인데 helm 이 없다 — DB pool env 계약이 미검증으로 지나간다.")
        pytest.skip("helm 바이너리가 없다")
    return exe


def _run(*files: Path, sets: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    cmd = [_helm(), "template", "dbpool-test", str(CHART)]
    for f in files:
        cmd += ["-f", str(f)]
    for s in sets:
        cmd += ["--set", s]
    return subprocess.run(cmd, capture_output=True, text=True)


def _render(*files: Path, sets: tuple[str, ...] = ()) -> list[dict]:
    proc = _run(*files, sets=sets)
    assert proc.returncode == 0, f"helm template 실패:\n{proc.stderr[-2000:]}"
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    assert len(docs) > 5, f"렌더 결과가 {len(docs)}개뿐이다 — 차트 경로/값을 확인"
    return docs


def _env_list(docs: list[dict], fragment: str) -> list[dict]:
    for d in docs:
        if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith(fragment):
            return d["spec"]["template"]["spec"]["containers"][0].get("env", [])
    raise AssertionError(f"{fragment} Deployment 를 렌더 결과에서 찾지 못했다")


def _pool_env(docs: list[dict], fragment: str) -> dict[str, str]:
    env = _env_list(docs, fragment)
    names = [e["name"] for e in env]
    assert len(names) == len(set(names)), f"{fragment}: env 이름 중복 {names}"
    return {e["name"]: e.get("value") for e in env if e["name"] in _ALL_POOL_ENV}


def _values_file(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "override.yaml"
    p.write_text(yaml.safe_dump(doc))
    return p


@pytest.mark.parametrize("values", [PROD_VALUES, DEV_VALUES], ids=["prod", "dev"])
def test_no_per_service_pool_env_without_db_block(values):
    """db 미선언 → per-service pool env 없음 (공통 ConfigMap 승계, prod 렌더 불변).
    dev 도 tracked values 에는 db: 가 없다 — 소유자는 gateway.yaml deploy.sizing."""
    docs = _render(values)
    for _, (fragment, _names) in _SERVICES.items():
        assert _pool_env(docs, fragment) == {}, fragment


def test_all_keys_render_exact_env_names_per_service(tmp_path):
    """5개 서비스 × 4개 키 — 서비스별 env 이름과 값을 정확히 고정한다."""
    doc = {hk: {"db": {"poolSize": 3 + i, "maxOverflow": 4 + i,
                       "poolTimeout": 5 + i, "poolRecycle": 1500 + i}}
           for i, hk in enumerate(_SERVICES)}
    docs = _render(PROD_VALUES, _values_file(tmp_path, doc))
    for i, (hk, (fragment, names)) in enumerate(_SERVICES.items()):
        expected = dict(zip(names, (str(3 + i), str(4 + i), str(5 + i), str(1500 + i))))
        assert _pool_env(docs, fragment) == expected, hk


@pytest.mark.parametrize("hk", ["costRecorderWorker", "notificationWorker"])
def test_worker_overflow_env_is_db_pool_overflow(hk):
    """워커 overflow 는 DB_POOL_OVERFLOW — DB_MAX_OVERFLOW 로 렌더하면 무시된다."""
    fragment = _SERVICES[hk][0]
    env = _pool_env(_render(PROD_VALUES, sets=(f"{hk}.db.maxOverflow=7",)), fragment)
    assert env == {"DB_POOL_OVERFLOW": "7"}


def test_max_overflow_zero_renders_zero():
    docs = _render(PROD_VALUES, sets=("gatewayProxy.db.maxOverflow=0",
                                      "costRecorderWorker.db.maxOverflow=0"))
    assert _pool_env(docs, "gateway-proxy") == {"DB_MAX_OVERFLOW": "0"}
    assert _pool_env(docs, "cost-recorder-worker") == {"DB_POOL_OVERFLOW": "0"}


def test_null_value_is_skipped(tmp_path):
    """나중 overlay 의 null — 빈 value 로 렌더하지 않고 미선언으로 본다."""
    over = _values_file(tmp_path, {"costRecorderWorker": {"db": {
        "poolSize": None, "maxOverflow": 6}}})
    docs = _render(PROD_VALUES, over)
    assert _pool_env(docs, "cost-recorder-worker") == {"DB_POOL_OVERFLOW": "6"}


def test_numeric_string_is_accepted(tmp_path):
    over = _values_file(tmp_path, {"adminApi": {"db": {"poolSize": "5"}}})
    assert _pool_env(_render(PROD_VALUES, over), "admin-api") == {"DB_POOL_SIZE": "5"}


@pytest.mark.parametrize("key,value", [
    ("poolSize", "abc"), ("poolSize", -1), ("poolSize", 0), ("poolSize", 1.5),
    ("maxOverflow", -1), ("maxOverflow", "x"), ("poolTimeout", 0),
    ("poolRecycle", -1), ("poolRecycle", True),
])
def test_invalid_values_fail_render(tmp_path, key, value):
    over = _values_file(tmp_path, {"notificationWorker": {"db": {key: value}}})
    proc = _run(PROD_VALUES, over)
    assert proc.returncode != 0, f"{key}={value!r} 가 렌더를 통과했다"
    assert f"notificationWorker.db.{key}" in proc.stderr


@pytest.mark.parametrize("hk,env_name", [
    ("gatewayProxy", "DB_POOL_SIZE"), ("costRecorderWorker", "DB_POOL_OVERFLOW"),
])
def test_duplicate_env_declaration_fails(tmp_path, hk, env_name):
    key = "poolSize" if env_name == "DB_POOL_SIZE" else "maxOverflow"
    over = _values_file(tmp_path, {hk: {"db": {key: 5}, "env": {env_name: "5"}}})
    proc = _run(PROD_VALUES, over)
    assert proc.returncode != 0
    assert "중복" in proc.stderr
