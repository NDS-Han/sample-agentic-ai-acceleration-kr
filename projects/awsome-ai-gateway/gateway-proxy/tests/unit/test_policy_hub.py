"""fetch_fragment — the non-model part of a Claude Code policy, from the AI Governance Hub.

GET /v1/policy asks the hub with the VK's user and team (POST {hub}/v1/resolve) and lays
the answer UNDER the models fragment the gateway computes itself. The hub never decides
models: any model key it returns is dropped here, so the server-enforced list and Claude
Code's /model list can never disagree. Every failure — unset URL, timeout, non-200, bad
body — degrades to {} (models only) and never blocks the helper.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.policy_hub import fetch_fragment

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

HUB = "http://governance-hub.llm-gateway.svc:8080"
PLUGINS = {
    "strictKnownMarketplaces": [
        {"source": "github", "repo": "example/claude-extensions", "ref": "demo"},
    ],
    "enabledPlugins": {"concise-response@example-extensions": True},
    "allowManagedMcpServersOnly": True,
}
ANSWER = {"version": "7", "managedSettings": {
    **PLUGINS, "availableModels": ["claude-opus-5-5"], "enforceAvailableModels": True, "model": "x",
}}


def hub(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def fetch(client, **over) -> dict:
    kw = {"url": HUB, "token": "hub-secret", "timeout": 2.0, "user_id": "u1", "team_id": "t1"}
    kw.update(over)
    return await fetch_fragment(client, **kw)


async def test_asks_resolve_with_user_team_and_bearer_and_drops_model_keys():
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), auth=request.headers.get("authorization"),
                    body=json.loads(request.content))
        return httpx.Response(200, json=ANSWER)

    async with hub(handler) as client:
        fragment = await fetch(client)

    assert seen["url"] == f"{HUB}/v1/resolve"
    assert seen["auth"] == "Bearer hub-secret"
    assert seen["body"] == {"user_id": "u1", "team_id": "t1", "client": "claude-code"}
    assert fragment == PLUGINS


async def test_trailing_slash_in_the_hub_url_is_harmless():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=ANSWER)

    async with hub(handler) as client:
        await fetch(client, url=HUB + "/")

    assert seen["url"] == f"{HUB}/v1/resolve"


async def test_unset_url_means_models_only_without_a_call():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=ANSWER)

    async with hub(handler) as client:
        assert await fetch(client, url="") == {}
    assert calls == []


@pytest.mark.parametrize("status", [401, 404, 500, 503])
async def test_non_200_degrades_to_models_only(status):
    async with hub(lambda request: httpx.Response(status, json={"detail": "no"})) as client:
        assert await fetch(client) == {}


async def test_timeout_and_connection_errors_degrade_to_models_only():
    def slow(request):
        raise httpx.ReadTimeout("hub slow", request=request)

    def down(request):
        raise httpx.ConnectError("hub down", request=request)

    async with hub(slow) as client:
        assert await fetch(client) == {}
    async with hub(down) as client:
        assert await fetch(client) == {}


@pytest.mark.parametrize("body", [
    "not json",
    json.dumps(["list"]),
    json.dumps({"managedSettings": "x"}),
    json.dumps({"managedSettings": {"_private": 1, "enabledPlugins": {}}}),
])
async def test_unusable_answers_degrade_to_models_only(body):
    async with hub(lambda request: httpx.Response(200, content=body.encode())) as client:
        assert await fetch(client) == {}


async def test_only_model_keys_from_the_hub_means_nothing_from_the_hub():
    answer = {"managedSettings": {
        "availableModels": ["claude-opus-5-5"], "model": "claude-opus-5-5",
    }}
    async with hub(lambda request: httpx.Response(200, json=answer)) as client:
        assert await fetch(client) == {}


async def test_timeout_is_passed_to_the_request():
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json=ANSWER)

    async with hub(handler) as client:
        await fetch(client, timeout=1.5)

    assert seen["timeout"] == {"connect": 1.5, "read": 1.5, "write": 1.5, "pool": 1.5}
