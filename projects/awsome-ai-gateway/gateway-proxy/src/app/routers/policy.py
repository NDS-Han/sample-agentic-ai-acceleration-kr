"""GET /v1/policy — Claude Code policy for policy-helper.

policy-helper (run by Claude Code at launch and every refreshIntervalMs) calls this
with the user's VK and lays `managedSettings` over its built-in baseline. The route
is exempt from client authorization (the helper's User-Agent is not Claude Code's)
and sits outside the budget / rate-limit / downgrade paths — it is not model usage.

Any non-200 answer makes the helper keep its cached policy, so failures here never
stop Claude Code from starting.

Two slices make the fragment: models (computed here from the tables the proxy enforces)
laid OVER the AI Governance Hub's slice (plugins, MCP servers, lock keys — asked with the
VK's user and team, see services/policy_hub.py). The hub never decides models. A hub
failure is a 503: the helper keeps its last good policy whole. A models-only 200 would
replace the cached team policy with a reduced one — plugins, MCP servers and locks gone
from every PC until the hub is back — while models are enforced server-side anyway.
"""
from __future__ import annotations

import hashlib
import json

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.policy_hub import MODEL_KEYS, HubUnavailable, hub_fragment
from app.services.policy_service import load_models_inputs, models_fragment

logger = structlog.get_logger(__name__)
router = APIRouter()

CLIENT = "claude-code"


def _version(fragment: dict) -> str:
    canonical = json.dumps(fragment, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@router.get("/v1/policy")
async def get_policy(request: Request) -> JSONResponse:
    state = request.scope.get("state", {})
    auth_context = state.get("auth_context")
    if auth_context is None:
        return JSONResponse(status_code=401, content={
            "type": "error", "error": {"type": "authentication_error", "message": "VK required"},
        })
    session_factory = state.get("_session_factory")
    try:
        async with session_factory() as db:
            inputs = await load_models_inputs(db, auth_context.user_id, CLIENT)
    except Exception:
        logger.warning("policy_lookup_failed", user_id=auth_context.user_id, exc_info=True)
        return JSONResponse(status_code=503, content={
            "type": "error", "error": {"type": "api_error", "message": "policy unavailable"},
        })
    if inputs is None:  # unknown or inactive user: nothing, and no hub round-trip
        return JSONResponse(content={"version": _version({}), "managedSettings": {}})
    models = models_fragment(**inputs)
    team_id = getattr(auth_context, "team_id", "") or ""
    try:
        hub = await hub_fragment(auth_context.user_id, team_id)
    except HubUnavailable as exc:
        # 503, not a models-only 200: the helper must keep its whole cached policy (see module doc).
        logger.warning(
            "policy_hub_unavailable", user_id=auth_context.user_id, team_id=team_id, error=str(exc),
        )
        return JSONResponse(status_code=503, content={
            "type": "error", "error": {"type": "api_error", "message": "policy hub unavailable"},
        })
    # models wins: the hub's model keys are dropped even if policy_hub let one through.
    fragment = {**{k: v for k, v in hub.items() if k not in MODEL_KEYS}, **models}
    return JSONResponse(content={"version": _version(fragment), "managedSettings": fragment})
