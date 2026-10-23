# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Availability fallback loop helper for the /v1/messages route.

Wraps the per-candidate invoke with:
  - Circuit-breaker gating (is_open / half-open probe)
  - Key-scope and rate-limit enforcement per candidate
  - TPM+cost reservation unwind on 5xx/timeout
  - Circuit-breaker record_failure / record_success

Returns a `FallbackResult` that carries enough information for the caller
(messages.py) to build its normal streaming or non-streaming response.

Design contract
---------------
- Only BEDROCK candidates produce a real fallback chain (the FallbackResolver
  only adds same-provider candidates).  A BEDROCK_MANTLE original resolves to
  just [original] because resolve() never crosses provider boundaries.
- The caller must supply `try_order` from FallbackResolver.resolve().
- For STREAMING the initial (status, chunk_iter) is returned BEFORE any bytes
  are yielded to the client, so a bad status can still trigger fallback.
- Mid-stream failures are NOT caught here — they propagate to the client.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog

from app.observability.provider_metrics import (
    build_provider_labels,
    record_provider_error,
    record_provider_request,
)
from app.config import get_settings
from app.providers.base import ATTEMPT_UNSETTLED_HEADER
from app.schemas.domain import ModelConfigSchema, TokenUsage
from app.schemas.errors import anthropic_error
from app.services.circuit_breaker import CircuitBreakerService
from app.services.rate_limit_enforcement import enforce_rate_limits
from app.services.rate_limit_service import RateLimitService
from app.services.router_service import ClientModelScopeError

logger = structlog.get_logger(__name__)

# HTTP status codes that trigger a fallback attempt.
# 429 = provider-side throttling (Bedrock ThrottlingException). Model quotas are
# per-model, so a throttled original can be served by a chained candidate —
# the same "temporary capacity" signal as 502/503. Our own admission 429s never
# reach this check (they return earlier as AdmissionRejection).
_FALLBACK_STATUSES = frozenset({429, 502, 503, 504})
# Among fallback statuses, only these count as a CB failure
# (504 = ModelTimeoutException = per-prompt fault, 429 = transient quota — do
# NOT penalise the circuit for either)
_CB_FAILURE_STATUSES = frozenset({502, 503})


@dataclass
class AdmissionRejection:
    """요청을 상류로 보내기 전에 거절한 결과(스코프 거부 또는 레이트리밋)."""

    status: int
    body: bytes
    headers: dict


def scope_denial_body(candidate_config: Any, *, client_scoped: bool) -> bytes:
    """스코프 거부의 클라이언트 응답 본문.

    ⚠️ 400 이지 403 이 아니다. Anthropic 클라이언트들은 403 을 자격증명 문제로 읽고
       재인증 루프를 돌린다 — 실제로는 모델 접근권이 없는 것이므로 재시도로 풀리지 않는다.

    ⚠️ 두 축의 안내가 달라야 한다. 계정 축(``check_key_scope``)은 "관리자에게 접근 요청",
       앱 축(``check_client_model_scope``)은 "이 앱에서는 못 쓴다" 다. 한 문구로 합치면
       Codex 에서만 막힌 사용자가 계정 권한을 요청하러 가고, 운영자는 이미 권한이 있다고
       답한다 — 아무도 원인을 못 찾는다.
    """
    alias = getattr(candidate_config, "alias", "?")
    if client_scoped:
        message = (
            f"Model '{alias}' is not available to this application. "
            "Your account may still use it from other applications — "
            "ask your administrator to allow this app for this model."
        )
    else:
        message = (
            f"Your account does not have access to model '{alias}'. "
            "Contact your administrator to request access."
        )
    return json.dumps(anthropic_error("invalid_request_error", message)).encode()


async def enforce_candidate_admission(
    *,
    router_service: Any,
    auth_context: Any,
    candidate_config: Any,
    redis: Any,
    req_data: Any,
    state: dict,
    request_id: str,
    budget_status: Any = None,
) -> AdmissionRejection | None:
    """한 후보 모델에 대한 **입장 심사**: 스코프 2축 + 레이트리밋.

    ``None`` 이면 통과. 값이 있으면 그것이 클라이언트에게 돌려줄 거절이다.

    ⚠️ 이 함수가 따로 있는 이유. 이 세 검사는 오래도록 ``run_fallback_loop`` 안에만
       있었고, 그것이 ``/v1/messages`` 의 **유일한** 호출 지점이었다. 그래서 폴백 루프
       앞에서 리턴하는 경로(웹서치 루프)는 세 검사를 전부 건너뛰었다 — 사용자별 모델
       허용목록, 앱별 모델 허용목록, RPM/TPM/비용 한도가 모두 무시됐고, 웹서치를 켠
       프로파일에서만 그랬으므로 증상이 없었다. 호출자가 늘어날 때 이 검사를 복사하지
       않도록 한 곳에 둔다.

    ⚠️ ``auth_context`` 가 없으면(인증 미들웨어를 거치지 않는 내부 호출) 통과시킨다 —
       기존 동작 그대로다. 이 함수가 인증을 대체하지는 않는다.
    """
    if not auth_context:
        return None

    try:
        router_service.check_key_scope(auth_context, candidate_config)
        # 모델 × 앱 축(migration 0035). 위 게이트(사용자 × 모델)와 AND 로 걸린다.
        # allowed_clients: None=제한 없음 / []=어떤 앱도 불가 / 목록=그 앱만.
        router_service.check_client_model_scope(candidate_config, state.get("client"))
    except PermissionError as exc:
        client_scoped = isinstance(exc, ClientModelScopeError)
        logger.info(
            "candidate_scope_denied",
            alias=getattr(candidate_config, "alias", None),
            axis="client_model" if client_scoped else "key",
        )
        return AdmissionRejection(
            status=400,
            body=scope_denial_body(candidate_config, client_scoped=client_scoped),
            headers={},
        )

    # 이전 후보의 예약 상태를 지운다 — enforce 가 새로 쓰도록.
    state.pop("rate_limit_state", None)
    rejected = await enforce_rate_limits(
        redis=redis,
        auth_context=auth_context,
        model_config=candidate_config,
        body=req_data,
        state=state,
        request_id=request_id,
        budget_status=budget_status,
    )
    if rejected is None:
        return None

    # rejected 는 Starlette JSONResponse — body 는 이미 bytes 다.
    raw_body = getattr(rejected, "body", None)
    if isinstance(raw_body, bytes):
        body_bytes = raw_body
    elif raw_body is not None:
        body_bytes = json.dumps(raw_body).encode()
    else:
        body_bytes = b"{}"
    return AdmissionRejection(
        status=rejected.status_code,
        body=body_bytes,
        headers=dict(rejected.headers) if hasattr(rejected, "headers") else {},
    )


@dataclass
class FallbackResult:
    """Outcome of `run_fallback_loop`."""

    status: int
    # Non-streaming: (bytes body, {headers}, TokenUsage)
    # Streaming: (AsyncIterator[bytes], {headers}, str|None request-id)
    payload: tuple[Any, dict, Any]
    model_config: ModelConfigSchema
    availability_fallback_from: str | None = None
    # True when every candidate was circuit-open (no invoke attempted)
    all_open: bool = False


async def release_reservations(
    *,
    redis,
    state: dict,
    auth_context,
) -> None:
    """``enforce_rate_limits`` 가 잡아 둔 TPM + 비용 예약을 되돌린다.

    예약은 요청을 **받아들일 때** 보수적으로(최대 출력 토큰 기준) 미리 깎아 두고, 응답
    뒤에 실제 사용량으로 정산한다. 그래서 정산되지 않은 요청은 사용자의 분/시간 한도를
    **실제로 쓴 적 없는 양만큼** 계속 물고 있게 된다.

    ⚠️ 오랫동안 이 함수는 502/503/504 에서만 불렸다. 그래서 상류가 400·404·422·429·500
       으로 응답하면(잘못된 tool 스키마, 컨텍스트 초과, ValidationException 등) 예약이
       그대로 남았다. 라우터의 ``finalize`` 도 usage 가 0 이라 호출되지 않아, 어느 쪽도
       되돌리지 않았다. 400 을 연속으로 받은 사용자는 **한 푼도 쓰지 않고** 자기 CPM/CPH
       한도를 소진해 그 분/시간이 끝날 때까지 스스로 429 를 맞는다.

    ⚠️ 정산 상태는 읽는 게 아니라 **처음에 pop** 한다 — 이것이 멱등성의 근거다.
       예전엔 ``get`` 으로 읽고 환불 후 맨 끝에 pop 했는데, 그 창 사이에
       ``unsettled_reservation_guard`` 의 데드라인이 끝나며 돌린 늦은 drain 의
       ``finalize`` 가 같은 상태를 pop 해 둘 다 정산할 수 있었다 — TPM/CPM 은
       이중 환불, 월예산은 release(0) 가 이기면 실비가 카운터에 반영 안 된다.
       단일 이벤트 루프에서 pop 은 원자적 소유권 주장이다 — 먼저 pop 한 쪽만
       정산한다.
    """
    if redis is None:
        return  # Redis 없이는 예약도 못 잡았다 — pop 할 상태도 없다
    rls = state.pop("rate_limit_state", None)
    if not rls:
        return

    svc = RateLimitService()

    tpm_descriptors = rls.get("tpm_descriptors", [])
    tpm_reserved = rls.get("tpm_reserved", 0)
    if tpm_descriptors and tpm_reserved > 0:
        try:
            await svc.settle_tpm(
                redis,
                tpm_descriptors,
                tpm_reserved,
                0,
                reserved_at=rls.get("tpm_reserved_at"),
            )
        except Exception:
            logger.warning("fallback_unwind_tpm_failed")

    cost_reserved = rls.get("cost_reserved")
    if cost_reserved is not None and cost_reserved != Decimal("0") and auth_context:
        try:
            await svc.settle_cost(
                redis,
                user_id=str(auth_context.user_id),
                actual_cost=Decimal("0"),
                reserved_cost=cost_reserved,
                team_id=str(auth_context.team_id) if auth_context.team_id else None,
                committed_scopes=rls.get("cost_committed_scopes"),
                cpm_window_ts=rls.get("cost_cpm_window_ts") or None,
                cph_window_ts=rls.get("cost_cph_window_ts") or None,
                committed_keys=rls.get("cost_committed_keys") or None,
            )
        except Exception:
            logger.warning("fallback_unwind_cost_failed")

    # 월예산 admission 예약 환불 — settle(0)은 마커 기반 멱등이라 finalize 와
    # 경쟁해도 한 번만 적용된다. 별도 try 로 분리해 위 환불 실패와 격리한다.
    budget_res = rls.get("budget_reservation")
    if budget_res:
        try:
            from app.services.budget_service import BudgetService

            await BudgetService().settle_budget(redis, budget_res, Decimal("0"))
        except Exception:
            logger.warning("fallback_unwind_budget_failed")


def unsettled_reservation_guard(state: dict) -> "BackgroundTask":
    """spec<2.4 경로 '미시작 제너레이터' disconnect 로 새는 예약의 안전망.

    Starlette 의 spec<2.4 경로는 ``listen_for_disconnect`` 로 끊김을 감지해
    task group 을 취소한다. 클라이언트가 첫 SSE 바이트 전에 끊으면 스트림
    제너레이터가 **한 번도 시작되지 않는다** — 미시작 async generator 는
    ``finally`` 조차 실행하지 않으므로 on_usage → pop + finalize 가 통째로
    건너뛰어지고 TPM/CPM/CPH/월예산 예약이 남는다(마커 TTL 만료까지 resvsum,
    usage 카운터는 무기한 — E 가 영구 부풀어 fail-closed 로 고착).

    ``StreamingResponse.background`` 는 task group 탈출 뒤 **취소 스코프 밖**
    에서 실행되므로 여기서 되돌린다. ``release_reservations`` 는 ``state.pop``
    멱등이라 정상 정산 뒤에는 no-op 이다 — 모든 StreamingResponse 에 달아도
    안전하다.
    """

    async def _guard() -> None:
        # 막 _spawn 된 정산 태스크가 state 를 pop 할 한 틱을 준다 — release(0)
        # 가 먼저 가면 뒤의 settle(actual) 이 마커 부재로 no-op 되어 실제 비용
        # 만큼 카운터가 얇아진다.
        await asyncio.sleep(0)
        if not state.get("rate_limit_state"):
            return
        # disconnect 경로는 백그라운드 drain 태스크가 잔여 청크에서 usage 를
        # 수거한 뒤 정산한다(streamer 가 outcome["drain_pending"] 을 세운다).
        # 한 틱만 재우면 드레인(최대 stream_disconnect_drain_timeout 초)보다
        # 항상 먼저 깨어 release(0) 로 이기고, 뒤의 settle(actual) 은 마커 부재로
        # no-op — 실비 유실 + TPM/CPM 이중 환불이었다. drain 이 살아있는 동안엔
        # 기다리고, 데드라인(드레인 타임아웃+버퍼)까지 정산이 안 끝나면 여기서
        # 되돌린다(드레인 사망/스폰 실패 시 예약 누수 방지).
        outcome = state.get("_stream_outcome")
        if outcome and outcome.get("drain_pending"):
            deadline = (
                time.monotonic()
                + get_settings().stream_disconnect_drain_timeout
                + 10
            )
            while (
                time.monotonic() < deadline
                and outcome.get("drain_pending")
                and state.get("rate_limit_state")
            ):
                await asyncio.sleep(0.25)
        if not state.get("rate_limit_state"):
            return
        redis = state.get("_redis_effective", state.get("_redis"))
        if redis is None:
            # Redis 가 없으면 Lua 예약 자체를 안 잡는다 — 되돌릴 마커도 없다.
            return
        try:
            await release_reservations(
                redis=redis,
                state=state,
                auth_context=state.get("auth_context"),
            )
        except Exception:
            logger.warning("unsettled_reservation_release_failed")

    from starlette.background import BackgroundTask

    return BackgroundTask(_guard)


async def run_fallback_loop(
    *,
    try_order: list[str],
    original_alias: str,
    is_stream: bool,
    req_data: dict,
    redis,
    auth_context,
    state: dict,
    request_id: str,
    budget_status,
    adapter,
    stream_kwargs: dict,
    nonstream_kwargs: dict,
    cb: CircuitBreakerService,
    router_service,
    session_factory,
    is_db_degraded: bool,
    # For haiku thinking-strip detection
    original_model_config: ModelConfigSchema,
    # These callables allow the helper to resolve candidate model configs
    # and rebuild the invoke body for each candidate.
    resolve_model_config,  # async callable(alias) -> ModelConfigSchema
    build_candidate_body,  # callable(req_data, model_config, is_stream) -> (bytes, dict, dict)
    # For Bedrock: _rewrite_model_id_for_region; for Mantle: identity
    # 두 번째 인자로 후보 config 가 온다 — 모델 레벨 라우팅(0049)으로 후보의
    # 대상 리전이 원본과 다를 수 있어 CRIS 접두는 후보별로 계산해야 한다.
    rewrite_model_id,  # callable(provider_model_id, candidate_config=None) -> str
    # 프로바이더 호출 결과 지표(선택). None 이면 아무것도 기록하지 않는다.
    metrics=None,
    # ⚠️ 회로 차원(선택). CB 키가 pmid 뿐이면 cross-account(333) 호출의 장애가
    #    같은 pmid 를 쓰는 in-account 사용자의 회로까지 연다 — 실제 장애 자원은
    #    (모델 × 계정) 이다(E2E S3-L3). 호출자는 계정/엔드포인트를 식별하는
    #    문자열을 넘기고, 없으면 종전과 같은 pmid 단독 키다.
    circuit_scope: str = "",
    # 회로 OPEN 전이 시 notifications:system 의 provider_error 발행자(선택).
    # None 이면 알림만 생략한다 — 추론 경로에는 영향 없음.
    degradation_notifier=None,
    # 후보별 해석 콜백(모델 레벨 라우팅, migration 0049).
    # adapter 가 하나로 고정이면 후보 B 의 호출이 후보 A 의 계정으로 나간다 —
    # alias 행에 ARN 이 있으면 후보마다 계정이 다를 수 있으므로 config 로
    # 어댑터·회로 차원·리전 재작성을 후보별로 계산한다. None = 종전과 같은
    # 단일 어댑터·단일 스코프(프로필 라우팅만 있는 세계와 동일).
    adapter_for=None,
    circuit_scope_for=None,
) -> FallbackResult:
    """Run the fallback loop over `try_order`.

    Each element of `try_order` is a model alias.  The original is always
    try_order[0].  We iterate:
      1. Resolve model_config + pmid
      2. Circuit-breaker gate
      3. check_key_scope (immediate 403 on failure)
      4. enforce_rate_limits (immediate 429 on failure)
      5. Invoke
      6. On 5xx / timeout: unwind reservation, record_failure, continue
      7. On success: record_success, build FallbackResult

    Returns a FallbackResult.  The caller decides how to respond.
    """
    last_error: tuple[int, bytes] | None = None
    any_invoked = False
    # 총 폴백 시간 예산 — 후보별 상류 타임아웃이 누적해 클라이언트/ALB 데드라인을
    # 넘는 것을 막는다. 첫 후보는 항상 시도하고, 이후 후보는 데드라인 전까지만.
    _deadline = time.monotonic() + get_settings().fallback_budget_sec

    for idx, alias in enumerate(try_order):
        is_original = idx == 0
        if not is_original and time.monotonic() >= _deadline:
            logger.info(
                "fallback_budget_exhausted",
                alias=alias,
                budget_sec=get_settings().fallback_budget_sec,
            )
            break

        # --- 1. Resolve model config for this candidate ---
        try:
            if is_original:
                # Already resolved by messages(); re-use to avoid extra DB hit
                candidate_config = original_model_config
            else:
                candidate_config = await resolve_model_config(alias)
        except LookupError as exc:
            logger.warning("fallback_candidate_not_found", alias=alias, error=str(exc))
            continue
        except Exception:
            # DB/Redis blip while resolving a CANDIDATE — skip the slot instead of
            # escaping as a 500 that replaces the original's real error.
            logger.exception("fallback_candidate_resolve_failed", alias=alias)
            continue

        pmid = candidate_config.provider_model_id
        # CB 키 = 회로 차원 + pmid — 같은 pmid 라도 계정이 다르면 회로가 다르다(E2E S3-L3).
        # circuit_scope_for 가 있으면 후보별로 계산한다 — 모델 레벨 라우팅으로
        # 후보마다 계정이 갈리는데 원본의 스코프로 묶으면 다른 계정의 장애가
        # 이 계정 회로를 오염시킨다.
        _cand_scope = (
            circuit_scope_for(candidate_config)
            if circuit_scope_for is not None
            else circuit_scope
        )
        cb_key = f"{_cand_scope}|{pmid}" if _cand_scope else pmid

        # --- 2. Circuit-breaker gate ---
        probe_token: str | None = None
        if await cb.is_open(redis, cb_key):
            probe_token = f"fb:{request_id}:{alias}"
            probe_won = await cb.try_acquire_halfopen_probe(
                redis, cb_key, token=probe_token)
            if not probe_won:
                probe_token = None
                logger.info("fallback_cb_open_skip", alias=alias, pmid=pmid)
                continue  # skip — circuit open, no probe slot
            logger.info("fallback_cb_halfopen_probe", alias=alias, pmid=pmid)

        # --- 3+4. Admission: scope (2 axes) + rate limits ---
        # ⚠️ 이 세 검사는 :func:`enforce_candidate_admission` 한 곳에 있다. 여기 인라인으로
        #    두면 폴백 루프 밖에서 상류를 호출하는 경로(웹서치 루프)가 그대로 우회한다 —
        #    실제로 그런 상태였다. 그 함수의 docstring 에 경위가 있다.
        try:
            rejection = await enforce_candidate_admission(
                router_service=router_service,
                auth_context=auth_context,
                candidate_config=candidate_config,
                redis=redis,
                req_data=req_data,
                state=state,
                request_id=request_id,
                budget_status=budget_status,
            )
        except Exception:
            # 프로브를 딴 뒤 심사가 예외로 죽어도 슬롯을 돌려준다(R20 round-2 N1).
            if probe_token is not None:
                await cb.release_halfopen_probe(redis, cb_key, probe_token)
            raise
        if rejection is not None:
            # 프로브를 따고도 상류를 호출하지 않는 거절 — 슬롯을 돌려준다(R20 N1).
            if probe_token is not None:
                await cb.release_halfopen_probe(redis, cb_key, probe_token)
            if is_original:
                # 원본 후보의 거절은 그대로 클라이언트에게 돌려준다(스코프 400 /
                # 레이트리밋 429). 레이트리밋에는 폴백을 시도하지 않는다 — 다른 모델로
                # 우회하는 것이 곧 한도 우회다.
                return FallbackResult(
                    status=rejection.status,
                    payload=(rejection.body, rejection.headers, TokenUsage()),
                    model_config=candidate_config,
                )
            # 폴백 후보의 거절은 그 슬롯만 건너뛴다 — 원본의 오류가 클라이언트가 볼 것이다.
            logger.info("fallback_candidate_rejected", alias=alias, status=rejection.status)
            continue

        # --- 5. Build invoke body for this candidate ---
        # ⚠️ 예약(위 admission)과 invoke 의 try 사이 구간이다 — 본문 조립이나
        #    지표 코드가 예외를 내면 예약이 해제 없이 새어 마커 TTL 까지 물리고,
        #    usage 카운터에는 추정치가 영구 잔류한다. invoke 와 같은 계약으로
        #    해제 후 재전파한다.
        try:
            invoke_body, cand_stream_kwargs, cand_nonstream_kwargs = build_candidate_body(
                req_data, candidate_config, is_stream
            )

            # ⚠️ 예전에는 여기서 haiku 후보의 `thinking` 을 **무조건 지웠다.** 지금은
            #    build_candidate_body 가 `normalize_thinking` 으로 계열에 맞게 변환하므로 그
            #    strip 은 유해하다: haiku 가 실제로 받는 `{"type":"enabled"}` 까지 지워서,
            #    사용자는 HTTP 200 을 받으면서 extended thinking 만 조용히 사라진다.
            #    변환은 본문을 만드는 단일 지점에서만 한다.

            # 후보별 리전 재작성 — 모델 레벨 라우팅으로 후보의 대상 리전이
            # 원본과 다를 수 있다(CRIS 접두는 리전 계약).
            call_model_id = rewrite_model_id(pmid, candidate_config)

            any_invoked = True

            # ── 이 후보 1건 = 프로바이더 호출 1건 ──
            #
            # 폴백 루프에서는 후보마다 따로 세는 것이 맞다. 요청 단위로 한 번만 세면
            # "원본 모델이 죽어서 폴백이 성공했다" 가 **성공 1건**으로만 보이고, 원본의
            # 실패는 지표에서 사라진다 — 정확히 알아야 할 사실이 그것이다.
            _pm_labels = build_provider_labels(
                model_config=candidate_config,
                client=state.get("client"),
                is_stream=is_stream,
            )
            record_provider_request(metrics, _pm_labels)
        except Exception:
            # 프로브를 딴 채 상류 호출 전에 죽으면 슬롯을 돌려준다(R20 round-2 N1).
            if probe_token is not None:
                await cb.release_halfopen_probe(redis, cb_key, probe_token)
            await release_reservations(
                redis=redis, state=state, auth_context=auth_context
            )
            raise

        # --- 6. Invoke ---
        # 후보별 어댑터 — 이 후보의 alias 에 모델 레벨 ARN 이 있으면 그 계정의
        # 클라이언트로 나가야 한다. 단일 adapter 재사용이면 후보 B 가 A 의 계정으로
        # 호출돼 모델 미존재/오과금이 된다.
        _cand_adapter = (
            adapter_for(candidate_config)
            if adapter_for is not None
            else adapter
        )
        caught_connection_error = False
        try:
            if is_stream:
                status, chunk_iter, headers, aws_request_id = await _cand_adapter.invoke_stream(
                    invoke_body, call_model_id, **cand_stream_kwargs
                )
            else:
                status, response_body, headers, usage = await _cand_adapter.invoke(
                    invoke_body, call_model_id, **cand_nonstream_kwargs
                )
        except (TimeoutError, ConnectionError, OSError) as exc:
            logger.warning(
                "fallback_invoke_connection_error",
                alias=alias,
                error=type(exc).__name__,
            )
            caught_connection_error = True
            record_provider_error(
                metrics, _pm_labels, status=503, error_code=type(exc).__name__
            )
            status = 503  # treat as 503 for unwind/CB purposes
            response_body = json.dumps(
                anthropic_error("connection_error", str(exc))
            ).encode()
            headers = {}
            usage = TokenUsage()
        except Exception:
            # 어댑터 계약 밖의 예외(예: bedrock_openai_adapter 의 ValueError,
            # 본문 조립 버그)는 여기까지 못 오게 돼 있지만, 실제로 새면 루프를
            # 빠져나가 500 이 되고 예약이 마커 TTL 까지 누수된다. 해제 후 재전파 —
            # 에러 의미론(500)은 그대로 두고 누수만 막는다.
            if probe_token is not None:
                await cb.release_halfopen_probe(redis, cb_key, probe_token)
            await release_reservations(
                redis=redis, state=state, auth_context=auth_context
            )
            raise

        # --- 7. Handle result ---
        # ⚠️ 비-2xx 는 폴백 대상이 아니어도 예약을 되돌려야 한다. 상류가 400/404/422/429/
        #    500 을 주면 usage 가 없으므로 라우터의 finalize 가 호출되지 않고, 옛 조건
        #    (502/503/504 만)에서는 어느 쪽도 되돌리지 않아 예약이 창(window) 끝까지
        #    남았다. release_reservations 는 멱등이라 여기서 먼저 불러도 안전하다.
        if not (200 <= status < 300):
            await release_reservations(redis=redis, state=state, auth_context=auth_context)

        # 클라이언트측 타임아웃(어댑터가 ATTEMPT_UNSETTLED 를 단다)은 상류가 요청을
        # 이미 처리·과금했을 수 있다 — 다음 후보로 재시도하면 같은 추론을 두 모델에
        # 이중 청구할 수 있으므로 폴백하지 않고 이 오류를 그대로 돌려준다.
        if headers.get(ATTEMPT_UNSETTLED_HEADER):
            if not caught_connection_error:
                record_provider_error(metrics, _pm_labels, status=status)
            error_bytes = response_body if not is_stream else json.dumps(
                anthropic_error("provider_error", f"Backend returned {status}")
            ).encode()
            return FallbackResult(
                status=status,
                payload=(error_bytes, {}, TokenUsage() if not is_stream else None),
                model_config=candidate_config,
                availability_fallback_from=original_alias if not is_original else None,
            )

        if status in _FALLBACK_STATUSES or caught_connection_error:
            # ⚠️ 연결 오류는 위 except 에서 이미 셌다 — 여기서 또 세면 2배가 된다.
            if not caught_connection_error:
                record_provider_error(metrics, _pm_labels, status=status)
            # CB: record failure only for {502,503} and connection errors, NOT 504
            if status in _CB_FAILURE_STATUSES or caught_connection_error:
                cb_opened = await cb.record_failure(redis, cb_key)
                # 회로가 OPEN 상태(방금 열렸거나 프로브 실패로 재개방) — 관리자
                # 알림의 신호다. 장애 지속 중 요청당 발행은 notifier 의 pod 로컬
                # dedup + worker 쿨다운이 억제한다.
                if cb_opened and degradation_notifier is not None:
                    degradation_notifier.notify_provider_error(
                        circuit_key=cb_key, pmid=pmid, status=status
                    )

            error_bytes = response_body if not is_stream else json.dumps(
                anthropic_error("provider_error", f"Backend returned {status}")
            ).encode()
            last_error = (status, error_bytes)

            logger.info(
                "fallback_candidate_failed",
                alias=alias,
                status=status,
                is_original=is_original,
            )
            continue

        else:
            # Non-fallback status (2xx, 4xx, etc.)
            if 200 <= status < 300:
                await cb.record_success(redis, cb_key, probe_token=probe_token)
            elif not is_original:
                # 후보가 비-폴백 오류(400 ValidationException, 404 잘못된 pmid, 500 등)를
                # 주면 그 슬롯만 건너뛴다 — 그대로 돌려주면 원본의 진짜 오류(예: 503)가
                # 잘못된 후보의 4xx 로 바뀌어, 클라이언트는 요청이 유효했는데
                # invalid_request_error 를 받는다. 원본의 오류가 우선이다.
                # last_error 에도 싣지 않는다 — 원본이 회로개방으로 시도조차 못 한
                # 경우 후보의 4xx 가 유일한 오류로 남아 마스킹이 재발한다.
                logger.info(
                    "fallback_candidate_error_skipped",
                    alias=alias,
                    status=status,
                )
                continue

            availability_fallback_from = original_alias if not is_original else None

            if availability_fallback_from:
                logger.info(
                    "fallback_succeeded",
                    original=original_alias,
                    used=alias,
                    status=status,
                )

            if is_stream:
                return FallbackResult(
                    status=status,
                    payload=(chunk_iter, headers, aws_request_id),
                    model_config=candidate_config,
                    availability_fallback_from=availability_fallback_from,
                )
            else:
                return FallbackResult(
                    status=status,
                    payload=(response_body, headers, usage),
                    model_config=candidate_config,
                    availability_fallback_from=availability_fallback_from,
                )

    # Loop exhausted
    if last_error is None:
        # 아무것도 호출되지 않았거나, 호출된 후보가 전부 스킵된 비-폴백 오류였다
        # (원본은 회로개방으로 시도 못 함). 후보의 오류로 원본 상황을 가리지 않고
        # 일괄 503 을 돌려준다 — all_open 과 같은 계약이라 라우터가 ERROR 행과
        # Retry-After 를 단다.
        return FallbackResult(
            status=503,
            payload=(
                json.dumps(
                    anthropic_error(
                        "service_unavailable",
                        "All fallback models are temporarily unavailable",
                    )
                ).encode(),
                {},
                TokenUsage(),
            ),
            model_config=original_model_config,
            all_open=not any_invoked,
        )

    # All candidates failed with real backend errors — return last error
    last_status, last_body = last_error
    return FallbackResult(
        status=last_status,
        payload=(last_body, {}, TokenUsage() if not is_stream else None),
        model_config=original_model_config,
    )
