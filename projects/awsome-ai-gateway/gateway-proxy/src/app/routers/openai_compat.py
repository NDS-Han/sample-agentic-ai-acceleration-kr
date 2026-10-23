# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTasks
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from app.schemas.domain import ProviderType, TokenUsage
from app.schemas.responses import ModelObject, ModelPricingObject, ModelsListResponse
from app.services.body_log_records import (
    build_body_record_for_nonstream,
    build_body_record_for_stream,
    model_alias_of,
    provider_name,
    resolve_body_logger,
)
from app.services.fallback_loop import (
    release_reservations,
    unsettled_reservation_guard,
)
from app.services.router_service import (
    ModelInactiveError,
    RouterService,
    apply_model_account_route,
    candidate_route_region,
)
from app.routers.bedrock import _rewrite_model_id_for_region
from app.services.streaming import openai_sse_stream, usage_status_of

logger = structlog.get_logger(__name__)

router = APIRouter()
_router_service = RouterService()

# Routing-profile `backend` values whose client may use /v1/responses.
#   mantle         — the original Codex → Bedrock Mantle path (bearer, openai.gpt-5.x).
#   bedrock_openai — runtime plane only (SigV4 + CRIS ids), for a client with no Mantle
#                    entitlement at all.
# Which PLANE a given request goes out on is decided by the resolved model row's provider,
# not by this value: a `mantle` client that asks for a BEDROCK_RUNTIME_OPENAI alias is
# served on the runtime plane. This gate only answers "is /v1/responses serviceable for
# this client at all", which is why both values are accepted and `invoke` (Bedrock native,
# e.g. claude-code) still is not.
_RESPONSES_BACKENDS = frozenset({"mantle", "bedrock_openai"})


async def _log_dispatch_error(
    request: Request,
    redis,
    session_factory,
    *,
    request_id: str,
    model_config,
    client,
    body: bytes,
    auth_context,
    is_streaming: bool,
) -> None:
    """어댑터 계약 밖 예외로 재전파되는 요청도 본문 레코드를 남긴다(R16) —
    성공/비-2xx 경로만 로깅하면 "요청 1건 = 레코드 1건" 계약이 예외 경로에서
    깨진다. 상류 응답이 없으므로 본문은 비우고 status 는 게이트웨이 관점 502.
    """
    bl = await resolve_body_logger(request.app.state, redis, session_factory)
    if bl is None:
        return
    await bl.enqueue(
        build_body_record_for_nonstream(
            request_id=request_id,
            provider=provider_name(model_config),
            client=client,
            model_alias=model_alias_of(model_config),
            status_code=502,
            request_body=body,
            response_body=b"",
            is_streaming=is_streaming,
            user_id=auth_context.user_id if auth_context else None,
            team_id=auth_context.team_id if auth_context else None,
            sso_subject=auth_context.sso_subject if auth_context else None,
        )
    )


@router.get("/v1/models")
async def list_models(request: Request) -> JSONResponse:
    state = request.scope.get("state", {})
    redis = state.get("_redis_effective", state.get("_redis"))
    session_factory = state.get("_session_factory")

    # DB degraded 시 죽은 풀에 붙지 않는다 — 캐시 히트면 서빙, 아니면 빈 리스트
    # (list_active_models(db=None) 계약). 빈 목록은 degraded dataplane 과 정합:
    # /v1/messages 해소도 503 이라 실제로 서빙 가능한 모델이 없다.
    from app.schemas.domain import DegradationLevel

    dm = state.get("_degradation_manager")
    is_db_degraded = dm and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    )

    if not is_db_degraded and session_factory is not None:
        async with session_factory() as db:
            models = await _router_service.list_active_models(redis, db)
    else:
        models = await _router_service.list_active_models(redis, None)
    data = []
    for m in models:
        pricing_obj = None
        if m.pricing and (m.pricing.input_per_1k or m.pricing.output_per_1k):
            pricing_obj = ModelPricingObject(
                input_per_1k_usd=m.pricing.input_per_1k,
                output_per_1k_usd=m.pricing.output_per_1k,
            )
        model_id = m.alias or m.provider_model_id
        data.append(
            ModelObject(
                id=model_id,
                display_name=m.description or model_id,
                created_at=m.created_at.isoformat() if m.created_at else None,
                created=int(m.created_at.timestamp()) if m.created_at else 0,
                provider=m.provider.value,
                api_format=m.api_format.value,
                provider_model_id=m.provider_model_id,
                description=m.description,
                pricing=pricing_obj,
            )
        )
    response = ModelsListResponse(
        data=data,
        first_id=data[0].id if data else None,
        last_id=data[-1].id if data else None,
    )
    return JSONResponse(content=response.model_dump(mode="json"))


@router.get("/v1/models/{model_id:path}")
async def get_model(model_id: str, request: Request) -> JSONResponse:
    """Anthropic 호환 single-model detail endpoint.

    Claude Code 클라이언트는 세션 시작 시 `/v1/models/{id}` 를 호출해서 모델
    가용성을 검증함. 이 엔드포인트가 없으면 (404/401) 모델을 사용 불가로 판단하고
    `/v1/messages` 호출을 아예 시작하지 않음.
    """
    state = request.scope.get("state", {})
    redis = state.get("_redis_effective", state.get("_redis"))
    session_factory = state.get("_session_factory")

    from app.schemas.domain import DegradationLevel

    dm = state.get("_degradation_manager")
    is_db_degraded = dm and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    )

    if not is_db_degraded and session_factory is not None:
        async with session_factory() as db:
            models = await _router_service.list_active_models(redis, db)
    else:
        models = await _router_service.list_active_models(redis, None)
    match = next((m for m in models if (m.alias or m.provider_model_id) == model_id), None)
    if match is None:
        return JSONResponse(
            status_code=404,
            content={
                "type": "error",
                "error": {"type": "not_found_error", "message": f"Model '{model_id}' not found"},
            },
        )

    pricing_obj = None
    if match.pricing and (match.pricing.input_per_1k or match.pricing.output_per_1k):
        pricing_obj = ModelPricingObject(
            input_per_1k_usd=match.pricing.input_per_1k,
            output_per_1k_usd=match.pricing.output_per_1k,
        )
    resolved_id = match.alias or match.provider_model_id
    obj = ModelObject(
        id=resolved_id,
        display_name=match.description or resolved_id,
        created_at=match.created_at.isoformat() if match.created_at else None,
        created=int(match.created_at.timestamp()) if match.created_at else 0,
        provider=match.provider.value,
        api_format=match.api_format.value,
        provider_model_id=match.provider_model_id,
        description=match.description,
        pricing=pricing_obj,
    )
    return JSONResponse(content=obj.model_dump(mode="json"))


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> StreamingResponse | JSONResponse:
    return await _handle_openai(request, "/v1/chat/completions")


@router.post("/v1/completions", response_model=None)
async def completions(request: Request) -> StreamingResponse | JSONResponse:
    return await _handle_openai(request, "/v1/completions")


@router.post("/v1/responses", response_model=None)
async def responses(request: Request) -> StreamingResponse | JSONResponse:
    """OpenAI **Responses API** endpoint — Codex -> Bedrock (Mantle or runtime plane).

    Routing-profile-driven: a request whose identified client has a Responses-capable
    routing profile (`mantle` or `bedrock_openai`, e.g. codex) is dispatched to the
    adapter that matches the RESOLVED MODEL's provider — BEDROCK_MANTLE_OPENAI (bearer,
    openai.gpt-5.x) or BEDROCK_RUNTIME_OPENAI (SigV4, us./global. CRIS) — using the
    profile's account + the client's model, falling back to the profile's default_model.
    Both planes speak the same Responses dialect, so everything downstream of the adapter
    (SSE re-framing, usage parsing, costing) is shared.
    """
    return await _handle_responses(request)


async def _handle_openai(request: Request, path: str):
    state = request.scope.get("state", {})
    auth_context = state.get("auth_context")
    client = state.get("client")
    redis = state.get("_redis_effective", state.get("_redis"))
    session_factory = state.get("_session_factory")
    registry = request.app.state.provider_registry
    cost_recorder = request.app.state.cost_recorder
    request_id = state.get("request_id", "")
    start_time = state.get("request_start_time", time.monotonic())

    body = await request.body()

    # 모델 alias 추출
    import json

    try:
        req_data = json.loads(body)
        alias = req_data.get("model", "")
        is_stream = req_data.get("stream", False)
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request", "message": "Invalid JSON"}},
        )

    from app.schemas.domain import DegradationLevel

    dm = state.get("_degradation_manager")
    is_db_degraded = dm and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    )

    try:
        if not is_db_degraded and session_factory is not None:
            async with session_factory() as db:
                model_config = await _router_service.resolve_openai_model(redis, db, alias)
        else:
            model_config = await _router_service.resolve_openai_model(redis, None, alias)
    except LookupError as e:
        return JSONResponse(
            status_code=404, content={"error": {"type": "not_found", "message": str(e)}}
        )

    state["model_config"] = model_config

    # Key Scope 검사
    if auth_context:
        try:
            _router_service.check_key_scope(auth_context, model_config)
            # 모델 × 앱 축(migration 0035). 위 게이트(사용자 × 모델)와 AND 로 걸린다.
            # allowed_clients: None=제한 없음 / []=어떤 앱도 불가 / 목록=그 앱만.
            _router_service.check_client_model_scope(model_config, state.get("client"))
        except PermissionError:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"Your account does not have access to model '{model_config.alias}'. Contact your administrator to request access.",
                    }
                },
            )

    # Which plane serves this Chat request is decided by the RESOLVED MODEL, not by the
    # path: OPENMODEL → in-house vLLM (default, unchanged), BEDROCK_RUNTIME_OPENAI →
    # bedrock-runtime's OpenAI Chat endpoint (SigV4 + CRIS model id, GPT-5.6). The two
    # adapters take different kwargs — vLLM is addressed by `path` against a fixed base
    # URL, the Bedrock one by the alias's own endpoint + a wire selector — so the call
    # kwargs are built here once and reused by both the streaming and non-streaming call.
    is_bedrock_runtime = model_config.provider == ProviderType.BEDROCK_RUNTIME_OPENAI
    if is_bedrock_runtime and path != "/v1/chat/completions":
        # The runtime plane serves /openai/v1/chat/completions and
        # /openai/v1/responses only — there is no legacy /v1/completions. Refusing is
        # correct: silently rerouting a completions request to the chat wire would
        # return a response shape the client cannot parse.
        # ⚠️ enforce_rate_limits **전**에 거절해야 한다 — 예약을 잡은 뒤 여기서
        #    404 를 내면 해제 경로가 없어 RPM/TPM/CPM·월예산 마커가 새고,
        #    월예산 누수분은 카운터에 영구 잔류한다.
        return JSONResponse(
            status_code=404,
            content={"error": {"type": "not_found",
                               "message": f"Model '{model_config.alias}' does not support {path}"}},
        )

    # Pre-reserve RPM + TPM (3-scope: USER/TEAM/GLOBAL)
    if auth_context:
        from app.services.rate_limit_enforcement import enforce_rate_limits

        rejected = await enforce_rate_limits(
            redis=redis,
            auth_context=auth_context,
            model_config=model_config,
            body=req_data if isinstance(req_data, dict) else {},
            state=state,
            request_id=request_id,
            budget_status=state.get("budget_status"),
            metrics=getattr(request.app.state, "metrics", None),
        )
        if rejected is not None:
            return rejected

    if is_bedrock_runtime:
        adapter = registry.get(ProviderType.BEDROCK_RUNTIME_OPENAI)
        # The routing profile only supplies the cross-account role here (the signing
        # region comes from the alias endpoint). Absent profile/loader → None → the
        # signer uses the pod's own IRSA identity, which is the in-account case.
        routing_loader = getattr(request.app.state, "routing_profile_loader", None)
        profile = None
        if routing_loader is not None:
            try:
                if not is_db_degraded and session_factory is not None:
                    async with session_factory() as db:
                        profile = await routing_loader.load(redis, db, client)
                else:
                    profile = await routing_loader.load(redis, None, client)
            except Exception:
                # A profile is optional on this path; failing to load one must not turn a
                # servable in-account request into a 500.
                logger.warning("chat_routing_profile_load_failed", client=client)
        # 모델 레벨 라우팅(0049) — alias 행의 ARN 이 프로필보다 우선한다.
        # 프로필이 없는 경로에서도 모델 ARN 만으로 크로스계정이 성립한다.
        profile = apply_model_account_route(profile, model_config, client=client)
        invoke_kwargs = {"profile": profile, "endpoint": model_config.endpoint, "wire": "chat"}
        # Rewrite the outgoing model id to the CRIS inference-profile id. Unlike the vLLM
        # adapter (which rewrites `model` itself), this adapter must send the body bytes
        # verbatim — the SigV4 signature covers them — so the substitution happens here,
        # before signing. Bedrock rejects our aliases; it only knows us./global. ids.
        if isinstance(req_data, dict):
            # CRIS 접두는 대상 **리전** 계약이다 — 모델 레벨 라우팅(또는 invoke
            # 프로필)이 pmid 와 다른 패밀리의 리전을 겨냥하면 서명된 본문의
            # model id 도 그 패밀리로 재작성해야 한다. 라우팅 리전이 없으면
            # 종전처럼 저장된 pmid 를 그대로 보낸다.
            _route_region = candidate_route_region(model_config, profile)
            req_data["model"] = (
                _rewrite_model_id_for_region(
                    model_config.provider_model_id, region=_route_region
                )
                if _route_region
                else model_config.provider_model_id
            )
            if is_stream:
                # vLLM(openmodel_adapter._build_request_body)과 같은 강제 삽입 —
                # OpenAI 스펙상 include_usage 미설정 스트림은 usage 프레임을 보내지
                # 않아 스트리밍 호출의 입력 토큰이 전량 미청구된다(R20 M3).
                opts = req_data.get("stream_options")
                if not isinstance(opts, dict):
                    # 클라이언트가 null/스칼라를 보낸 경우 — setdefault 는 None 을
                    # 그대로 두어 주입이 건너뛰어졌다(R20 round-2).
                    opts = {}
                    req_data["stream_options"] = opts
                if not opts.get("include_usage"):
                    opts["include_usage"] = True
            body = json.dumps(req_data).encode()
    else:
        adapter = registry.get(ProviderType.OPENMODEL)
        invoke_kwargs = {"path": path}
    tokenizer = getattr(request.app.state, "tokenizer", None)

    if is_stream:
        try:
            if is_bedrock_runtime:
                # 4-tuple (…, aws_request_id) — the join key to the Bedrock model-invocation
                # log for this call, persisted as usage_logs.bedrock_request_id below.
                status, chunk_iter, headers, aws_request_id = await adapter.invoke_stream(
                    body, model_config.provider_model_id, **invoke_kwargs
                )
            else:
                # vLLM is a 3-tuple and has no AWS request id — there is no Bedrock
                # invocation-log record to join to, so None is the truthful value.
                status, chunk_iter, headers = await adapter.invoke_stream(
                    body, model_config.provider_model_id, **invoke_kwargs
                )
                aws_request_id = None
        except Exception:
            # 어댑터 계약 밖 예외 — 폴백 루프의 catch-all unwind 와 같은 이유로
            # 해제 후 재전파(E2E S4 — 누수만 막고 에러 의미론 500 은 유지).
            await release_reservations(redis=redis, state=state, auth_context=auth_context)
            await _log_dispatch_error(
                request, redis, session_factory,
                request_id=request_id, model_config=model_config, client=client,
                body=body, auth_context=auth_context, is_streaming=True,
            )
            raise

        # KI-08: OpenAI path는 tiktoken(cl100k_base) 근사로 출력 토큰 역산.
        async def _estimate(text: str) -> int | None:
            if not tokenizer:
                return None
            return await tokenizer.estimate_output_tokens(
                text,
                provider=model_config.provider,
                model_id=model_config.provider_model_id,
            )

        # 요청 종결 상태 — 스트림 헬퍼가 채우고 _record 가 usage_logs.status 로
        # 옮긴다(E2E S5). 비-2xx 오류 제너레이터는 http_status 인자가 잡는다.
        outcome: dict = {}
        state["_stream_outcome"] = outcome

        async def _record(usage: TokenUsage, first_token_time: float | None) -> None:
            if not auth_context:
                return
            # ⚠️ rate_limit_state 는 **호출 시점**에 pop 한다 — 위에서 잡아 둔 지역
            #    변수를 쓰면 비-2xx 스트림에서 이중 환불이 된다(E2E S3-H1): 상류 오류 시
            #    release_reservations 가 환불하고 state 를 pop 하는데, 오류 청크를 다
            #    소비한 제너레이터가 정상 종료 경로로 on_usage(0토큰) 를 발화해 예전 dict
            #    로 settle 이 또 돌았다(settle_tpm/settle_cost 는 마커가 없어 멱등이 아님).
            #    pop 은 그 반대 방향도 막는다 — 정산된 뒤 어떤 경로가 release 를 다시
            #    호출해도 비어 있어 no-op 이다(E2E S4 — finalize 가 pop 하지 않으므로
            #    정산 주체가 pop 해야 release_reservations 주석의 전제가 성립한다).
            _rl_state = state.pop("rate_limit_state", None)
            duration_ms = int((time.monotonic() - start_time) * 1000)
            if first_token_time is not None:
                ttft_ms = int((first_token_time - start_time) * 1000)
            else:
                ttft_ms = duration_ms
            await cost_recorder.finalize(
                redis,
                auth_context,
                model_config,
                usage,
                request_id,
                True,
                duration_ms,
                ttft_ms=ttft_ms,
                rate_limit_state=_rl_state,
                downgraded_from=state.get("downgraded_from"),
                bedrock_request_id=aws_request_id,
                client=client,
                status=usage_status_of(outcome, status),
            )

        # ⚠️ 상류가 비-2xx 면 스트림을 클라이언트에게 넘기기 **전에** 되돌린다.
        #    release_reservations 는 state["rate_limit_state"] 를 pop 하므로 이후
        #    on_usage 발화 시 위 _record 는 None 을 보고 정산을 건너뛴다.
        if not (200 <= status < 300):
            await release_reservations(redis=redis, state=state, auth_context=auth_context)

        async def _log_body_stream(
                sse_text: str, log_status: str, truncated: bool = False
            ) -> None:
            """스트림 종료 시 본문을 큐에 넣는다. 절대 블로킹하지 않는다(enqueue 만)."""
            bl = getattr(request.app.state, "body_logger", None)
            if bl is None:
                return
            await bl.enqueue(
                build_body_record_for_stream(
                    request_id=request_id,
                    provider=provider_name(model_config),
                    client=client,
                    model_alias=model_alias_of(model_config),
                    status=log_status,
                    request_body=body,
                    sse_text=sse_text,
                    truncated=truncated,
                    user_id=auth_context.user_id if auth_context else None,
                    team_id=auth_context.team_id if auth_context else None,
                    sso_subject=auth_context.sso_subject if auth_context else None,
                    bedrock_request_id=aws_request_id,
                )
            )

        # ⚠️ **사전** 게이팅. on_complete 를 넘기면 제너레이터가 SSE 프레임 전문을 메모리에
        #    누적하므로(services/streaming.py), 스트림이 시작되기 **전에** 판정해야 한다.
        _on_complete = (
            _log_body_stream
            if await resolve_body_logger(request.app.state, redis, session_factory)
            else None
        )

        return StreamingResponse(
            openai_sse_stream(
                request,
                chunk_iter,
                on_usage=_record,
                tokenizer_hook=_estimate,
                on_complete=_on_complete,
                outcome=outcome,
            ),
            status_code=status,
            media_type="text/event-stream",
            background=unsettled_reservation_guard(state),
            # nginx ingress 버퍼링으로 SSE 가 막히지 않게(E2E S4).
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        try:
            status, response_body, resp_headers, usage = await adapter.invoke(
                body, model_config.provider_model_id, **invoke_kwargs
            )
        except Exception:
            # 스트림 분기와 같은 이유 — 계약 밖 예외는 해제 후 재전파(E2E S4).
            await release_reservations(redis=redis, state=state, auth_context=auth_context)
            await _log_dispatch_error(
                request, redis, session_factory,
                request_id=request_id, model_config=model_config, client=client,
                body=body, auth_context=auth_context, is_streaming=False,
            )
            raise
        if auth_context and usage.total_tokens > 0:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await cost_recorder.finalize(
                redis,
                auth_context,
                model_config,
                usage,
                request_id,
                False,
                duration_ms,
                ttft_ms=duration_ms,
                # pop — 정산 완료 표시(E2E S4).
                rate_limit_state=state.pop("rate_limit_state", None),
                downgraded_from=state.get("downgraded_from"),
                # Both streaming and non-streaming are captured by Bedrock invocation
                # logging (measured 2026-09-03 — one record per invocation either way), so
                # this row is joinable exactly like the streaming branch above.
                # vLLM returns {} here → None → column stays NULL (nothing to join to).
                bedrock_request_id=(resp_headers or {}).get("x-amzn-requestid"),
                client=client,
                status=usage_status_of(None, status),
            )
        elif auth_context and status >= 500:
            # 상류 5xx — usage_logs ERROR/TIMEOUT 행으로 monitoring 에 남긴다.
            # finalize 의 zero-usage 분기가 예약 해제도 겸한다(E2E S5).
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await cost_recorder.finalize(
                redis, auth_context, model_config, TokenUsage(),
                request_id, False, duration_ms,
                ttft_ms=duration_ms,
                rate_limit_state=state.pop("rate_limit_state", None),
                client=client,
                status=usage_status_of(None, status),
            )
        else:
            # ⚠️ finalize 를 타지 않는 경로다(상류 4xx, 또는 usage 가 비어 온 응답).
            #    그러면 enforce_rate_limits 가 잡아 둔 TPM/CPM/CPH 예약을 되돌리는 곳이
            #    아무 데도 없다 — 이 라우트에는 폴백 루프가 없어 그쪽 unwind 도 안 돈다.
            #    400 을 연속으로 받은 사용자가 실제 지출 0 으로 자기 한도를 소진했다.
            await release_reservations(redis=redis, state=state, auth_context=auth_context)

        # 본문 로깅(성공 **및** 오류). 위 cost_recorder 블록과 달리 토큰 수를 조건으로
        # 걸지 않는다 — 조사에 필요한 것은 오히려 실패한 요청의 본문이고, 실패한 호출은
        # usage 가 0 이라 그 조건을 달면 정확히 필요한 레코드만 사라진다.
        body_logger = await resolve_body_logger(request.app.state, redis, session_factory)
        if body_logger is not None:
            await body_logger.enqueue(
                build_body_record_for_nonstream(
                    request_id=request_id,
                    provider=provider_name(model_config),
                    client=client,
                    model_alias=model_alias_of(model_config),
                    status_code=status,
                    request_body=body,
                    response_body=response_body,
                    is_streaming=False,
                    user_id=auth_context.user_id if auth_context else None,
                    team_id=auth_context.team_id if auth_context else None,
                    sso_subject=auth_context.sso_subject if auth_context else None,
                    bedrock_request_id=(resp_headers or {}).get("x-amzn-requestid"),
                )
            )

        try:
            content = json.loads(response_body)
        except Exception:
            content = {"error": {"type": "provider_error", "message": "Invalid response"}}
        return JSONResponse(status_code=status, content=content)


async def _handle_responses(request: Request):
    """Routing-profile-driven OpenAI Responses handler (Codex -> Mantle **or** runtime).

    Two-step resolution: the routing profile decides whether this client may use
    /v1/responses at all (_RESPONSES_BACKENDS), and the resolved model row's provider
    decides which Bedrock plane serves it. Auth/key-scope/rate-limit/cost mirror the chat
    path; budget enforcement already ran in middleware (path now registered).
    """
    import json

    state = request.scope.get("state", {})
    auth_context = state.get("auth_context")
    client = state.get("client")
    redis = state.get("_redis_effective", state.get("_redis"))
    session_factory = state.get("_session_factory")
    registry = request.app.state.provider_registry
    cost_recorder = request.app.state.cost_recorder
    request_id = state.get("request_id", "")
    start_time = state.get("request_start_time", time.monotonic())
    routing_loader = getattr(request.app.state, "routing_profile_loader", None)

    body = await request.body()
    try:
        req_data = json.loads(body)
        requested_alias = req_data.get("model", "")
        is_stream = bool(req_data.get("stream", False))
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request", "message": "Invalid JSON"}},
        )

    from app.schemas.domain import DegradationLevel

    dm = state.get("_degradation_manager")
    is_db_degraded = dm and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    )

    # Load the client's routing profile. Codex must have a mantle profile with a
    # default_model; without it (or wrong client), /v1/responses is not serviceable
    # here (we do NOT silently fall back to vLLM).
    async def _load_profile(db):
        if routing_loader is None:
            return None
        return await routing_loader.load(redis, db, client)

    # Per-request model selection (added with the GPT-5.6 Sol/Terra/Luna aliases).
    #
    # Before this, the profile's default_model was the ONLY reachable model on this
    # route, so registering three GPT-5.6 aliases would have left all three
    # unreachable: a client sending {"model": "codex-gpt-5.6-sol"} still got the
    # default. Now the client's own model wins WHEN it resolves to an ACTIVE alias on
    # either OpenAI-Responses plane (BEDROCK_MANTLE_OPENAI or BEDROCK_RUNTIME_OPENAI) —
    # which is also how one client reaches both planes by model name alone.
    #
    # Falling back (rather than 404ing) on an unresolvable value is what makes this
    # change regression-free for existing clients: Codex always sends SOME model id,
    # typically an upstream OpenAI name like "gpt-5.5-codex" that is not one of our
    # aliases. Before this change that value was ignored; a strict lookup would turn
    # every such request into a 404. The default_model path below is therefore reached
    # by exactly the requests that reached it before.
    #
    # This is NOT an entitlement bypass: whatever is resolved goes through
    # check_key_scope() below, which matches against {alias, provider_model_id}. A key
    # scoped to codex-gpt cannot reach a GPT-5.6 alias by asking for it.
    #
    # KNOWN COST, accepted: a model name we do not register costs 2 extra indexed
    # SELECTs (alias match, then provider_model_id match) before the fallback, on every
    # such request -- failed lookups are not negatively cached, and a Codex client sends
    # the same unregistered name every time. Measured: exactly 2 queries per miss.
    # Accepted rather than optimised because the alternative (caching negative lookups)
    # would delay a newly-registered alias becoming reachable, and an operator adding a
    # model expects it to work immediately. If this ever shows up in DB load, negatively
    # cache the miss with a TTL well under MODEL_CACHE_TTL rather than reordering these
    # lookups -- and note the cost lands only on clients sending unregistered names.
    def _is_usable_model_ref(value) -> bool:
        """True only for a value we can safely hand to the resolver.

        "model" comes straight from untrusted client JSON, so it is not necessarily a
        usable string, and an unusable one must not reach the resolver: the resolver's
        failures below are caught as LookupError, but these two failure modes are NOT
        LookupError, so they would escape the fallback and 500 a request that returned
        200 before this feature existed.

          * non-str (number/list/dict/bool) → asyncpg gets a non-text bind for a VARCHAR
            comparison and raises DBAPIError. Verified against real Postgres with
            123 / ["a"] / {"x":1} / True.
          * lone UTF-16 surrogate (e.g. {"model": "\\ud800"}) → valid JSON and a real str,
            but redis-py encodes cache keys with encoding_errors="strict", so
            redis.get(f"model:{ref}") raises UnicodeEncodeError before any DB call.
            Verified directly against redis-py's Encoder.
          * NUL byte (e.g. {"model": "codex\\u0000gpt"}) → valid JSON, a real str, and
            encodes to UTF-8 cleanly, so neither check above catches it. Postgres has no
            NUL in text: asyncpg raises CharacterNotInRepertoireError ("invalid byte
            sequence for encoding UTF8: 0x00"). Verified against real Postgres.

        All are rejected here rather than caught below, so the profile default serves
        them exactly as it did before per-request selection.

        Deliberately NOT rejected: an over-long value. Postgres compares
        `alias = $1` by VALUE, not by the column's declared width, so a 100 000-char ref
        simply matches nothing and falls back (verified: HTTP 200). Length is bounded
        for LOGGING only, at the log call below.
        """
        if not isinstance(value, str) or not value or "\x00" in value:
            return False
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True

    async def _resolve_model(db, profile):
        """Resolve the client-sent model, falling back to the profile default."""
        if _is_usable_model_ref(requested_alias) and requested_alias != profile.default_model:
            try:
                return await _router_service.resolve_codex_model(redis, db, requested_alias)
            except ModelInactiveError:
                # Do NOT fall back. status=INACTIVE is an operator kill switch: an alias
                # that exists but was deliberately disabled must be refused, not quietly
                # served as some other model (which would also bill the wrong alias).
                # Propagates as a 404 via the LookupError handler below.
                raise
            except (LookupError, DBAPIError) as e:
                # 인프라 장애(연결 끊김·타임아웃·드라이버 단절)는 "해석 불가"가 아니다 —
                # profile.default_model 로 조용히 대체하면 엉뚱한 모델을 서빙하고 그
                # alias 로 청구한다. 아래의 outer except 가 503 으로 매핑한다.
                if isinstance(e, DBAPIError) and (
                    e.connection_invalidated
                    or isinstance(e, (OperationalError, InterfaceError))
                ):
                    raise
                # The alias is genuinely not ours (unknown name, or a different
                # provider's alias) → the pre-existing behaviour: profile default.
                #
                # DBAPIError is a deliberate backstop, not a substitute for the guard
                # above: "model" is untrusted client JSON, and a driver-level rejection
                # of some value we did not anticipate must degrade to the default (the
                # pre-feature behaviour) rather than 500 a request that used to succeed.
                # It is caught HERE only — around the *requested* model. The default
                # model's own lookup below is left to propagate, because a broken
                # default is a real server fault and must not be hidden.
                #
                # INFO, not DEBUG: this is a silent model substitution, so it has to be
                # visible at the default log level to be diagnosable. Truncated to the
                # alias column's own width (128): the value is unbounded client input,
                # and logging it verbatim turns one request into a multi-MB log line,
                # while nothing legitimate is ever longer than the column allows.
                logger.info(
                    "responses_requested_model_unresolved_using_default",
                    requested=requested_alias[:128],
                    default_model=profile.default_model,
                    client=client,
                )
        return await _router_service.resolve_codex_model(redis, db, profile.default_model)

    try:
        if not is_db_degraded and session_factory is not None:
            async with session_factory() as db:
                profile = await _load_profile(db)
                if (
                    profile is None
                    or profile.backend not in _RESPONSES_BACKENDS
                    or not profile.default_model
                ):
                    return JSONResponse(
                        status_code=404,
                        content={"error": {"type": "not_found",
                                            "message": "No Responses backend for this client"}},
                    )
                model_config = await _resolve_model(db, profile)
        else:
            profile = await _load_profile(None)
            if (
                profile is None
                or profile.backend not in _RESPONSES_BACKENDS
                or not profile.default_model
            ):
                return JSONResponse(
                    status_code=404,
                    content={"error": {"type": "not_found",
                                        "message": "No Responses backend for this client"}},
                )
            model_config = await _resolve_model(None, profile)
    except LookupError as e:
        return JSONResponse(
            status_code=404, content={"error": {"type": "not_found", "message": str(e)}}
        )
    except DBAPIError:
        # 헬스체커가 열화를 마킹하기 전 미드요청 DB 단절 — 500 이 아니라 503.
        logger.exception("responses_model_resolution_db_failed", client=client)
        return JSONResponse(
            status_code=503,
            content={"error": {"type": "service_unavailable",
                                "message": "Model resolution backend unavailable"}},
        )

    state["model_config"] = model_config

    # ⚠️ 강등 미들웨어가 재작성한 대상이 codex 평면에서 해소되지 않으면
    #    _resolve_model 이 profile.default_model 로 조용히 대체한다 — 실제로
    #    일어나지 않은 강등이 usage_logs/downgraded_from 에 기록되고, 더 비싼
    #    default 로 돌아가도 "강등 성공"으로 집계됐다(E2E S3-M5-4). 서빙된 모델이
    #    재작성 대상(=requested_alias, alias 또는 pmid 로 비교)이 아니면 플래그를 지운다.
    if state.get("downgraded_from") and requested_alias not in (
        model_config.alias,
        model_config.provider_model_id,
    ):
        logger.info(
            "downgrade_reverted_default_substituted",
            downgraded_from=state.get("downgraded_from"),
            requested=requested_alias[:128],
            served=model_config.alias,
        )
        state.pop("downgraded_from", None)

    # Key scope (model allow-list) — the entitlement gate (trust axis).
    if auth_context:
        try:
            _router_service.check_key_scope(auth_context, model_config)
            # 모델 × 앱 축(migration 0035). 위 게이트(사용자 × 모델)와 AND 로 걸린다.
            # allowed_clients: None=제한 없음 / []=어떤 앱도 불가 / 목록=그 앱만.
            _router_service.check_client_model_scope(model_config, state.get("client"))
        except PermissionError:
            return JSONResponse(
                status_code=400,
                content={"error": {"type": "invalid_request_error",
                                    "message": f"Your account does not have access to model '{model_config.alias}'. Contact your administrator to request access."}},
            )

    # Pre-reserve RPM + TPM (USER/TEAM/GLOBAL) — same enforcement as chat path.
    if auth_context:
        from app.services.rate_limit_enforcement import enforce_rate_limits
        from app.services.web_search_loop import conservative_admission_body

        _admission_body = req_data if isinstance(req_data, dict) else {}
        # R3-5: web_search 가 켜진 프로파일의 /v1/responses 도 다중 턴 상한으로
        #   예약한다. 이 경로는 심사가 웹서치 루프 진입 전에 돌아 원본 바디만
        #   예약됐고, 루프가 최대 max_iterations 턴을 돌며 한도의 최대 N 배까지
        #   지출할 수 있었다. messages.py /v1/messages 와 같은 헬퍼·같은 값.
        if (
            getattr(request.app.state, "agentcore_mcp_client", None) is not None
            and getattr(profile, "web_search_enabled", False)
            and isinstance(req_data, dict)
        ):
            from app.config import get_settings as _gs

            _s = _gs()
            _admission_body = conservative_admission_body(
                req_data, _s.web_search_max_iterations,
                max_result_chars=_s.web_search_max_result_chars,
                max_searches_per_turn=_s.web_search_max_searches_per_turn,
                default_max_results=_s.web_search_max_results_default,
                # Responses 스티처는 soft/nudge 턴이 없다 — 검색 턴 + hard final 이
                # 실제 상한이다(Anthropic 의 iters+3 과 구분, R20 S-b).
                bound_turns=_s.web_search_max_iterations + 1,
            )

        rejected = await enforce_rate_limits(
            redis=redis,
            auth_context=auth_context,
            model_config=model_config,
            body=_admission_body,
            state=state,
            request_id=request_id,
            budget_status=state.get("budget_status"),
            metrics=getattr(request.app.state, "metrics", None),
        )
        if rejected is not None:
            return rejected

    # PLANE SELECTION — by the resolved model row, not by the routing profile.
    # BEDROCK_MANTLE_OPENAI → Mantle bearer plane; BEDROCK_RUNTIME_OPENAI → bedrock-runtime
    # SigV4 plane with a CRIS model id. resolve_codex_model() accepts exactly these two
    # (OPENAI_RESPONSES_PROVIDERS), so registry.get() cannot land on an unrelated adapter,
    # and both take the same (profile, endpoint) kwargs plus a wire selector — the runtime
    # adapter serves two wires and needs to be told which; the Mantle adapter only serves
    # /v1/responses and absorbs the kwarg. One dict therefore drives every call below,
    # including the web-search loop's per-turn invokes.
    adapter = registry.get(model_config.provider)
    # 모델 레벨 라우팅(0049) — alias 행의 ARN 이 프로필보다 우선한다.
    # Mantle plane 의 broker 도 profile.account_role_arn 을 읽으므로 두 평면
    # 모두 이 한 줄로 커버된다.
    profile = apply_model_account_route(profile, model_config, client=client)
    invoke_kwargs = {
        "profile": profile,
        "endpoint": model_config.endpoint,
        "wire": "responses",
    }

    # Rewrite the outgoing model id to the resolved provider_model_id (e.g. openai.gpt-5.5,
    # openai.gpt-5.6-terra on Mantle, us.openai.gpt-5.6-terra on the runtime plane), so the
    # alias the client sent (codex-gpt-5.6-terra) — or the profile default, when the client
    # sent something we do not recognise — maps to the real model. Neither plane accepts
    # our aliases; the runtime plane additionally requires the CRIS prefix, which is why it
    # is stored in provider_model_id rather than added here.
    if isinstance(req_data, dict):
        # runtime 평면의 CRIS 접두는 대상 리전 계약 — 모델 레벨 라우팅(0049)이
        # 다른 패밀리의 리전을 겨냥하면 재작성. 라우팅 리전이 없으면 저장된
        # pmid 그대로(종전 계약 유지).
        _route_region = candidate_route_region(model_config, profile)
        req_data["model"] = (
            _rewrite_model_id_for_region(
                model_config.provider_model_id, region=_route_region
            )
            if _route_region
            else model_config.provider_model_id
        )
        body = json.dumps(req_data).encode()

    # KI-08 역산용 토크나이저 훅. 이 방언은 usage 가 종결 이벤트(response.completed)
    # 안에만 있어서, 상류가 그 전에 끊기면 역산 없이는 usage 가 전부 0 이 되고
    # cost_recorder 가 usage_logs 행을 아예 만들지 않는다.
    #
    # ⚠️ 정의가 **웹서치 분기보다 앞**에 있어야 한다. 그 분기는 아래 `if is_stream:` 보다
    #    먼저 리턴하므로, 정의를 그 블록 안에 두면 웹서치 경로에서 UnboundLocalError 가
    #    된다(실측으로 잡았다 — "함수 안에 정의돼 있다" 는 AST 검사로는 안 잡힌다).
    _tokenizer = getattr(request.app.state, "tokenizer", None)

    async def _estimate(text: str) -> int | None:
        if not _tokenizer:
            return None
        return await _tokenizer.estimate_output_tokens(
            text,
            provider=model_config.provider,
            model_id=model_config.provider_model_id,
        )

    # --- Server-side web search (Architecture C) — opt-in per routing profile ---
    # When the codex profile enables web search AND the AgentCore MCP client is present,
    # run the Responses-dialect tool-use loop instead of the plain single dispatch below.
    # Skipped otherwise → the existing dispatch is byte-identical (zero regression).
    mcp_client = getattr(request.app.state, "agentcore_mcp_client", None)
    if (
        mcp_client is not None
        and profile is not None
        and getattr(profile, "web_search_enabled", False)
        and isinstance(req_data, dict)
    ):
        from app.config import get_settings
        from app.services.web_search_loop import run_web_search_loop

        _pmid = model_config.provider_model_id

        # 회로 게이트 — messages.py /v1/messages 웹서치 분기와 같은 계약. 회로가
        # 열려 있으면(다른 표면의 트래픽이 같은 pmid 에서 연 경우 포함) 프로브를
        # 따낸 요청만 상류를 두드리고, 턴 결과를 회로에 반영한다.
        _cb = getattr(request.app.state, "circuit_breaker", None)
        if not inspect.iscoroutinefunction(getattr(_cb, "is_open", None)):
            _cb = None  # 미배선(또는 테스트 MagicMock) — 게이트 생략
        _ws_cb_key: str | None = _pmid if _cb is not None else None
        _ws_probe_token: str | None = None
        _ws_probe_used = False
        if _ws_cb_key is not None and await _cb.is_open(redis, _ws_cb_key):
            _ws_probe_token = f"ws:{request_id}"
            if not await _cb.try_acquire_halfopen_probe(
                    redis, _ws_cb_key, token=_ws_probe_token):
                _ws_probe_token = None
                logger.info("web_search_skipped_circuit_open", pmid=_pmid)
                # 입장심사 예약(위 enforce_rate_limits)은 이미 잡혀 있다 — 상류를
                # 호출하지 않고 돌려보내므로 여기서 되돌린다.
                await release_reservations(redis=redis, state=state, auth_context=auth_context)
                return JSONResponse(
                    status_code=503,
                    content={"error": {"type": "service_unavailable",
                                       "message": "Model temporarily unavailable"}},
                )

        async def _ws_cb_record(status: int, *, conn_error: bool = False) -> None:
            nonlocal _ws_probe_used, _ws_probe_token
            if _ws_cb_key is None:
                return
            _ws_probe_used = True  # 실제 상류 호출 결과 — 슬롯은 소비된 것이다.
            try:
                if 200 <= status < 300:
                    # 토큰은 첫 성공 기록과 함께 소비 — 다음 턴의 성공이 사이에
                    # 재오픈된 회로를 지우지 못하게(R22 r3 S1).
                    _token, _ws_probe_token = _ws_probe_token, None
                    await _cb.record_success(
                        redis, _ws_cb_key, probe_token=_token
                    )
                elif status in (502, 503) or conn_error:
                    _ws_opened = await _cb.record_failure(redis, _ws_cb_key)
                    if _ws_opened:
                        _dn = getattr(
                            request.app.state, "degradation_notifier", None
                        )
                        if _dn is not None:
                            _dn.notify_provider_error(
                                circuit_key=_ws_cb_key,
                                pmid=_pmid,
                                status=status,
                            )
            except Exception:
                logger.warning("ws_cb_record_failed")

        async def _ws_invoke(turn_body: dict) -> tuple[int, bytes, dict, TokenUsage]:
            tb = dict(turn_body)
            tb["model"] = _pmid
            try:
                result = await adapter.invoke(json.dumps(tb).encode(), _pmid, **invoke_kwargs)
            except Exception:
                await _ws_cb_record(503, conn_error=True)
                raise
            await _ws_cb_record(result[0])
            return result

        async def _ws_invoke_stream(turn_body: dict):
            tb = dict(turn_body)
            tb["model"] = _pmid
            try:
                result = await adapter.invoke_stream(
                    json.dumps(tb).encode(), _pmid, **invoke_kwargs
                )
            except Exception:
                await _ws_cb_record(503, conn_error=True)
                raise
            await _ws_cb_record(result[0])
            return result

        # 클라이언트 체감 TTFT — 루프의 on_usage 는 1-arg(멀티턴 합산이라 per-turn
        # TTFT 를 안 넘긴다)라 ttft_ms 가 항상 NULL 로 기록돼 모니터링에 "ttft=-" 로
        # 나왔다. 응답 이터레이터의 첫 청크 시각을 잡아 기록한다.
        _ws_first_frame: dict[str, float | None] = {"t": None}
        # 요청 종결 상태 — 루프가 채우고 _ws_record 가 usage_logs.status 로 옮긴다(E2E S5).
        _ws_outcome: dict = {}
        state["_stream_outcome"] = _ws_outcome

        async def _ws_record(usage: TokenUsage) -> None:
            if not auth_context:
                return
            duration_ms = int((time.monotonic() - start_time) * 1000)
            ft = _ws_first_frame["t"]
            ttft_ms = int((ft - start_time) * 1000) if ft is not None else duration_ms
            # bedrock_request_id is deliberately NOT set on the web-search path, and left
            # NULL. The loop makes N Bedrock invocations (one per tool-use turn) whose
            # usage is summed into ONE usage_logs row, so no single x-amzn-requestid is
            # the join key: recording one of them would make an invocation-log
            # reconciliation report a 1:1 match while the true relationship is 1:N, i.e.
            # it would claim "0 discrepancies" while silently comparing our N-turn total
            # against a single turn's record. NULL is the honest value — the reconciler
            # skips these rows instead of mis-matching them. Web search is opt-in per
            # routing profile, so this affects only profiles that enabled it.
            await cost_recorder.finalize(
                redis, auth_context, model_config, usage, request_id, is_stream, duration_ms,
                ttft_ms=ttft_ms,
                # 호출 시점 pop — messages.py 의 _ws_record 와 같은 계약(E2E S4).
                rate_limit_state=state.pop("rate_limit_state", None),
                downgraded_from=state.get("downgraded_from"), client=client,
                status=usage_status_of(_ws_outcome),
            )

        # 본문 로깅 — 웹서치 경로 전용 훅. 이 경로는 아래 `if is_stream:` 블록보다 먼저
        # 리턴하므로, 여기 배선하지 않으면 웹서치를 켠 프로파일의 본문이 조용히 미기록된다.
        # Mantle 은 AWS invocation log 에도 남지 않으므로 그 조합에서는 본문의 정본이
        # 어디에도 없게 된다.
        #
        # ⚠️ bedrock_request_id 는 여기서 의도적으로 None 이다 — 위 `_ws_record` 주석과
        #    같은 이유다. 루프는 턴마다 별개의 Bedrock 호출을 하므로 단일 요청 id 가
        #    이 레코드의 조인 키가 되지 못한다. 하나를 골라 넣으면 대조 리포트가 1:N 을
        #    1:1 로 오판한다.
        async def _ws_log_stream(
                sse_text: str, log_status: str, truncated: bool = False
            ) -> None:
            bl = getattr(request.app.state, "body_logger", None)
            if bl is None:
                return
            await bl.enqueue(
                build_body_record_for_stream(
                    request_id=request_id,
                    provider=provider_name(model_config),
                    client=client,
                    model_alias=model_alias_of(model_config),
                    status=log_status,
                    request_body=body,
                    sse_text=sse_text,
                    truncated=truncated,
                    user_id=auth_context.user_id if auth_context else None,
                    team_id=auth_context.team_id if auth_context else None,
                    sso_subject=auth_context.sso_subject if auth_context else None,
                    bedrock_request_id=None,
                )
            )

        async def _ws_log_nonstream(resp_status: int, resp_body: bytes) -> None:
            bl = getattr(request.app.state, "body_logger", None)
            if bl is None:
                return
            await bl.enqueue(
                build_body_record_for_nonstream(
                    request_id=request_id,
                    provider=provider_name(model_config),
                    client=client,
                    model_alias=model_alias_of(model_config),
                    status_code=resp_status,
                    request_body=body,
                    response_body=resp_body,
                    is_streaming=False,
                    user_id=auth_context.user_id if auth_context else None,
                    team_id=auth_context.team_id if auth_context else None,
                    sso_subject=auth_context.sso_subject if auth_context else None,
                    bedrock_request_id=None,
                )
            )

        # 사전 게이팅 — 훅을 넘기면 루프가 SSE 전문을 누적한다.
        # 프로브를 딴 뒤 첫 상류 호출 전에 죽으면 창의 유일한 슬롯이 탄다 —
        # _ws_cb_record 가 한 번도 안 돌았으면 슬롯을 돌려준다(R20 round-2 N1).
        try:
            _ws_logging = await resolve_body_logger(request.app.state, redis, session_factory)

            _settings_ws = get_settings()
            _ws_resp = await run_web_search_loop(
                dialect="responses",
                invoke=_ws_invoke,
                invoke_stream=_ws_invoke_stream,
                initial_req_data=req_data,
                is_stream=is_stream,
                mcp_client=mcp_client,
                request=request,
                on_usage=_ws_record,
                max_iterations=_settings_ws.web_search_max_iterations,
                total_deadline_sec=_settings_ws.web_search_total_deadline_sec,
                default_max_results=_settings_ws.web_search_max_results_default,
                max_result_chars=_settings_ws.web_search_max_result_chars,
                max_searches_per_turn=_settings_ws.web_search_max_searches_per_turn,
                cache_results=_settings_ws.web_search_cache_results,
                result_text_chars=_settings_ws.web_search_result_text_chars,
                handshake_timeout=_settings_ws.agentcore_handshake_timeout,
                tokenizer_hook=_estimate,
                on_stream_complete=_ws_log_stream if _ws_logging else None,
                on_nonstream_complete=_ws_log_nonstream if _ws_logging else None,
                outcome=_ws_outcome,
            )
        except Exception:
            if _ws_probe_token is not None and not _ws_probe_used:
                await _cb.release_halfopen_probe(redis, _ws_cb_key, _ws_probe_token)
            raise
        # 스티처가 삼킨 예외로 상류를 한 번도 못 두드린 정상 반환 — 프로브 슬롯
        # 반환(R20 N-4, messages.py 와 같은 계약).
        if _ws_probe_token is not None and not _ws_probe_used:
            await _cb.release_halfopen_probe(redis, _ws_cb_key, _ws_probe_token)
        # messages.py 의 웹서치 경로와 동일 — 첫 yield 시각을 _ws_record 가 ttft_ms 로
        # 쓰게 한다. 비스트리밍은 래핑 대상이 없어 duration_ms 폴백.
        if isinstance(_ws_resp, StreamingResponse):
            _ws_body_iter = _ws_resp.body_iterator

            async def _ws_timed_iter():
                # aclosing 없이 async for 하면 클라이언트 끊김 시 내부 generator 가
                # GC 에 맡겨져, 과금 finalize 가 들어 있는 finally 가 늦게/안 도는
                # 경우가 있다 — 명시적으로 닫아 과금 누락을 막는다.
                async with contextlib.aclosing(_ws_body_iter) as it:
                    async for chunk in it:
                        if _ws_first_frame["t"] is None:
                            _ws_first_frame["t"] = time.monotonic()
                        yield chunk

            _ws_resp.body_iterator = _ws_timed_iter()
            # spec<2.4 '미시작 제너레이터' disconnect 안전망 — messages.py 의 같은
            # 분기처럼, 스트림이 한 번도 시작되지 못한 채 끊기면 예약(최대
            # max_iterations×max_output)이 마커 TTL 까지 남는다.
            # 스티처-클로즈 태스크가 guard 보다 먼저 돌아야 drain_pending → 실비
            # 정산이 성립한다(S-1, messages.py 와 같은 계약) — 덮어쓰지 말고 합성.
            # 패스스루/MCP-실패 경로는 background=None 이라 걸러야 TypeError 가
            # 안 난다(R20 round-4 M-1).
            _ws_resp.background = BackgroundTasks(tasks=[
                t for t in (_ws_resp.background,
                            unsettled_reservation_guard(state)) if t is not None
            ])
        return _ws_resp

    if is_stream:
        # 4th element = x-amzn-requestid. Populated on the runtime plane (the join key to
        # the Bedrock model-invocation log record); always None on Mantle, which AWS does
        # not capture in invocation logging at all — so a NULL column here is a true
        # statement about the plane, not a lost value.
        try:
            status, chunk_iter, headers, aws_request_id = await adapter.invoke_stream(
                body, model_config.provider_model_id, **invoke_kwargs
            )
        except Exception:
            # 계약 밖 예외 — 해제 후 재전파(E2E S4).
            await release_reservations(redis=redis, state=state, auth_context=auth_context)
            await _log_dispatch_error(
                request, redis, session_factory,
                request_id=request_id, model_config=model_config, client=client,
                body=body, auth_context=auth_context, is_streaming=True,
            )
            raise

        # 비-2xx 면 예약을 스트림 전달 전에 되돌린다. release_reservations 가
        # state["rate_limit_state"] 를 pop 하므로, 오류 청크 소비 후 정상 종료 경로로
        # 발화하는 on_usage 가 예전 dict 로 settle 을 다시 돌리는 이중 환불을 막는다
        # (E2E S3-H1 — _handle_openai 의 같은 블록 주석 참조).
        if not (200 <= status < 300):
            await release_reservations(redis=redis, state=state, auth_context=auth_context)
            # 오류 제너레이터를 SSE 로 내보내면 OpenAI SDK 가 비-2xx 본문을 JSON
            # 오류로 파싱하지 못한다 — JSON 응답으로 환원한다(messages.py 동일).
            err_payload = bytearray()
            try:
                async for _err_chunk in chunk_iter:
                    err_payload += _err_chunk
            except Exception:
                pass
            try:
                err_content = json.loads(bytes(err_payload))
            except (json.JSONDecodeError, ValueError):
                err_content = {"error": {"type": "provider_error",
                                         "message": f"Backend returned {status}"}}
            # 예전에는 오류 제너레이터가 스트림 헬퍼를 타며 _record 가 ERROR 행을
            # 남겼다 — JSON 환원으로 그 기록이 빠지면 안 된다(모니터링 정합).
            # 4xx 거절은 게이트웨이/상류 실패가 아니라 기록하지 않는다.
            if auth_context and status >= 500:
                duration_ms = int((time.monotonic() - start_time) * 1000)
                await cost_recorder.finalize(
                    redis, auth_context, model_config, TokenUsage(),
                    request_id, True, duration_ms,
                    ttft_ms=duration_ms,
                    rate_limit_state=state.pop("rate_limit_state", None),
                    downgraded_from=state.get("downgraded_from"),
                    bedrock_request_id=aws_request_id,
                    client=client,
                    status=usage_status_of(None, status),
                )
            # R16: early JSON 반환도 본문 레코드를 남긴다 — 이 경로가 body-log
            # 배선을 우회해 오류 요청이 감사 없이 지나갔다(messages.py 동일 수정).
            _bl = await resolve_body_logger(request.app.state, redis, session_factory)
            if _bl is not None:
                await _bl.enqueue(
                    build_body_record_for_nonstream(
                        request_id=request_id,
                        provider=provider_name(model_config),
                        client=client,
                        model_alias=model_alias_of(model_config),
                        status_code=status,
                        request_body=body,
                        response_body=bytes(err_payload),
                        is_streaming=True,
                        user_id=auth_context.user_id if auth_context else None,
                        team_id=auth_context.team_id if auth_context else None,
                        sso_subject=auth_context.sso_subject if auth_context else None,
                        bedrock_request_id=aws_request_id,
                    )
                )
            return JSONResponse(status_code=status, content=err_content)

        # 요청 종결 상태 — 스트림 헬퍼가 채우고 _record 가 usage_logs.status 로
        # 옮긴다(E2E S5). 비-2xx 오류 제너레이터는 http_status 인자가 잡는다.
        outcome: dict = {}
        state["_stream_outcome"] = outcome

        async def _record(usage: TokenUsage, first_token_time: float | None) -> None:
            if not auth_context:
                return
            # 호출 시점에 pop — 정산 완료 표시. release 가 먼저 pop 했으면 None
            # (E2E S3-H1), 정산 후 재-release 도 막는다(E2E S4).
            _rl_state = state.pop("rate_limit_state", None)
            duration_ms = int((time.monotonic() - start_time) * 1000)
            if first_token_time is not None:
                ttft_ms = int((first_token_time - start_time) * 1000)
            else:
                ttft_ms = duration_ms
            await cost_recorder.finalize(
                redis, auth_context, model_config, usage, request_id, True, duration_ms,
                ttft_ms=ttft_ms,
                rate_limit_state=_rl_state,
                downgraded_from=state.get("downgraded_from"),
                bedrock_request_id=aws_request_id,
                client=client,
                status=usage_status_of(outcome, status),
            )

        async def _log_body_stream(
                sse_text: str, log_status: str, truncated: bool = False
            ) -> None:
            """스트림 종료 시 본문을 큐에 넣는다. 절대 블로킹하지 않는다(enqueue 만)."""
            bl = getattr(request.app.state, "body_logger", None)
            if bl is None:
                return
            await bl.enqueue(
                build_body_record_for_stream(
                    request_id=request_id,
                    provider=provider_name(model_config),
                    client=client,
                    model_alias=model_alias_of(model_config),
                    status=log_status,
                    request_body=body,
                    sse_text=sse_text,
                    truncated=truncated,
                    user_id=auth_context.user_id if auth_context else None,
                    team_id=auth_context.team_id if auth_context else None,
                    sso_subject=auth_context.sso_subject if auth_context else None,
                    # Mantle 에서는 항상 None 이다(위 주석) — 그리고 바로 그것이 이 경로에
                    # 본문 로깅이 **필요한** 이유다. AWS 쪽에 대조할 레코드가 없으므로 이
                    # 레코드가 유일한 본문 정본이 된다.
                    bedrock_request_id=aws_request_id,
                )
            )

        # ⚠️ **사전** 게이팅. 근거는 _handle_openai 의 같은 주석 참조.
        _on_complete = (
            _log_body_stream
            if await resolve_body_logger(request.app.state, redis, session_factory)
            else None
        )

        from app.services.streaming import responses_sse_stream

        return StreamingResponse(
            responses_sse_stream(
                request,
                chunk_iter,
                on_usage=_record,
                on_complete=_on_complete,
                # KI-08 역산 — 이 방언은 usage 가 종결 이벤트 안에만 있어서, 그 전에
                # 끊기면 역산이 없으면 usage_logs 행이 아예 만들어지지 않는다.
                tokenizer_hook=_estimate,
                outcome=outcome,
            ),
            status_code=status,
            media_type="text/event-stream",
            # nginx ingress 버퍼링으로 SSE 가 막히지 않게(E2E S4).
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            background=unsettled_reservation_guard(state),
        )
    else:
        try:
            status, response_body, resp_headers, usage = await adapter.invoke(
                body, model_config.provider_model_id, **invoke_kwargs
            )
        except Exception:
            # 스트림 분기와 같은 이유 — 계약 밖 예외는 해제 후 재전파(E2E S4).
            await release_reservations(redis=redis, state=state, auth_context=auth_context)
            await _log_dispatch_error(
                request, redis, session_factory,
                request_id=request_id, model_config=model_config, client=client,
                body=body, auth_context=auth_context, is_streaming=False,
            )
            raise
        if auth_context and usage.total_tokens > 0:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await cost_recorder.finalize(
                redis, auth_context, model_config, usage, request_id, False, duration_ms,
                ttft_ms=duration_ms,
                # pop — 정산 완료 표시(E2E S4).
                rate_limit_state=state.pop("rate_limit_state", None),
                downgraded_from=state.get("downgraded_from"),
                bedrock_request_id=(resp_headers or {}).get("x-amzn-requestid"),
                client=client,
                status=usage_status_of(None, status),
            )
        elif auth_context and status >= 500:
            # 상류 5xx — usage_logs ERROR/TIMEOUT 행으로 monitoring 에 남긴다.
            # finalize 의 zero-usage 분기가 예약 해제도 겸한다(E2E S5).
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await cost_recorder.finalize(
                redis, auth_context, model_config, TokenUsage(),
                request_id, False, duration_ms,
                ttft_ms=duration_ms,
                rate_limit_state=state.pop("rate_limit_state", None),
                client=client,
                status=usage_status_of(None, status),
            )
        else:
            # 예약 되돌리기 — 근거는 _handle_openai 의 같은 블록 주석 참조.
            await release_reservations(redis=redis, state=state, auth_context=auth_context)

        # 본문 로깅(성공 **및** 오류). usage 조건을 걸지 않는 이유는 _handle_openai 의
        # 같은 블록 주석 참조.
        body_logger = await resolve_body_logger(request.app.state, redis, session_factory)
        if body_logger is not None:
            await body_logger.enqueue(
                build_body_record_for_nonstream(
                    request_id=request_id,
                    provider=provider_name(model_config),
                    client=client,
                    model_alias=model_alias_of(model_config),
                    status_code=status,
                    request_body=body,
                    response_body=response_body,
                    is_streaming=False,
                    user_id=auth_context.user_id if auth_context else None,
                    team_id=auth_context.team_id if auth_context else None,
                    sso_subject=auth_context.sso_subject if auth_context else None,
                    bedrock_request_id=(resp_headers or {}).get("x-amzn-requestid"),
                )
            )

        try:
            content = json.loads(response_body)
        except Exception:
            content = {"error": {"type": "provider_error", "message": "Invalid response"}}
        return JSONResponse(status_code=status, content=content)
