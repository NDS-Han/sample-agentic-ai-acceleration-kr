# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""선언된 지표는 **어딘가에서 실제로 기록돼야** 한다.

왜 이 가드가 필요한가 — 실측
----------------------------
지표 24개가 선언되어 있었고, 그중 **6개는 코드베이스 어디에서도 값을 받지 않았다**:

    gateway_provider_error_total              (에러 카운터. 분모조차 없었다)
    gateway_rate_limit_hits_total             (429 를 내는 3개 경로 모두 미배선)
    gateway_cache_hits_total                  (캐시 히트 분기 미배선)
    gateway_streaming_chunk_duration_seconds  (기록 지점 없음)
    gateway_usage_buffer_size                 (observable gauge 콜백 미등록)
    gateway_budget_remaining_usd              (같음)

**이것이 지표가 없는 것보다 나쁘다.** 한 번도 증가하지 않은 카운터는 Prometheus 에
시계열이 아예 없다. 그래서

    rate(gateway_provider_error_total[5m]) > 0

같은 알람은 **영구히 거짓**이고, 대시보드 패널은 "No data" 를 띄운다. 둘 다 사람에게는
"정상" 으로 읽힌다. 지표가 없으면 최소한 없다는 것을 알지만, 있는데 조용한 지표는
있다고 믿게 만든다.

무엇을 검사하나
---------------
``GatewayMetrics.__init__`` 이 만드는 계측기 속성을 AST 로 뽑고, 각 이름이 소스
어딘가(``metrics.py`` 자신은 제외)에서 참조되는지 본다. 참조되지 않으면 실패한다.
정당한 예외는 ``_ALLOWED_UNWIRED`` 에 **이유와 함께** 올려야 한다 — 목록에 이름을
올리는 행위가 곧 "이건 알고도 비워 둔다" 는 기록이 된다.

⚠️ 이 가드는 "선언 ↔ 참조" 만 본다. 참조가 실제로 실행되는지(도달 가능성)는 보지
   않는다 — 그건 커버리지의 일이다. 그래도 위 6건 전부가 이 검사에 걸린다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"
_METRICS_PY = _SRC / "observability" / "metrics.py"

# 계측기를 만드는 팩토리 이름들. 이 목록에 없는 팩토리가 생기면 아래 대조군이 잡는다.
_FACTORIES = {
    "create_counter",
    "create_histogram",
    "create_up_down_counter",
    "create_observable_gauge",
    "create_gauge",
    "create_observable_counter",
    "create_observable_up_down_counter",
}

# 배선하지 않아도 되는 것 — **이유를 반드시 적는다.**
#
# 지금은 비어 있다. 비어 있는 상태가 정상이며, 여기에 이름을 올리는 행위 자체가
# "이건 알고도 비워 둔다" 는 기록이 된다.
_ALLOWED_UNWIRED: dict[str, str] = {}


def _declared_instruments() -> dict[str, str | None]:
    """``self.<attr> = meter.create_*(<prom_name>, ...)`` 를 전부 뽑는다.

    AST 로 읽는 이유: 문자열 grep 은 주석에 적힌 지표 이름에도 걸려서, **주석만 있고
    선언은 없는** 상태를 선언으로 착각한다(이 레포에서 이미 두 번 겪은 함정 유형).
    """
    tree = ast.parse(_METRICS_PY.read_text(encoding="utf-8"))
    out: dict[str, str | None] = {}
    for node in ast.walk(tree):
        target = None
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Attribute):
                target, value = node.targets[0], node.value
        if target is None or value is None:
            continue
        if not (isinstance(target.value, ast.Name) and target.value.id == "self"):
            continue
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
            continue
        if value.func.attr not in _FACTORIES:
            # 계측기 팩토리가 아닌 self 대입(콜백 리스트 등)은 이름만 기록한다.
            out.setdefault(target.attr, None)
            continue
        prom = None
        if value.args and isinstance(value.args[0], ast.Constant):
            prom = value.args[0].value
        out[target.attr] = prom
    return out


def _attrs_assigned_on_self() -> set[str]:
    """metrics.py 안에서 self 에 대입되는 **모든** 이름(계측기 + 콜백 리스트)."""
    tree = ast.parse(_METRICS_PY.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        )
        for t in targets:
            if not isinstance(t, ast.Attribute):
                continue
            if isinstance(t.value, ast.Name) and t.value.id == "self":
                names.add(t.attr)
    return names


def _observable_gauge_registrars() -> dict[str, str]:
    """observable gauge 속성 → 그 콜백을 등록하는 메서드 이름.

    ⚠️ observable gauge 는 **속성을 직접 참조하지 않는다.** OTel 이 수집 시점에 콜백을
       호출하므로, 배선의 증거는 ``register_*_callback`` 이 불렸는지다. 이걸 모르면
       가드가 정상 배선된 게이지를 "미배선" 으로 오판한다(실제로 처음에 그랬다).

    metrics.py 의 AST 에서 ``self._X_callback.append(...)`` 를 하는 메서드를 찾고,
    같은 리스트를 ``callbacks=`` 로 받는 게이지에 대응시킨다.
    """
    tree = ast.parse(_METRICS_PY.read_text(encoding="utf-8"))

    registrar_of_list: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "append"
                and isinstance(call.func.value, ast.Attribute)
                and isinstance(call.func.value.value, ast.Name)
                and call.func.value.value.id == "self"
            ):
                registrar_of_list[call.func.value.attr] = node.name

    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        for kw in node.value.keywords:
            if kw.arg == "callbacks" and isinstance(kw.value, ast.Attribute):
                lst = kw.value.attr
                if lst in registrar_of_list:
                    out[node.target.attr] = registrar_of_list[lst]
    return out


# 지표를 실제로 올리는 얇은 헬퍼가 사는 곳. 이 안의 참조는 **그 자체로는 배선이 아니다**
# (아래 2단 검사 참조).
_HELPER_DIR = _SRC / "observability"


def _reference_sites() -> dict[str, set[tuple[Path, str | None]]]:
    """지표 이름 → 그것을 참조하는 (파일, 감싸는 함수명) 집합.

    ⚠️ 감싸는 함수명이 필요한 이유: 이름이 어딘가에 등장하는지만 보면 **헬퍼가 언급만
       해도 배선으로 판정된다.** 실측으로 그 구멍에 빠졌다 — `record_rate_limit_hit`
       안의 `metrics.rate_limit_hits_total` 때문에, 그 함수를 부르는 곳을 전부
       지워도 가드가 통과했다. 언급과 호출은 다르다.
    """
    names = _attrs_assigned_on_self()
    sites: dict[str, set[tuple[Path, str | None]]] = {n: set() for n in names}

    for path in _SRC.rglob("*.py"):
        if "__pycache__" in str(path) or path == _METRICS_PY:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover
            continue
        # 각 노드의 감싸는 함수를 알기 위해 부모 링크를 심는다.
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child._parent = parent  # type: ignore[attr-defined]

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in sites:
                continue
            fn = None
            cur = getattr(node, "_parent", None)
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = cur.name
                    break
                cur = getattr(cur, "_parent", None)
            sites[node.attr].add((path, fn))
    return sites


def _functions_called_outside_helpers() -> set[str]:
    """헬퍼 디렉터리 **밖에서** 호출되는 함수 이름 집합."""
    called: set[str] = set()
    for path in _SRC.rglob("*.py"):
        if "__pycache__" in str(path) or _HELPER_DIR in path.parents:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    called.add(f.id)
                elif isinstance(f, ast.Attribute):
                    called.add(f.attr)
    return called


def _referenced_outside_metrics_module() -> set[str]:
    """실제로 **배선된** 지표 이름.

    2단 판정:
      1. 헬퍼 밖(라우터/서비스)에서 직접 참조되면 배선이다.
      2. 헬퍼 안에서만 참조되면, 그 참조를 감싸는 함수가 헬퍼 밖에서 **호출돼야** 한다.
         이게 없으면 "지표를 올리는 함수는 있지만 아무도 부르지 않는" 상태가 통과한다.

    observable gauge 는 속성 참조가 아니라 등록 메서드 호출이 배선의 증거다.
    """
    sites = _reference_sites()
    called_outside = _functions_called_outside_helpers()
    registrars = _observable_gauge_registrars()

    wired: set[str] = set()
    for name, places in sites.items():
        if not places:
            continue
        direct = any(_HELPER_DIR not in path.parents for path, _fn in places)
        if direct:
            wired.add(name)
            continue
        # 헬퍼 전용 참조 — 감싸는 함수가 밖에서 불리는가.
        if any(fn in called_outside for _path, fn in places if fn):
            wired.add(name)

    for gauge, registrar in registrars.items():
        if registrar in called_outside:
            wired.add(gauge)
    return wired


# ─────────────────────────────────────────────────────────────────────────────
# 1. 본 검사
# ─────────────────────────────────────────────────────────────────────────────


def test_every_declared_metric_is_referenced_somewhere():
    declared = _declared_instruments()
    referenced = _referenced_outside_metrics_module()

    unwired = sorted(set(declared) - referenced - set(_ALLOWED_UNWIRED))
    detail = "\n".join(f"    {n}  → {declared[n]}" for n in unwired)
    assert not unwired, (
        f"선언만 되고 어디서도 기록되지 않는 지표 {len(unwired)}개:\n{detail}\n\n"
        "카운터가 한 번도 증가하지 않으면 Prometheus 에 시계열이 없다 — 알람은 영구히 "
        "거짓이고 대시보드는 'No data' 를 띄우며, 사람은 둘 다 '정상' 으로 읽는다.\n"
        "배선하거나, 제거하거나, 이 파일의 _ALLOWED_UNWIRED 에 **이유와 함께** 올릴 것."
    )


def test_allowlist_has_no_stale_entries():
    """예외 목록이 실제 선언과 어긋나면 그 항목은 죽은 문서다."""
    known = set(_declared_instruments()) | _attrs_assigned_on_self()
    stale = sorted(set(_ALLOWED_UNWIRED) - known)
    assert not stale, f"_ALLOWED_UNWIRED 에 존재하지 않는 이름이 있다: {stale}"


def test_allowlist_entries_are_actually_unwired():
    """예외로 올려 뒀는데 이제 배선됐다면 목록에서 빼야 한다(잘못된 인상 방지)."""
    referenced = _referenced_outside_metrics_module()
    now_wired = sorted(set(_ALLOWED_UNWIRED) & referenced)
    assert not now_wired, (
        f"_ALLOWED_UNWIRED 의 {now_wired} 가 이제 배선됐다 — 목록에서 제거할 것"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 대조군 — 이 가드가 공허하지 않은가
# ─────────────────────────────────────────────────────────────────────────────


def test_parser_finds_a_reasonable_number_of_instruments():
    """AST 파싱이 깨지면 declared 가 비어 위 단정이 자동 통과한다."""
    declared = _declared_instruments()
    instruments = {k: v for k, v in declared.items() if v is not None}
    assert len(instruments) >= 15, (
        f"계측기를 {len(instruments)}개만 찾았다 — AST 파싱이 깨졌고 이 가드는 공허하다"
    )
    # Prometheus 이름도 실제로 뽑혔는지(팩토리 첫 인자 파싱 확인).
    assert all(str(v).startswith("gateway_") for v in instruments.values()), (
        f"지표 이름이 gateway_ 로 시작하지 않는 것이 있다: {instruments}"
    )


def test_reference_scan_finds_a_reasonable_number():
    """참조 스캔이 항상 전부를 찾으면(또는 아무것도 못 찾으면) 검사가 무의미하다."""
    referenced = _referenced_outside_metrics_module()
    assert len(referenced) >= 10, (
        f"참조를 {len(referenced)}개만 찾았다 — 경로/정규식이 깨졌다"
    )


def test_a_fake_unwired_metric_would_be_caught():
    """가드의 판정 자체를 검증한다 — 존재하지 않는 이름은 unwired 로 분류돼야 한다."""
    declared = dict(_declared_instruments())
    declared["totally_unwired_probe"] = "gateway_totally_unwired_probe"
    referenced = _referenced_outside_metrics_module()
    unwired = set(declared) - referenced - set(_ALLOWED_UNWIRED)
    assert "totally_unwired_probe" in unwired, (
        "가짜 미배선 지표가 검출되지 않았다 — 판정 로직이 잘못됐다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. 카디널리티 계약
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("forbidden", ["user_id", "team_id", "request_id", "sso_subject"])
def test_no_unbounded_label_in_provider_metrics(forbidden):
    """이 Prometheus 는 prometheus-adapter 로 HPA 도 먹인다.

    사용자 단위 라벨을 붙이면 시계열이 사용자 수만큼 늘어나 관측성과 함께 오토스케일링이
    죽는다. 라벨을 만드는 곳이 한 군데(provider_metrics.build_provider_labels)이므로
    거기만 지키면 된다.
    """
    src = (_SRC / "observability" / "provider_metrics.py").read_text(encoding="utf-8")
    # 주석/docstring 은 제외한다 — 금지 사실을 **설명하는 문장**에 걸리면 안 된다.
    tree = ast.parse(src)
    literals = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(body[0].value.value)
    label_literals = [s for s in literals if s not in docstrings]

    assert forbidden not in label_literals, (
        f"provider_metrics 가 라벨 문자열로 {forbidden!r} 를 쓴다 — 카디널리티 폭발"
    )
    # 대조군: 허용 라벨은 실제로 리터럴에 있어야 한다(추출이 no-op 이 아님을 보인다).
    assert "provider" in label_literals and "model" in label_literals, (
        "허용 라벨조차 찾지 못했다 — 리터럴 추출이 깨졌고 위 단정은 공허하다"
    )
