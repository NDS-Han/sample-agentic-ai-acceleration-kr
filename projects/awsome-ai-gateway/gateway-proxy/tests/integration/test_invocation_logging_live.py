# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Bedrock model-invocation logging — the live proof, on real AWS.

This is the test that makes the claim in ``providers/bedrock_openai_adapter.py`` provable
instead of remembered:

    plane    wire       stream   records
    runtime  responses  no          1
    runtime  responses  YES         1      <- the load-bearing one
    runtime  chat       YES         1
    Mantle   responses  no          0      <- negative control (plane)
    Mantle   responses  YES         0

Two properties are worth a *live* test and cannot be mocked:

1. **Streaming is captured.** Streaming is the bulk of real traffic. If AWS only logged
   non-streaming calls, ``invoke_stream`` surfacing ``x-amzn-requestid`` would be pointless
   and every streamed prompt would be unauditable. A mock cannot answer this — only AWS can.
2. **Mantle is not captured.** This is what makes ``usage_logs.bedrock_request_id`` NULL for
   the Mantle plane a *documented property*, not a gap. A reconciliation report that treats
   those NULLs as "missing records" would cry wolf on every codex call forever.

Attribution is by CONTENT, not only by id: every call embeds a unique marker string in its
prompt, so a Mantle record could not hide behind "we joined on the wrong key".

## Why this test is opt-in

Enabling invocation logging is an **account-wide write, per Region** — there is no per-model
or per-principal switch, so for as long as the config is on, EVERY bedrock-runtime call in
that Region has its request and response bodies written to CloudWatch. Region isolation is
the only scoping lever we have. Therefore:

* it runs only with ``RUN_INVOCATION_LOG_LIVE=1`` — never in CI, never by accident;
* it runs only in ``us-east-2`` (dev's GPT-5.6 Region), and refuses ``ap-northeast-2``,
  where prod Claude traffic would be captured;
* it runs only against the expected account id;
* it **refuses to run if logging is already configured** rather than clobbering someone
  else's config;
* it restores the exact pre-state in a ``finally``, then asserts the restore succeeded, and
  deletes its scratch log group.

The role and the sidecar S3 bucket are deliberately LEFT BEHIND: they are the conventional,
tagged, dev resources ``provision_bedrock_invocation_logging.py deploy`` creates and reuses,
and this test exercises that exact code path rather than a parallel copy of it — so a
mistake in the real provisioning script fails here.

Run:

    RUN_INVOCATION_LOG_LIVE=1 python3 -m pytest tests/integration/test_invocation_logging_live.py -v -s
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import time
import uuid
from functools import lru_cache
from pathlib import Path

import boto3
import httpx
import pytest

from app.providers.bedrock_openai_adapter import BedrockOpenAIAdapter
from app.providers.mantle_openai_adapter import MantleOpenAIAdapter
from app.schemas.routing import RoutingProfileSchema
from app.services.mantle_credentials import MantleCredentialBroker
from app.services.sigv4_signer import SigV4Signer

pytestmark = pytest.mark.live

# ── configuration ─────────────────────────────────────────────────────────────
REGION = "us-east-2"
SEOUL = "ap-northeast-2"
EXPECTED_ACCOUNT = os.environ.get("INVLOG_TEST_ACCOUNT", "123456789012")

RUNTIME_ENDPOINT = f"https://bedrock-runtime.{REGION}.amazonaws.com/openai"
RUNTIME_MODEL = "us.openai.gpt-5.6-terra"
MANTLE_ENDPOINT = f"https://bedrock-mantle.{REGION}.api.aws/openai"
MANTLE_MODEL = "openai.gpt-5.6-terra"

# Delivery is asynchronous. Records normally appear within a few seconds; the ceiling is
# generous because a false "not captured" here would be read as a product defect.
DELIVERY_TIMEOUT_S = 180
POLL_INTERVAL_S = 5

_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "deployment"
    / "scripts"
    / "provision_bedrock_invocation_logging.py"
)


def _load_provisioner():
    """Import the real provisioning script by path.

    Reused rather than reimplemented on purpose: the IAM trust policy, the log-group ARN
    scoping and the ``loggingConfig`` shape are the parts most likely to be wrong, and a
    test that built its own copy would pass while the shipped script stayed broken.
    """
    spec = importlib.util.spec_from_file_location("_invlog_provisioner", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def _preconditions() -> str | None:
    """Return a skip reason, or None when it is safe to run.

    Cached: it is read twice to build the skip marker, and the STS call must not be made
    twice at collection time (nor at all when the opt-in flag is absent).
    """
    if os.environ.get("RUN_INVOCATION_LOG_LIVE") != "1":
        return (
            "RUN_INVOCATION_LOG_LIVE != 1 — this test enables ACCOUNT-WIDE body capture in "
            f"{REGION} for the duration of the run. Opt in explicitly."
        )
    if not _SCRIPT.exists():
        return f"provisioning script not found at {_SCRIPT}"
    try:
        from botocore.config import Config

        ident = boto3.client(
            "sts",
            config=Config(connect_timeout=5, read_timeout=5, retries={"max_attempts": 2}),
        ).get_caller_identity()
    except Exception as exc:  # no creds, offline, expired SSO
        return f"AWS credentials unusable ({type(exc).__name__})"
    if ident["Account"] != EXPECTED_ACCOUNT:
        return (
            f"caller account {ident['Account']} != expected {EXPECTED_ACCOUNT} — refusing to "
            "write a logging configuration into an unexpected account"
        )
    return None


_SKIP = pytest.mark.skipif(_preconditions() is not None, reason=str(_preconditions()))


def _marker(tag: str) -> str:
    """A token that can only come from this run, for content-based attribution."""
    return f"INVLOG-{tag}-{uuid.uuid4().hex[:12]}"


def _profile(region: str = REGION) -> RoutingProfileSchema:
    return RoutingProfileSchema(
        client="codex", backend="invoke", region=region, default_model="gpt-5.6-terra"
    )


def _responses_body(marker: str, stream: bool, model: str) -> bytes:
    return json.dumps(
        {
            "model": model,
            "input": f"Reply with exactly this token and nothing else: {marker}",
            "max_output_tokens": 32,
            **({"stream": True} if stream else {}),
        }
    ).encode()


def _chat_body(marker: str, stream: bool, model: str) -> bytes:
    return json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "user", "content": f"Reply with exactly this token: {marker}"}
            ],
            "max_completion_tokens": 32,
            **({"stream": True} if stream else {}),
        }
    ).encode()


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def no_bearer_token_env():
    """Remove ``AWS_BEARER_TOKEN_BEDROCK`` for the whole module.

    A bearer token in the shell environment silently overrides SigV4 for Bedrock SDK calls
    and can belong to a DIFFERENT account than the one STS just reported. Every account
    assertion in this file — and the whole point of checking ``identity.arn`` in the log
    record — is void if it is present, so it is removed rather than tolerated.
    """
    stashed = os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
    if stashed:
        print("\n[invlog-test] removed AWS_BEARER_TOKEN_BEDROCK from the test environment")
    try:
        yield
    finally:
        if stashed is not None:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = stashed


@pytest.fixture(scope="module")
def logging_enabled(no_bearer_token_env):
    """Enable invocation logging to a SCRATCH log group, then restore the exact pre-state.

    The scratch group is per-run so a failed run can never leave a half-configured shared
    group behind, and so ``filter_log_events`` sees only this run's records — a shared group
    would make "exactly one record per invocation" unprovable.
    """
    prov = _load_provisioner()
    acct = prov.account_id()

    pre = prov.get_logging_config(REGION)
    if pre is not None:
        pytest.skip(
            f"invocation logging is ALREADY configured in {REGION}: {json.dumps(pre)[:200]} — "
            "refusing to clobber a configuration this test did not create. Inspect it with "
            "`provision_bedrock_invocation_logging.py status` and re-run once it is intended "
            "to be off."
        )

    seoul_pre = prov.get_logging_config(SEOUL)
    log_group = f"/aws/bedrock/gwtest-invlog-{int(time.time())}"

    prov.ensure_log_group(REGION, log_group, retention_days=1, dry=False)
    bucket = prov.ensure_bucket(acct, REGION, None, 7, False)
    role_arn = prov.ensure_role(acct, REGION, log_group, bucket, False)
    prov.put_logging_config(REGION, log_group, role_arn, bucket, {"text"}, False)

    live = prov.get_logging_config(REGION)
    assert live, "PutModelInvocationLoggingConfiguration reported success but the config is absent"
    assert live["cloudWatchConfig"]["logGroupName"] == log_group

    started_at_ms = int(time.time() * 1000) - 5_000
    try:
        yield {
            "prov": prov,
            "log_group": log_group,
            "account": acct,
            "started_at_ms": started_at_ms,
            "seoul_pre": seoul_pre,
        }
    finally:
        # Restore first, delete second: if the delete fails we still want logging off.
        bedrock = boto3.client("bedrock", region_name=REGION)
        try:
            bedrock.delete_model_invocation_logging_configuration()
            print(f"\n[invlog-test] logging configuration deleted in {REGION}")
        except Exception as exc:
            print(f"\n[invlog-test] !! FAILED to delete logging config in {REGION}: {exc}")
            raise
        restored = prov.get_logging_config(REGION)
        assert restored is None, (
            f"pre-state NOT restored: {REGION} still has a logging configuration {restored!r}. "
            "Account-wide body capture is still ON — disable it manually."
        )
        try:
            boto3.client("logs", region_name=REGION).delete_log_group(logGroupName=log_group)
            print(f"[invlog-test] scratch log group deleted: {log_group}")
        except Exception as exc:
            print(f"[invlog-test] scratch log group NOT deleted ({log_group}): {exc}")
        print(
            "[invlog-test] left in place (conventional dev resources, reused by "
            f"`provision_bedrock_invocation_logging.py deploy`): role={role_arn} bucket={bucket}"
        )


@pytest.fixture(scope="module")
def calls(logging_enabled):
    """Make one call per (plane, wire, streaming) combination and return their markers/ids.

    All five calls happen once, in one fixture, so delivery is waited for once instead of
    five times — and so the "exactly one record per invocation" count is taken over a
    window that contains all of them.
    """
    async def _run():
        results: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=120.0) as http:
            runtime = BedrockOpenAIAdapter(http_client=http, signer=SigV4Signer())
            mantle = MantleOpenAIAdapter(http_client=http, broker=MantleCredentialBroker(None))
            profile = _profile()

            # 1. runtime / responses / non-streaming
            m = _marker("rt-resp-sync")
            status, body, headers, usage = await runtime.invoke(
                _responses_body(m, False, RUNTIME_MODEL),
                RUNTIME_MODEL,
                profile=profile,
                endpoint=RUNTIME_ENDPOINT,
                wire="responses",
            )
            results["rt_resp_sync"] = {
                "marker": m,
                "status": status,
                "request_id": headers.get("x-amzn-requestid"),
                "usage": usage,
                "body": body[:400],
            }

            # 2. runtime / responses / streaming
            m = _marker("rt-resp-stream")
            status, gen, _h, req_id = await runtime.invoke_stream(
                _responses_body(m, True, RUNTIME_MODEL),
                RUNTIME_MODEL,
                profile=profile,
                endpoint=RUNTIME_ENDPOINT,
                wire="responses",
            )
            chunks = [c async for c in gen]  # drain: an unread stream may not be logged
            results["rt_resp_stream"] = {
                "marker": m,
                "status": status,
                "request_id": req_id,
                "chunks": len(chunks),
                "body": b"".join(chunks)[:400],
            }

            # 3. runtime / chat / streaming
            m = _marker("rt-chat-stream")
            status, gen, _h, req_id = await runtime.invoke_stream(
                _chat_body(m, True, RUNTIME_MODEL),
                RUNTIME_MODEL,
                profile=profile,
                endpoint=RUNTIME_ENDPOINT,
                wire="chat",
            )
            chunks = [c async for c in gen]
            results["rt_chat_stream"] = {
                "marker": m,
                "status": status,
                "request_id": req_id,
                "chunks": len(chunks),
                "body": b"".join(chunks)[:400],
            }

            # 4. Mantle / responses / non-streaming  (negative control)
            m = _marker("mantle-sync")
            status, body, headers, _u = await mantle.invoke(
                _responses_body(m, False, MANTLE_MODEL),
                MANTLE_MODEL,
                profile=profile,
                endpoint=MANTLE_ENDPOINT,
            )
            results["mantle_sync"] = {
                "marker": m,
                "status": status,
                "request_id": headers.get("x-amzn-requestid"),
                "body": body[:400],
            }

            # 5. Mantle / responses / streaming  (negative control)
            m = _marker("mantle-stream")
            status, gen, _h, req_id = await mantle.invoke_stream(
                _responses_body(m, True, MANTLE_MODEL),
                MANTLE_MODEL,
                profile=profile,
                endpoint=MANTLE_ENDPOINT,
            )
            chunks = [c async for c in gen]
            results["mantle_stream"] = {
                "marker": m,
                "status": status,
                "request_id": req_id,
                "chunks": len(chunks),
                "body": b"".join(chunks)[:400],
            }
        return results

    out = asyncio.run(_run())
    for name, r in out.items():
        print(f"[invlog-test] {name}: HTTP {r['status']} request_id={r.get('request_id')}")
    return out


@pytest.fixture(scope="module")
def records(logging_enabled, calls):
    """Every log record delivered in this run's window, parsed.

    Waits until the three runtime calls that returned 200 are all present, then returns
    whatever was delivered. Polling on the POSITIVE cases only is deliberate: the Mantle
    negative control can never satisfy a wait condition, so waiting on it would just burn
    the timeout and then assert the same thing.
    """
    logs = boto3.client("logs", region_name=REGION)
    log_group = logging_enabled["log_group"]
    start = logging_enabled["started_at_ms"]

    expected_ids = {
        calls[k]["request_id"]
        for k in ("rt_resp_sync", "rt_resp_stream", "rt_chat_stream")
        if calls[k]["status"] == 200 and calls[k]["request_id"]
    }

    deadline = time.time() + DELIVERY_TIMEOUT_S
    parsed: list[dict] = []
    while True:
        parsed = []
        token = None
        while True:
            kwargs = {"logGroupName": log_group, "startTime": start, "limit": 1000}
            if token:
                kwargs["nextToken"] = token
            try:
                resp = logs.filter_log_events(**kwargs)
            except logs.exceptions.ResourceNotFoundException:
                resp = {"events": []}
            for event in resp.get("events", []):
                try:
                    parsed.append(json.loads(event["message"]))
                except json.JSONDecodeError:
                    continue
            token = resp.get("nextToken")
            if not token:
                break
        seen = {r.get("requestId") for r in parsed}
        if expected_ids and expected_ids <= seen:
            break
        if time.time() > deadline:
            break
        time.sleep(POLL_INTERVAL_S)

    print(
        f"[invlog-test] {len(parsed)} record(s) in {log_group}; "
        f"waited for {len(expected_ids)} runtime id(s)"
    )
    return parsed


def _by_marker(records: list[dict], marker: str) -> list[dict]:
    """Records whose request body contains the marker — attribution by CONTENT.

    Independent of the request id, so a Mantle record cannot be missed just because its
    ``req_...`` id does not join to anything.
    """
    return [r for r in records if marker in json.dumps(r.get("input", {}))]


# ── runtime plane: captured ───────────────────────────────────────────────────
@_SKIP
def test_runtime_nonstreaming_is_captured_and_joins_on_the_request_id(calls, records):
    call = calls["rt_resp_sync"]
    assert call["status"] == 200, call["body"]
    assert call["request_id"], "no x-amzn-requestid returned — nothing to join a log record to"

    matches = [r for r in records if r.get("requestId") == call["request_id"]]
    assert len(matches) == 1, (
        f"expected exactly 1 record for {call['request_id']}, got {len(matches)}"
    )
    rec = matches[0]
    assert rec["accountId"] == EXPECTED_ACCOUNT
    assert rec["region"] == REGION
    assert RUNTIME_MODEL in rec["modelId"]
    assert rec["identity"]["arn"]
    assert rec["input"]["inputTokenCount"] > 0
    assert rec["output"]["outputTokenCount"] > 0
    # The id in the header and the id in the record are the SAME id — this is the
    # usage_logs.bedrock_request_id join, proven rather than assumed.
    assert _by_marker(records, call["marker"]) == matches


@_SKIP
def test_runtime_streaming_is_captured(calls, records):
    """The load-bearing case: if streaming were NOT logged, most traffic would be
    unauditable and ``invoke_stream`` returning the request id would be dead code."""
    call = calls["rt_resp_stream"]
    assert call["status"] == 200, call["body"]
    assert call["chunks"] > 0, "no SSE chunks — the call did not actually stream"
    assert call["request_id"], "streaming returned no x-amzn-requestid"

    matches = [r for r in records if r.get("requestId") == call["request_id"]]
    assert len(matches) == 1, (
        "STREAMING NOT CAPTURED (or captured more than once): expected exactly 1 record for "
        f"{call['request_id']}, got {len(matches)}. This invalidates the table in "
        "providers/bedrock_openai_adapter.py — update it before shipping any audit claim."
    )
    rec = matches[0]
    assert RUNTIME_MODEL in rec["modelId"]
    assert rec["output"]["outputTokenCount"] > 0, (
        "record present but no output tokens — a streamed response body was not assembled"
    )
    assert _by_marker(records, call["marker"]) == matches


@_SKIP
def test_runtime_chat_wire_is_captured_too(calls, records):
    """Both wires, not just Responses: the chat wire is what the OpenAI SDK defaults to."""
    call = calls["rt_chat_stream"]
    assert call["status"] == 200, call["body"]
    assert call["request_id"]
    matches = [r for r in records if r.get("requestId") == call["request_id"]]
    assert len(matches) == 1
    assert RUNTIME_MODEL in matches[0]["modelId"]


@_SKIP
def test_each_invocation_produces_exactly_one_record(calls, records):
    """One record per invocation — not one per SSE event, not one per retry.

    A per-event record would make the log group's volume (and cost) a function of output
    length, and would break any reconciliation that counts records per request id.
    """
    for key in ("rt_resp_sync", "rt_resp_stream", "rt_chat_stream"):
        marker = calls[key]["marker"]
        found = _by_marker(records, marker)
        assert len(found) == 1, f"{key}: expected 1 record for its marker, got {len(found)}"


@_SKIP
def test_records_carry_the_request_body_for_audit(calls, records):
    """Bodies (≤100 KB) are inline in the record — that is what makes the log an audit trail
    rather than a metering duplicate of ``usage_logs``."""
    rec = _by_marker(records, calls["rt_resp_sync"]["marker"])[0]
    body_json = json.dumps(rec["input"])
    assert calls["rt_resp_sync"]["marker"] in body_json
    assert rec["operation"], "no operation field — cannot tell Responses from ChatCompletions"


# ── Mantle plane: NOT captured (negative control) ─────────────────────────────
@_SKIP
def test_mantle_produces_no_record_on_either_wire(calls, records):
    """The documented reason ``usage_logs.bedrock_request_id`` is NULL for Mantle rows.

    Checked by CONTENT as well as by id: Mantle does return an ``x-amzn-requestid``, but it
    is an OpenAI-style ``req_...`` value that joins to nothing, so an id-only check could
    not distinguish "not logged" from "logged under a different key".
    """
    for key in ("mantle_sync", "mantle_stream"):
        call = calls[key]
        assert call["status"] == 200, f"{key}: Mantle call failed, control is void: {call['body']}"
        assert _by_marker(records, call["marker"]) == [], (
            f"{key}: a log record contains the Mantle marker — Mantle IS being captured now. "
            "The NULL bedrock_request_id for Mantle rows is then a real gap, not a property; "
            "the adapter docstring and the reconcile endpoint's `skipped_null` reasoning both "
            "have to change."
        )
        if call["request_id"]:
            assert [r for r in records if r.get("requestId") == call["request_id"]] == []


# ── Region isolation (the only scoping lever) ─────────────────────────────────
@_SKIP
def test_seoul_logging_stays_untouched(logging_enabled):
    """Enabling us-east-2 must not enable ap-northeast-2.

    Logging has no per-model scope, so a config in Seoul would capture the prompt bodies of
    every Claude call from every app in prod. Region isolation is the whole containment
    story, and this asserts it held.
    """
    prov = logging_enabled["prov"]
    assert prov.get_logging_config(SEOUL) == logging_enabled["seoul_pre"], (
        "ap-northeast-2 logging configuration CHANGED during this test — prod Claude request "
        "bodies may now be captured. Disable it immediately."
    )
