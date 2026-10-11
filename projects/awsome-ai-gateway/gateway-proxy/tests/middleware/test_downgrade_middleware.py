# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware.downgrade import DowngradeMiddleware
from app.services.downgrade_loader import DowngradePolicyLoader, DowngradeRule


@dataclass
class FakeAuthContext:
    user_id: str = "u1"
    team_id: str = ""


@dataclass
class FakeBudgetStatus:
    threshold_pct: int = 0


def _make_app(
    rules: list[DowngradeRule],
    threshold_pct: int,
    team_id: str | None,
    allowed_models: list[str] | None = None,
):
    app = FastAPI()

    received_body: dict = {}
    received_state: dict = {}

    @app.post("/v1/messages")
    async def messages(request: Request):
        received_body["body"] = await request.json()
        received_state["downgraded_from"] = request.scope.get("state", {}).get("downgraded_from")
        return {"ok": True}

    loader = DowngradePolicyLoader()

    async def fake_get_rules(*_args, **_kwargs):
        return rules

    loader.get_active_rules = fake_get_rules  # type: ignore[method-assign]

    # 등록 순서 (LIFO 주의): 마지막에 add한 것이 outermost(먼저 실행)
    # (1) inner: DowngradeMiddleware
    app.add_middleware(DowngradeMiddleware, loader=loader)

    # (2) outer: state 주입 — DowngradeMiddleware 실행 전에 state가 채워져 있어야 함
    @app.middleware("http")
    async def inject_state(request: Request, call_next):
        request.scope.setdefault("state", {})
        auth_ctx = FakeAuthContext(team_id=team_id or "")
        auth_ctx.allowed_models = allowed_models
        request.scope["state"]["auth_context"] = auth_ctx
        request.scope["state"]["budget_status"] = FakeBudgetStatus(threshold_pct=threshold_pct)
        request.scope["state"]["_redis"] = None
        request.scope["state"]["_session_factory"] = None
        return await call_next(request)

    return app, received_body, received_state


def test_below_threshold_no_downgrade():
    rules = [DowngradeRule("opus", "sonnet", 80)]
    app, recv, _ = _make_app(rules, threshold_pct=70, team_id=str(uuid.uuid4()))
    client = TestClient(app)
    res = client.post("/v1/messages", json={"model": "opus", "messages": []})
    assert res.status_code == 200
    assert recv["body"]["model"] == "opus"


def test_above_threshold_downgrades_and_rewrites_body():
    rules = [DowngradeRule("opus", "sonnet", 80)]
    app, recv, _ = _make_app(rules, threshold_pct=85, team_id=str(uuid.uuid4()))
    client = TestClient(app)
    res = client.post(
        "/v1/messages",
        json={"model": "opus", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == 200
    assert recv["body"]["model"] == "sonnet"
    assert recv["body"]["messages"] == [{"role": "user", "content": "hi"}]


def test_chain_applied():
    rules = [
        DowngradeRule("opus", "sonnet", 70),
        DowngradeRule("sonnet", "haiku", 90),
    ]
    app, recv, _ = _make_app(rules, threshold_pct=95, team_id=str(uuid.uuid4()))
    client = TestClient(app)
    client.post("/v1/messages", json={"model": "opus"})
    assert recv["body"]["model"] == "haiku"


def test_no_team_id_skips_middleware():
    rules = [DowngradeRule("opus", "sonnet", 50)]
    app, recv, _ = _make_app(rules, threshold_pct=99, team_id=None)
    client = TestClient(app)
    client.post("/v1/messages", json={"model": "opus"})
    assert recv["body"]["model"] == "opus"


def test_loader_failure_fail_open():
    app = FastAPI()
    received: dict = {}

    @app.post("/v1/messages")
    async def messages(request: Request):
        received["body"] = await request.json()
        return {"ok": True}

    loader = DowngradePolicyLoader()

    async def boom(*_args, **_kwargs):
        raise RuntimeError("redis+db down")

    loader.get_active_rules = boom  # type: ignore[method-assign]

    # LIFO: DowngradeMiddleware먼저 add → inner; inject_state 나중에 add → outer
    app.add_middleware(DowngradeMiddleware, loader=loader)

    @app.middleware("http")
    async def inject_state(request: Request, call_next):
        request.scope.setdefault("state", {})
        request.scope["state"]["auth_context"] = FakeAuthContext(team_id=str(uuid.uuid4()))
        request.scope["state"]["budget_status"] = FakeBudgetStatus(threshold_pct=99)
        return await call_next(request)

    client = TestClient(app)
    res = client.post("/v1/messages", json={"model": "opus"})
    assert res.status_code == 200
    assert received["body"]["model"] == "opus"  # fail-open


def test_non_downgrade_path_skipped():
    rules = [DowngradeRule("opus", "sonnet", 50)]
    app, recv, _ = _make_app(rules, threshold_pct=99, team_id=str(uuid.uuid4()))

    @app.get("/health")
    async def health():
        return {"ok": True}

    client = TestClient(app)
    res = client.get("/health")
    assert res.status_code == 200


def test_bedrock_model_path_downgrades():
    """E2E S2-10: /model/{id}/{op} 는 모델이 path 에 있다 — path segment 재작성."""
    rules = [DowngradeRule("us.anthropic.claude-opus-4", "cheap-haiku", 50)]
    app, recv, _ = _make_app(rules, threshold_pct=99, team_id=str(uuid.uuid4()))
    seen: dict = {}

    @app.post("/model/{model_id:path}/converse")
    async def converse(model_id: str, request: Request):
        seen["model_id"] = model_id
        seen["downgraded_from"] = request.scope.get("state", {}).get("downgraded_from")
        return {"ok": True}

    client = TestClient(app)
    res = client.post(
        "/model/us.anthropic.claude-opus-4/converse", json={"messages": []}
    )
    assert res.status_code == 200
    assert seen["model_id"] == "cheap-haiku"
    assert seen["downgraded_from"] == "us.anthropic.claude-opus-4"


def test_responses_path_downgrades():
    """E2E S2-10: Codex /v1/responses 도 body model 재작성 대상."""
    rules = [DowngradeRule("gpt-5-codex", "gpt-5-mini", 50)]
    app, recv, _ = _make_app(rules, threshold_pct=99, team_id=str(uuid.uuid4()))

    @app.post("/v1/responses")
    async def responses(request: Request):
        recv["responses_body"] = await request.json()
        return {"ok": True}

    client = TestClient(app)
    res = client.post("/v1/responses", json={"model": "gpt-5-codex", "input": "hi"})
    assert res.status_code == 200
    assert recv["responses_body"]["model"] == "gpt-5-mini"


def test_downgrade_target_outside_allowed_models_skipped():
    """E2E S2-10: 강등 대상이 allowed_models 밖이면 재작성하지 않는다."""
    rules = [DowngradeRule("opus", "sonnet", 50)]
    app, recv, _ = _make_app(
        rules, threshold_pct=99, team_id=str(uuid.uuid4()),
        allowed_models=["opus"],  # sonnet 은 미허용
    )

    client = TestClient(app)
    res = client.post("/v1/messages", json={"model": "opus"})
    assert res.status_code == 200
    assert recv["body"]["model"] == "opus"
