"""GET /v1/policy — the route policy-helper calls with the user's VK.

The auth middleware (not exercised here) resolves the VK into state["auth_context"];
these tests inject that state directly and fake the DB loader.
"""
from __future__ import annotations

import contextlib

import httpx
import pytest
from fastapi import FastAPI

from app.routers import policy as policy_router

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ACTIVE = ["claude-haiku-4-5-20251001", "claude-opus-5-5", "claude-sonnet-5"]


class Ctx:
    user_id = "00000000-0000-4000-a000-0000000000aa"


@contextlib.asynccontextmanager
async def fake_session():
    yield object()


@pytest.fixture(autouse=True)
def no_hub(monkeypatch):
    """Default: no hub, whatever POLICY_HUB_URL says in the environment. Tests that want
    the hub patch hub_fragment again and win."""
    async def none(user_id, team_id):
        return {}

    monkeypatch.setattr(policy_router, "hub_fragment", none)


def make_app(state: dict) -> FastAPI:
    app = FastAPI()
    app.include_router(policy_router.router)

    @app.middleware("http")
    async def inject(request, call_next):
        request.scope.setdefault("state", {}).update(state)
        return await call_next(request)

    return app


async def get(state: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=make_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.get("/v1/policy")


def inputs(**over):
    base = {"user_aliases": [], "team_aliases": ["claude-sonnet-5"], "app_models": ACTIVE,
            "active_models": ACTIVE, "default_model": None}
    base.update(over)
    return base


async def test_returns_fragment_for_the_vk_user(monkeypatch):
    seen = {}

    async def loader(db, user_id, client):
        seen.update(user_id=user_id, client=client)
        return inputs()

    monkeypatch.setattr(policy_router, "load_models_inputs", loader)

    resp = await get({"auth_context": Ctx(), "_session_factory": fake_session})

    assert resp.status_code == 200
    body = resp.json()
    assert body["managedSettings"] == {
        "availableModels": ["claude-sonnet-5"], "enforceAvailableModels": True,
    }
    assert body["version"]
    assert seen == {"user_id": Ctx.user_id, "client": "claude-code"}


async def test_unknown_user_gets_empty_fragment(monkeypatch):
    async def loader(db, user_id, client):
        return None

    monkeypatch.setattr(policy_router, "load_models_inputs", loader)

    resp = await get({"auth_context": Ctx(), "_session_factory": fake_session})

    assert resp.status_code == 200
    assert resp.json()["managedSettings"] == {}


async def test_missing_auth_context_is_rejected():
    resp = await get({"_session_factory": fake_session})

    assert resp.status_code == 401


async def test_db_failure_is_503_so_the_helper_keeps_its_cache(monkeypatch):
    async def loader(db, user_id, client):
        raise RuntimeError("db down")

    monkeypatch.setattr(policy_router, "load_models_inputs", loader)

    resp = await get({"auth_context": Ctx(), "_session_factory": fake_session})

    assert resp.status_code == 503


async def test_version_changes_only_when_the_fragment_changes(monkeypatch):
    current = {"value": inputs()}

    async def loader(db, user_id, client):
        return current["value"]

    monkeypatch.setattr(policy_router, "load_models_inputs", loader)
    state = {"auth_context": Ctx(), "_session_factory": fake_session}

    v1 = (await get(state)).json()["version"]
    v1_again = (await get(state)).json()["version"]
    current["value"] = inputs(team_aliases=["claude-opus-5-5"])
    v2 = (await get(state)).json()["version"]

    assert v1 == v1_again
    assert v1 != v2


# --- the hub's slice (plugins / MCP / locks) goes UNDER the gateway's models slice ---------

class TeamCtx(Ctx):
    team_id = "00000000-0000-4000-b000-0000000000bb"


HUB_SLICE = {"enabledPlugins": {"concise-response@example-extensions": True},
             "allowManagedMcpServersOnly": True}


async def test_hub_fragment_is_merged_under_the_models_fragment(monkeypatch):
    asked = {}

    async def loader(db, user_id, client):
        return inputs()

    async def hub(user_id, team_id):
        asked.update(user_id=user_id, team_id=team_id)
        return dict(HUB_SLICE)

    monkeypatch.setattr(policy_router, "load_models_inputs", loader)
    monkeypatch.setattr(policy_router, "hub_fragment", hub)

    resp = await get({"auth_context": TeamCtx(), "_session_factory": fake_session})

    assert resp.status_code == 200
    assert resp.json()["managedSettings"] == {
        **HUB_SLICE, "availableModels": ["claude-sonnet-5"], "enforceAvailableModels": True,
    }
    assert asked == {"user_id": TeamCtx.user_id, "team_id": TeamCtx.team_id}


async def test_models_fragment_wins_over_anything_the_hub_says_about_models(monkeypatch):
    async def loader(db, user_id, client):
        return inputs()

    async def hub(user_id, team_id):
        # policy_hub.fetch_fragment already drops these; the router must not trust that alone.
        return {"availableModels": ["claude-opus-5-5"], "model": "claude-opus-5-5", **HUB_SLICE}

    monkeypatch.setattr(policy_router, "load_models_inputs", loader)
    monkeypatch.setattr(policy_router, "hub_fragment", hub)

    body = (await get({"auth_context": TeamCtx(), "_session_factory": fake_session})).json()

    assert body["managedSettings"]["availableModels"] == ["claude-sonnet-5"]
    assert "model" not in body["managedSettings"]


async def test_unknown_user_does_not_ask_the_hub(monkeypatch):
    calls = []

    async def loader(db, user_id, client):
        return None

    async def hub(user_id, team_id):
        calls.append(team_id)
        return dict(HUB_SLICE)

    monkeypatch.setattr(policy_router, "load_models_inputs", loader)
    monkeypatch.setattr(policy_router, "hub_fragment", hub)

    body = (await get({"auth_context": TeamCtx(), "_session_factory": fake_session})).json()

    assert body["managedSettings"] == {}
    assert calls == []


async def test_version_changes_when_only_the_hub_slice_changes(monkeypatch):
    current = {"hub": dict(HUB_SLICE)}

    async def loader(db, user_id, client):
        return inputs()

    async def hub(user_id, team_id):
        return dict(current["hub"])

    monkeypatch.setattr(policy_router, "load_models_inputs", loader)
    monkeypatch.setattr(policy_router, "hub_fragment", hub)
    state = {"auth_context": TeamCtx(), "_session_factory": fake_session}

    v1 = (await get(state)).json()["version"]
    current["hub"] = {**HUB_SLICE, "allowManagedMcpServersOnly": False}
    v2 = (await get(state)).json()["version"]

    assert v1 != v2
