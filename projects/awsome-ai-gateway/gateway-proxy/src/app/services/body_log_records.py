# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Shared construction + gating for request/response body log records.

These lived as private helpers in ``routers/messages.py`` while the Anthropic Messages
route was the only one that logged bodies. ``/v1/responses`` (Codex → Mantle GPT-5.6)
now logs too, and the two routes must emit records of the SAME shape: an operator
reading back a Codex complaint queries the same stream, with the same fields, as for a
Claude Code complaint. Two independent copies of ``BodyLogRecord.make`` call sites would
drift — one would gain a field, or spell ``status`` differently, and the stored bodies
would silently stop being comparable across clients.

The gate lives here for the same reason. It is deliberately a *pre*-gate: ``enqueue()``
already no-ops when the logger is not ``enabled_effective``, but the streaming paths
must decide BEFORE the stream starts whether to accumulate the re-framed SSE text in
memory. Asking afterwards would mean buffering an entire response body for every
request just to throw it away.
"""

from __future__ import annotations

import json
from typing import Any

from app.schemas.body_log import BodyLogRecord


def safe_json(raw: bytes) -> dict:
    """Parse for logging only — never raise.

    A body log is diagnostic; failing to parse one must not fail the request that
    produced it. Non-dict JSON is wrapped rather than returned as-is so the stored
    ``request_body`` / ``response_body`` are always objects.
    """
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    except Exception:
        return {"_unparseable": True}


def build_body_record_for_nonstream(
    *,
    request_id: str,
    provider: str,
    client: str | None,
    model_alias: str,
    status_code: int,
    request_body: bytes,
    response_body: bytes,
    is_streaming: bool,
    user_id: str | None = None,
    team_id: str | None = None,
    sso_subject: str | None = None,
    bedrock_request_id: str | None = None,
) -> BodyLogRecord:
    resp = safe_json(response_body)
    is_ok = 200 <= status_code < 300
    error = None if is_ok else (resp.get("error") if isinstance(resp, dict) else None)
    return BodyLogRecord.make(
        request_id=request_id,
        provider=provider,
        client=client or "other",
        model_alias=model_alias,
        status="success" if is_ok else "error",
        is_streaming=is_streaming,
        request_body=safe_json(request_body),
        response_body=resp,
        user_id=user_id,
        team_id=team_id,
        sso_subject=sso_subject,
        bedrock_request_id=bedrock_request_id,
        error=error if error is not None else ({"message": "non-2xx"} if not is_ok else None),
    )


def build_body_record_for_stream(
    *,
    request_id: str,
    provider: str,
    client: str | None,
    model_alias: str,
    status: str,  # "success" | "partial"
    request_body: bytes,
    sse_text: str,
    user_id: str | None = None,
    team_id: str | None = None,
    sso_subject: str | None = None,
    bedrock_request_id: str | None = None,
) -> BodyLogRecord:
    return BodyLogRecord.make(
        request_id=request_id,
        provider=provider,
        client=client or "other",
        model_alias=model_alias,
        status="success" if status == "success" else "partial",
        is_streaming=True,
        request_body=safe_json(request_body),
        response_body={"sse_text": sse_text},
        user_id=user_id,
        team_id=team_id,
        sso_subject=sso_subject,
        bedrock_request_id=bedrock_request_id,
        error=None if status == "success" else {"type": "stream_incomplete", "message": status},
    )


async def resolve_body_logger(app_state: Any, redis: Any, session_factory: Any) -> Any | None:
    """The BodyLogger to use for this request, or ``None`` when logging is off.

    Two independent switches, both of which must be on:

      * ``enabled_effective`` — static infra readiness (feature enabled AND a Firehose
        stream actually configured). Never changes at runtime.
      * ``body_log_flag`` — the dynamic admin toggle, read through a fast in-process
        cache backed by Redis/DB.

    The flag is only consulted when the logger is already effective, so turning the
    feature off at build time costs no Redis round-trip per request. A missing flag
    object means "no dynamic gate configured" → the static answer stands.
    """
    bl = getattr(app_state, "body_logger", None)
    if bl is None or not bl.enabled_effective:
        return None
    flag = getattr(app_state, "body_log_flag", None)
    if flag is not None and not await flag.is_enabled(redis, session_factory):
        return None
    return bl


def provider_name(model_config: Any) -> str:
    """``ProviderType`` → its wire string, tolerating an already-plain provider."""
    provider = model_config.provider
    return provider.value if hasattr(provider, "value") else str(provider)


def model_alias_of(model_config: Any) -> str:
    """The alias that served the request, falling back to the upstream model id.

    ``alias`` is nullable in the model config; a record whose ``model_alias`` came out
    empty would be unattributable, and this is the field the usage screens join on.
    """
    return model_config.alias or model_config.provider_model_id
