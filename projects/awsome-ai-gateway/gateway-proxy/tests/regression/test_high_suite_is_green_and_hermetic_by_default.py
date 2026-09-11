# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: 그냥 ``pytest`` 는 GREEN 이고 hermetic 이어야 한다.

## 고친 상태

``pytest`` 한 줄이 **3 failed + 10 errors** 였다. 원인은 세 integration 파일
(``test_count_tokens_endpoint.py`` / ``test_models_endpoint.py`` /
``test_openai_compat_e2e.py``)이 localhost 스택(gateway-proxy:8000, admin-api:8080, seed 된
DB)을 전제하는데 **skip 게이트가 아예 없었다**는 것. 스택이 없으면 ``httpx.ConnectError``.

CI 관점에서 이건 치명적이다 — 붙이는 순간 첫날부터 빨강이고, 빨강이 정상이 되면 진짜 회귀를
아무도 못 본다.

## 그리고 더 나빴던 것: 스위트가 hermetic 하지 않았다

``test_bedrock_real.py`` 의 자격증명 게이트가 **모듈 임포트(수집) 시점에**
``sts.get_caller_identity()`` 를 불렀다. 즉 그냥 ``pytest`` 가 테스트 하나 돌기 전에 실제
AWS 로 나갔다. 그리고 자격증명이 있는 개발자 셸에서는 게이트가 통과하면서
``test_real_bedrock_invoke`` / ``test_real_bedrock_converse`` 가 **실제 Bedrock 추론을 호출**
했다 — 매 ``pytest`` 마다 과금. (실측: 이 가드 도입 전 646 passed 에 그 2개가 포함돼 있었고,
도입 후 644 passed + 2 skipped 가 됐다.)

이제 실 AWS 를 만지는 경로는 ``RUN_REAL_BEDROCK=1`` 로 **명시 opt-in** 해야만 열린다.
레포의 기존 관용구와 같다(``RUN_INVOCATION_LOG_LIVE``, ``REDIS_CLUSTER_URL``).
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

INTEGRATION_DIR = Path(__file__).resolve().parent.parent / "integration"

#: 라이브 스택(HTTP)을 전제하므로 게이트가 반드시 있어야 하는 파일.
_LIVE_STACK_FILES = (
    "test_count_tokens_endpoint.py",
    "test_models_endpoint.py",
    "test_openai_compat_e2e.py",
)


def _module_ast(name: str) -> ast.Module:
    return ast.parse((INTEGRATION_DIR / name).read_text(encoding="utf-8"))


def _has_module_level_pytestmark(tree: ast.Module) -> bool:
    """모듈 최상단에 ``pytestmark = ...`` 대입이 있는가."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# 0) 공허성 대조군
# ──────────────────────────────────────────────────────────────────────────────


def test_the_integration_files_actually_exist():
    """경로가 틀리면 아래 가드 전부가 공허해진다."""
    assert INTEGRATION_DIR.is_dir(), f"integration 디렉터리 없음: {INTEGRATION_DIR}"
    missing = [f for f in _LIVE_STACK_FILES if not (INTEGRATION_DIR / f).exists()]
    assert missing == [], f"검사 대상 파일이 사라졌다(이름 변경?): {missing}"
    # 게이트 헬퍼 자체
    assert (INTEGRATION_DIR / "conftest.py").exists(), "공용 게이트 conftest 가 없다"


def test_the_ast_helper_detects_a_missing_pytestmark():
    """대조군 — 탐지기가 '없음'을 실제로 '없음'이라고 하는가."""
    assert not _has_module_level_pytestmark(ast.parse("import pytest\nx = 1\n"))
    assert _has_module_level_pytestmark(ast.parse("pytestmark = [1]\n"))
    # 클래스/함수 안의 pytestmark 는 모듈 게이트가 아니다.
    assert not _has_module_level_pytestmark(
        ast.parse("def f():\n    pytestmark = [1]\n    return pytestmark\n")
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1) 라이브 스택 전제 파일은 반드시 게이트를 갖는다
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("filename", _LIVE_STACK_FILES)
def test_live_stack_files_are_gated(filename):
    tree = _module_ast(filename)
    assert _has_module_level_pytestmark(tree), (
        f"{filename} 에 모듈 레벨 pytestmark 게이트가 없다 — 스택이 없으면 "
        f"httpx.ConnectError 로 스위트가 RED 가 된다(CI 첫날부터 빨강)"
    )
    src = (INTEGRATION_DIR / filename).read_text(encoding="utf-8")
    assert "live_stack_gate()" in src, (
        f"{filename} 이 공용 게이트 live_stack_gate() 를 쓰지 않는다 — 게이트 로직이 "
        f"파일마다 갈라지면 한 곳만 고쳐도 다른 곳이 남는다"
    )


# DB DSN / 엔진을 실제로 여는 신호. 이 중 하나라도 나오면 그 파일(또는 그 클래스)은
# 실 Postgres 를 요구하므로 게이트가 반드시 있어야 한다.
_DB_SIGNALS = ("create_async_engine", "TEST_DB_URL", ":5432/", "async_sessionmaker")
# 게이트로 인정하는 신호.
_GATE_SIGNALS = ("live_stack_gate(", "live_db_gate(", "db_unavailable(", "skipif")


def _needs_and_has_db_gate(name: str) -> tuple[bool, bool]:
    src = (INTEGRATION_DIR / name).read_text(encoding="utf-8")
    needs = any(sig in src for sig in _DB_SIGNALS)
    has = any(sig in src for sig in _GATE_SIGNALS)
    return needs, has


def test_every_db_touching_integration_file_is_gated():
    """⚠️ 실 Postgres 를 여는 파일은 모듈이든 클래스든 게이트가 있어야 한다.

    하드코딩 목록(_LIVE_STACK_FILES)만으로는 부족했다 — test_bedrock_e2e.py 와
    test_router_service_db.py 가 그 목록에 없어 게이트 없이 CI 에 올라갔고,
    localhost:5432 를 열다 7 failed 로 죽었다(로컬은 떠돌던 compose 컨테이너 덕에 통과).
    이 가드는 목록이 아니라 **내용**으로 판정하므로 새 파일도 자동으로 걸린다.
    """
    ungated = []
    scanned = 0
    for path in sorted(INTEGRATION_DIR.glob("test_*.py")):
        scanned += 1
        needs, has = _needs_and_has_db_gate(path.name)
        if needs and not has:
            ungated.append(path.name)

    # 대조군 — 파일을 못 훑었으면 공허하다.
    assert scanned >= 8, f"integration 파일을 {scanned}개만 훑었다"
    # 대조군 — 신호 집합이 실제로 무언가를 잡는지(전부 통과면 신호가 죽은 것).
    matched = [
        p.name for p in INTEGRATION_DIR.glob("test_*.py")
        if _needs_and_has_db_gate(p.name)[0]
    ]
    assert matched, "DB 신호를 가진 파일을 하나도 못 찾았다 — 신호 문자열이 낡았다"
    assert ungated == [], (
        "실 Postgres 를 여는데 게이트가 없는 integration 파일 — CI 에서 connect 실패로 "
        "RED 가 된다. live_db_gate() 또는 클래스 skipif(db_unavailable(...)) 를 붙일 것:\n  "
        + "\n  ".join(ungated)
    )


def test_the_shared_gate_skips_when_the_stack_is_down():
    """게이트가 실제로 skip 마커를 만드는가(도달성 probe 동작)."""
    from tests.integration.conftest import live_stack_gate

    marks = live_stack_gate()
    assert marks, "게이트가 빈 목록을 돌려준다 — 아무것도 막지 않는다"
    # integration 마커는 항상 붙는다(-m 선택을 위해).
    assert any(getattr(m, "name", None) == "integration" for m in marks), (
        f"integration 마커가 없다: {marks}"
    )
    # 로컬 스택이 떠 있으면 skipif(False), 안 떠 있으면 skipif(True) — 둘 중 하나여야 한다.
    gate = [m for m in marks if getattr(m, "name", None) in ("skipif", "skip")]
    assert gate, f"skip/skipif 마커가 전혀 없다: {marks}"


def test_the_shared_gate_refuses_a_nonlocal_target(monkeypatch):
    """⚠️ 보안 — 이 테스트들은 /internal/test/issue-key 로 실사용 VK 를 발급한다.

    prod 를 가리킨 채 '통과'하는 것이 최악의 결과다. ``/internal/*`` 의 dev 가드는
    ``APP_ENV == "production"`` 을 보는데 prod 는 ``APP_ENV="prod"`` 로 떠 있어서 가드가
    무력했던 전력이 있다 — 즉 prod 에서도 무인증 VK 발급이 실제로 열려 있었다.
    """
    import importlib

    monkeypatch.setenv("GATEWAY_URL", "https://gateway.example.com")
    monkeypatch.setenv("ADMIN_API_URL", "https://admin.example.com")
    monkeypatch.delenv("ALLOW_NONLOCAL_INTEGRATION_TARGET", raising=False)

    mod = importlib.reload(importlib.import_module("tests.integration.conftest"))
    try:
        marks = mod.live_stack_gate()
        skips = [m for m in marks if getattr(m, "name", None) == "skip"]
        assert skips, (
            f"비-로컬 대상인데 무조건 skip 이 아니다 — 실수로 prod 를 향해 VK 를 발급할 수 "
            f"있다: {marks}"
        )
        reason = skips[0].kwargs.get("reason", "")
        assert "비-로컬" in reason and "issue-key" in reason, (
            f"차단 이유가 왜 막혔는지 알려주지 않는다: {reason!r}"
        )

        # 명시 opt-in 하면 통과시켜야 한다(막기만 하면 정당한 용도가 불가능해진다).
        monkeypatch.setenv("ALLOW_NONLOCAL_INTEGRATION_TARGET", "1")
        marks_opt = mod.live_stack_gate()
        assert not [m for m in marks_opt if getattr(m, "name", None) == "skip"], (
            "opt-in 했는데도 무조건 skip 이다 — 정당한 비-로컬 사용을 막는다"
        )
    finally:
        # 다른 테스트가 원래 모듈 상태를 보도록 되돌린다(monkeypatch 는 env 만 되돌린다).
        monkeypatch.undo()
        importlib.reload(importlib.import_module("tests.integration.conftest"))


# ──────────────────────────────────────────────────────────────────────────────
# 2) hermetic — opt-in 없이는 실 AWS 로 나가지 않는다
# ──────────────────────────────────────────────────────────────────────────────


def test_real_bedrock_gate_does_not_touch_aws_without_opt_in(monkeypatch):
    """opt-in 없으면 STS probe 자체를 하지 않는다.

    boto3.client 를 폭발하는 스텁으로 바꿔놓고 게이트를 부른다 — 호출하면 터진다.
    """
    monkeypatch.delenv("RUN_REAL_BEDROCK", raising=False)
    import boto3

    from tests.integration import test_bedrock_real as mod

    calls: list[str] = []

    def _explode(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("service_name", "?"))
        raise AssertionError(
            "opt-in 없이 boto3.client 를 불렀다 — 그냥 pytest 가 실 AWS 로 나간다"
        )

    monkeypatch.setattr(boto3, "client", _explode)
    assert mod._aws_credentials_available() is False
    assert calls == [], f"AWS 클라이언트를 만들었다: {calls}"


def test_real_bedrock_gate_still_probes_when_opted_in(monkeypatch):
    """대조군 — opt-in 하면 예전처럼 STS 로 **실제** 검증해야 한다.

    이게 없으면 위 테스트는 "게이트가 항상 False" 로도 통과한다(= 공허).
    느긋한 자격증명 해석 때문에 client 생성만으론 판별이 안 되므로 probe 는 대체 불가다.
    """
    monkeypatch.setenv("RUN_REAL_BEDROCK", "1")
    import boto3

    from tests.integration import test_bedrock_real as mod

    probed: list[str] = []

    class _FakeSts:
        def get_caller_identity(self):
            probed.append("get_caller_identity")
            return {"Account": "000000000000"}

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeSts())
    assert mod._aws_credentials_available() is True
    assert probed == ["get_caller_identity"], (
        f"opt-in 했는데 STS 검증을 하지 않았다 — 자격증명 없이도 라이브 테스트가 돌아 "
        f"hard-fail 한다(DEVLOG §68.3 재발): {probed}"
    )


def test_real_bedrock_gate_reports_probe_failure_as_skip(monkeypatch):
    """opt-in 했지만 자격증명이 죽어 있으면 False(=skip) — 예외를 밖으로 던지면 수집이 깨진다."""
    monkeypatch.setenv("RUN_REAL_BEDROCK", "1")
    import boto3

    from tests.integration import test_bedrock_real as mod

    def _fail(*a, **k):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(boto3, "client", _fail)
    assert mod._aws_credentials_available() is False


def test_no_integration_module_calls_aws_at_import_time():
    """AST 가드 — 모듈 최상단(함수 밖)에서 boto3 클라이언트를 만들면 수집이 비-hermetic.

    함수 안의 호출은 게이트가 제어할 수 있으므로 허용한다. 문제는 **임포트 시점 실행**이다.
    """
    offenders: list[str] = []
    scanned = 0
    for path in sorted(INTEGRATION_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scanned += 1
        # 최상단 문장만 훑는다(함수/클래스 본문은 제외).
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "client"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "boto3"
                ):
                    offenders.append(f"{path.name}:{sub.lineno}")

    # 대조군 — 파일을 못 훑었으면 위 결과가 공허하다.
    assert scanned >= 8, f"integration 테스트 모듈을 {scanned}개만 훑었다"
    assert offenders == [], (
        "모듈 임포트 시점에 boto3 클라이언트를 만든다 — 그냥 pytest 가 실 AWS 로 나간다. "
        "게이트 함수 안으로 옮기고 opt-in env 로 감쌀 것:\n  " + "\n  ".join(offenders)
    )


def test_bare_pytest_does_not_opt_into_live_aws():
    """이 스위트가 도는 지금, opt-in 스위치가 꺼져 있어야 정상이다.

    켜져 있으면 위 hermetic 단정들이 실제 상황을 반영하지 못한다(= 측정 자체가 오염).
    """
    assert os.environ.get("RUN_REAL_BEDROCK") != "1", (
        "RUN_REAL_BEDROCK=1 로 스위트를 돌리고 있다 — 실 Bedrock 과금 경로가 열린 상태다"
    )
