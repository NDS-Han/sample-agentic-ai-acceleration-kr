# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""자동 강등 규칙이 **provider 를 넘지 못하게** 한다.

무엇이 문제였나
---------------
강등은 요청 본문의 ``model`` 을 그대로 바꿔치기한다. 그런데 각 라우트는 자기 provider 로
필터해서 alias 를 해석한다 — ``/v1/messages`` 는 ``resolve_bedrock_model``(provider ==
BEDROCK)이다. 그래서 BEDROCK alias 를 BEDROCK_MANTLE / BEDROCK_RUNTIME_OPENAI alias 로
바꾸는 규칙은 임계값을 넘는 순간 ``LookupError`` → **404** 가 되고, **그 스코프의 모든
사용자가 한꺼번에** 끊긴다. 비용 절감 설정이 팀을 오프라인으로 만드는 것이고, 404 본문에는
강등 규칙이 원인이라는 단서가 없다.

반대 방향(mantle → runtime plane)은 더 조용하고 더 나쁘다: 두 provider 가 같은 리졸버를
통과하므로 **HTTP 200 인 채로** 인증 방식(bearer vs SigV4), 단가, AWS 쪽 invocation 로깅이
함께 바뀐다.

저장 시점이 막을 수 있는 유일한 지점이다 — 요청 시점에는 이미 늦었고(그 요청은 실패한다),
화면은 200 을 받은 뒤다. Admin UI 는 from/to 드롭다운을 필터하지 않고 `from === to` 만
검사하므로 UI 도 막아 주지 않는다.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


class _Alias:
    def __init__(self, alias: str, provider: str):
        self.alias = alias
        self.provider = provider


class _FakeModelRepo:
    def __init__(self, rows: dict[str, str]):
        self._rows = rows
        self.lookups = 0

    async def get_by_alias(self, alias: str):
        self.lookups += 1
        provider = self._rows.get(alias)
        return _Alias(alias, provider) if provider else None


def _rule(from_alias: str, to_alias: str, threshold: int = 80):
    from app.schemas.budgets import DowngradeRuleItem

    return DowngradeRuleItem(
        from_model_alias=from_alias, to_model_alias=to_alias, threshold_pct=threshold
    )


async def _save(rows: dict[str, str], rules, monkeypatch):
    """``set_downgrade_config`` 를 provider 검증 지점까지만 몰아 본다.

    ⚠️ 검증은 예산/규칙 저장보다 **앞**에 있어야 하므로, DB 계층을 대역화하고 그 앞에서
       ValidationError 가 나오는지를 본다. 대역이 호출되면 검증이 통과한 것이다.
    """
    from app.schemas.budgets import AutoDowngradeConfigRequest
    from app.services import budget_service as bs

    # ⚠️ ModelRepository 는 함수 **안에서** import 된다(`from ... import ModelRepository`).
    #    그래서 budget_service 모듈 속성을 패치해도 먹지 않는다 — 원본 모듈을 패치해야
    #    함수-지역 import 가 대역을 집는다. (처음에 모듈 속성만 패치했다가 실제
    #    ModelRepository(None) 이 만들어져 AttributeError 로 실패했다.)
    import app.repositories.model_repository as mr

    repo = _FakeModelRepo(rows)
    monkeypatch.setattr(mr, "ModelRepository", lambda _session: repo)

    reached_budget_lookup = {"hit": False}

    class _BudgetRepo:
        def __init__(self, _session):
            pass

        async def get_first_active_config(self, _scope, _scope_id):
            reached_budget_lookup["hit"] = True
            return None  # 여기서 다른 ValidationError 로 멈춘다(그 지점까지 갔다는 신호)

    monkeypatch.setattr(bs, "BudgetRepository", _BudgetRepo, raising=False)

    svc = bs.BudgetService(cache_mgr=None)
    data = AutoDowngradeConfigRequest(rules=rules)
    actor = type("A", (), {"user_id": uuid.uuid4(), "role": type("R", (), {"value": "ADMIN"})()})()
    try:
        await svc.set_downgrade_config(
            None,
            scope=bs.BudgetScope.TEAM,
            scope_id=uuid.uuid4(),
            data=data,
            actor=actor,
        )
    except Exception as e:  # noqa: BLE001
        return e, reached_budget_lookup["hit"]
    return None, reached_budget_lookup["hit"]


async def test_a_cross_provider_rule_is_refused(monkeypatch):
    """⚠️ 핵심. BEDROCK → RUNTIME_OPENAI 규칙은 저장되면 안 된다.

    저장되면 임계값을 넘는 순간 그 스코프의 모든 /v1/messages 요청이 404 다.
    """
    from app.core.exceptions import ValidationError

    err, reached = await _save(
        {"claude-opus-4-8": "BEDROCK", "codex-gpt-5.6-terra": "BEDROCK_RUNTIME_OPENAI"},
        [_rule("claude-opus-4-8", "codex-gpt-5.6-terra")],
        monkeypatch,
    )
    assert isinstance(err, ValidationError), f"거부되지 않았다: {err!r}"
    assert not reached, "provider 검증이 예산 조회보다 뒤에 있다 — 순서가 뒤집혔다"
    msg = str(err)
    # 운영자가 무엇을 고쳐야 하는지 알 수 있어야 한다.
    assert "provider" in msg.lower()
    assert "claude-opus-4-8" in msg and "codex-gpt-5.6-terra" in msg


async def test_the_quiet_direction_is_refused_too(monkeypatch):
    """mantle → runtime plane 은 404 가 아니라 **200 인 채로** 인증·단가·로깅이 바뀐다.

    404 보다 조용해서 더 오래 남는다 — 이쪽도 막아야 한다.
    """
    from app.core.exceptions import ValidationError

    err, _reached = await _save(
        {"cowork-opus": "BEDROCK_MANTLE", "codex-gpt": "BEDROCK_RUNTIME_OPENAI"},
        [_rule("cowork-opus", "codex-gpt")],
        monkeypatch,
    )
    assert isinstance(err, ValidationError), f"거부되지 않았다: {err!r}"


async def test_a_same_provider_rule_still_passes_validation(monkeypatch):
    """⚠️ 대조군. 위 단정들이 "모든 규칙을 거부" 로 통과하는 것이 아님을 보인다.

    같은 provider 끼리의 강등은 이 기능의 정상 사용례다.
    """
    err, reached = await _save(
        {"claude-opus-4-8": "BEDROCK", "claude-haiku-4-5": "BEDROCK"},
        [_rule("claude-opus-4-8", "claude-haiku-4-5")],
        monkeypatch,
    )
    assert reached, (
        f"같은 provider 규칙이 provider 검증에서 막혔다: {err!r} — 정상 사용례를 깨뜨린다"
    )


async def test_a_missing_alias_is_still_a_not_found(monkeypatch):
    """기존 동작(존재 확인)이 유지되는지."""
    from app.core.exceptions import NotFoundError

    err, _reached = await _save(
        {"claude-opus-4-8": "BEDROCK"},
        [_rule("claude-opus-4-8", "does-not-exist")],
        monkeypatch,
    )
    assert isinstance(err, NotFoundError), f"{err!r}"


async def test_identical_aliases_are_still_refused(monkeypatch):
    from app.core.exceptions import ValidationError

    err, _reached = await _save(
        {"claude-opus-4-8": "BEDROCK"},
        [_rule("claude-opus-4-8", "claude-opus-4-8")],
        monkeypatch,
    )
    assert isinstance(err, ValidationError)
    assert "same" in str(err).lower()


async def test_each_alias_is_looked_up_once(monkeypatch):
    """존재 확인과 provider 대조가 alias 당 조회를 두 번 하지 않아야 한다.

    행을 버리고 다시 읽으면 규칙 수 × 2 만큼의 왕복이 된다 — 관리자 저장은 드물지만,
    같은 값을 두 번 읽는 코드는 두 값이 갈릴 수 있다는 뜻이기도 하다.
    """
    from app.schemas.budgets import AutoDowngradeConfigRequest
    from app.services import budget_service as bs

    import app.repositories.model_repository as mr

    repo = _FakeModelRepo({"a": "BEDROCK", "b": "BEDROCK", "c": "BEDROCK"})
    monkeypatch.setattr(mr, "ModelRepository", lambda _s: repo)

    class _BudgetRepo:
        def __init__(self, _s):
            pass

        async def get_first_active_config(self, _scope, _sid):
            return None

    monkeypatch.setattr(bs, "BudgetRepository", _BudgetRepo, raising=False)
    actor = type("A", (), {"user_id": uuid.uuid4()})()
    with pytest.raises(Exception):
        await bs.BudgetService(cache_mgr=None).set_downgrade_config(
            None,
            scope=bs.BudgetScope.TEAM,
            scope_id=uuid.uuid4(),
            data=AutoDowngradeConfigRequest(rules=[_rule("a", "b"), _rule("b", "c")]),
            actor=actor,
        )
    # 서로 다른 alias 3개 → 조회 3회.
    assert repo.lookups == 3, f"조회가 {repo.lookups}회 — alias 당 1회여야 한다"


def test_the_validation_lives_before_any_write():
    """구조 검사 — 검증이 저장보다 앞이어야 한다.

    뒤에 있으면 잘못된 규칙이 커밋된 뒤에 거부되거나(트랜잭션에 따라) 감사 로그만 남는다.
    """
    src = (_SRC / "services" / "budget_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "set_downgrade_config"
    )
    provider_check = None
    first_write = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if {"from_provider", "to_provider"} <= names and provider_check is None:
                provider_check = node.lineno
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "DowngradePolicy" and first_write is None:
                first_write = node.lineno
    assert provider_check is not None, "provider 대조를 찾지 못했다"
    assert first_write is not None, "DowngradePolicy 생성을 찾지 못했다 — 전제가 깨졌다"
    assert provider_check < first_write, (
        f"provider 검증(L{provider_check})이 규칙 생성(L{first_write})보다 뒤에 있다"
    )
