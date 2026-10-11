# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Router-level rate limit enforcement (pre-reserve/post-settle 배선).

라우터(bedrock.py / openai_compat.py)가 `model_config` 해결 직후 호출.
이 모듈이 다음을 수행:
    1. 요청 body에서 max_tokens/추정 input tokens 추출
    2. 예약 토큰 계산 (`estimate_reserved_tokens`)
    3. 3-스코프 RPM 체크
    4. 3-스코프 TPM 체크 (pre-reserve)
    5. 거부 시 429 JSONResponse 반환 / 통과 시 state에 settle용 정보 주입

Step 5에서 `_load_scope_limits()`를 `model.rate_limit_configs` DB 조회로 교체 예정.
현재는 FR-4.1 배선 검증용 기본값 사용.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal

import structlog
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.observability.provider_metrics import record_rate_limit_hit
from app.schemas.domain import AuthContext, BudgetStatus, DegradationLevel, ModelConfigSchema
from app.schemas.errors import anthropic_error
from app.services.rate_limit_config_loader import (
    AllScopeLimits,
    ScopeLimits,
    load_all_scope_limits,
)
from app.services.rate_limit_scope import (
    ScopeDescriptor,
    build_scope_descriptors,
    estimate_reserved_tokens,
)
from app.services.rate_limit_service import RateLimitService

logger = structlog.get_logger(__name__)

# 모델별 기본 max_output 상한 (Anthropic spec 기준 대체값).
_DEFAULT_MAX_OUTPUT = 4096

# 간단 추정: 평균 bytes per token (Bedrock count_tokens 호출 비용 회피).
# tiktoken/Anthropic tokenizer 반영은 추후 정확도 튜닝 단계에서.
_BYTES_PER_TOKEN = 4


def _extract_max_output(body: dict) -> int:
    """요청 body에서 max output 토큰 추출 (Bedrock/OpenAI/Responses 공통 필드).

    max_output_tokens 는 OpenAI **Responses API**(Codex/GPT-5.x) 필드 — 빠지면
    Codex 요청이 _DEFAULT_MAX_OUTPUT(4096)로 과대 예약돼 거짓 TPM/CPM throttle 유발.
    """
    for key in ("max_tokens", "max_completion_tokens", "max_new_tokens", "max_output_tokens"):
        value = body.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return _DEFAULT_MAX_OUTPUT


def _estimate_input_tokens(body: dict) -> int:
    """body 전체 문자열 길이 기반 보수적 추정.

    정확한 카운팅은 tokenizer 필요하나 reserve는 과대 예약이 허용되므로
    bytes/4 휴리스틱으로 시작. post-settle에서 차액 환불됨.
    """
    try:
        serialized = json.dumps(body, ensure_ascii=False)
    except Exception:
        return _DEFAULT_MAX_OUTPUT
    return max(1, len(serialized.encode("utf-8")) // _BYTES_PER_TOKEN)


def _apply_budget_throttle(
    limits: AllScopeLimits, budget_status: BudgetStatus | None
) -> AllScopeLimits:
    """Budget throttle 활성 시 USER RPM을 설정값에 따라 감경.

    예산 throttle은 **USER 스코프에만** 적용 — 팀/전체는 별도 독립 한도.
    USER RPM 미설정(unlimited)인 경우 throttle 무시 (감경 대상 없음).
    """
    if not (budget_status and budget_status.throttle_active):
        return limits
    if limits.user.rpm is None:
        return limits
    pct = budget_status.throttle_rpm_pct or 50
    limits.user = ScopeLimits(
        rpm=max(1, int(limits.user.rpm * pct / 100)),
        tpm=limits.user.tpm,
    )
    return limits


async def enforce_rate_limits(
    *,
    redis,
    auth_context: AuthContext,
    model_config: ModelConfigSchema,
    body: dict,
    state: dict,
    request_id: str,
    budget_status: BudgetStatus | None = None,
    metrics=None,
) -> JSONResponse | None:
    """라우터용 Pre-reserve RPM + TPM 체크 진입점.

    Returns:
        거부 시 ``JSONResponse(status_code=429)``, 통과 시 ``None``.
        통과한 경우 ``state`` 에 ``rate_limit_state`` 키로 settle 정보 주입:
            - ``tpm_descriptors``: list[ScopeDescriptor]
            - ``tpm_reserved``: int
    """
    if redis is None:
        # Redis 다운 — fail-open (NFR-2.4). Middleware의 in-memory fallback이 처리 중.
        return None

    model_alias = model_config.alias or model_config.provider_model_id

    # DB fallback 가용성 확인 (NFR-2.4 degradation 반영)
    dm = state.get("_degradation_manager")
    is_db_degraded = dm and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    )
    session_factory = state.get("_session_factory")

    # short-lived session: rate limit config 조회 후 즉시 반환.
    if not is_db_degraded and session_factory is not None:
        async with session_factory() as db:
            limits = await load_all_scope_limits(
                redis=redis,
                db=db,
                user_id=auth_context.user_id,
                team_id=auth_context.team_id,
                model_alias=model_alias,
            )
    else:
        limits = await load_all_scope_limits(
            redis=redis,
            db=None,
            user_id=auth_context.user_id,
            team_id=auth_context.team_id,
            model_alias=model_alias,
        )
    limits = _apply_budget_throttle(limits, budget_status)

    descriptors = build_scope_descriptors(
        user_id=auth_context.user_id,
        team_id=auth_context.team_id,
        model_alias=model_alias,
        user_rpm=limits.user.rpm,
        user_tpm=limits.user.tpm,
        team_rpm=limits.team.rpm,
        team_tpm=limits.team.tpm,
        global_rpm=limits.global_.rpm,
        global_tpm=limits.global_.tpm,
    )

    svc = RateLimitService()

    # RPM 체크 (요청 수)
    rpm_result = await svc.check_multi_scope_rpm(
        redis, descriptors, request_id=request_id
    )
    if not rpm_result.allowed:
        return _build_429(rpm_result, metrics)

    # TPM 체크 (Pre-reserve)
    max_output = _extract_max_output(body)
    estimated_input = _estimate_input_tokens(body)
    reserved = estimate_reserved_tokens(
        estimated_input_tokens=estimated_input,
        max_output_tokens=max_output,
    )

    # TPM 커밋 목록 — 비용 단계가 거절하면 이 목록으로 예약을 되돌린다.
    # (check_multi_scope_tpm 내부에서 스코프 간 거절은 이미 해제한다.)
    tpm_committed: list[ScopeDescriptor] = []
    tpm_reserved_at = time.time()
    tpm_result = await svc.check_multi_scope_tpm(
        redis, descriptors, reserved_tokens=reserved, committed_out=tpm_committed
    )
    if not tpm_result.allowed:
        return _build_429(tpm_result, metrics)

    # CPM/CPH 체크 (Pre-reserve, FR-4.6 — USER+TEAM 2 스코프)
    estimated_cost = _estimate_cost(model_config, estimated_input, max_output)
    cost_result = await svc.reserve_cost(
        redis,
        user_id=auth_context.user_id,
        estimated_cost=estimated_cost,
        user_cpm_limit=limits.user.cpm,
        user_cph_limit=limits.user.cph,
        team_id=auth_context.team_id,
        team_cpm_limit=limits.team.cpm,
        team_cph_limit=limits.team.cph,
    )
    if not cost_result.allowed:
        # ⚠️ TPM 예약은 이미 전 스코프에 커밋됐다 — 비용 거절로 요청이 나가면
        #    수만 토큰의 예약이 버킷 만료까지 남아, 거절당한 재시도가 TEAM TPM
        #    카운터를 부풀려 팀 전체를 기아시키는 양의 피드백이 됐다.
        #    rate_limit_state 는 전 단계 통과 후에만 채워지므로 여기서 직접
        #    해제해야 한다. 실패해도 거절은 진행(누수분은 버킷 TTL 이 회수).
        try:
            await svc.settle_tpm(redis, tpm_committed, reserved, 0)
        except Exception:
            logger.warning("tpm_unwind_on_cost_reject_failed")
        return _build_cost_429(cost_result, metrics)

    # 월예산 admission 예약 — budget_check(middleware)는 읽기 전용이라
    # 동시 요청이 같은 used 를 보고 전부 통과해 하드캡을 넘길 수 있었다.
    # 여기서 최악비용을 원자적으로 선예약하고, finalize 의 settle 이 실비로
    # 정산한다(실패/거절 경로는 release_reservations 가 환불).
    from app.periods import request_period
    from app.services.budget_service import BudgetService

    worst_cost = _estimate_worst_cost(model_config, estimated_input, max_output)
    try:
        budget_reservation = await BudgetService().reserve_budget(
            redis,
            str(auth_context.user_id),
            str(auth_context.team_id) if auth_context.team_id else None,
            request_period(),
            worst_cost,
            request_id,
            client=state.get("client"),
            marker_ttl=get_settings().budget_reservation_ttl_seconds,
        )
    except PermissionError as exc:
        # 앞서 커밋된 TPM + 비용 예약을 되돌린다 — 월예산 거절로 요청이 나가도
        # 분/시간 한도를 실비 없이 물고 있으면 같은 기아 피드백이 생긴다.
        try:
            await svc.settle_tpm(redis, tpm_committed, reserved, 0)
        except Exception:
            logger.warning("tpm_unwind_on_budget_reject_failed")
        if cost_result.reserved_cost is not None and cost_result.reserved_cost != Decimal("0"):
            try:
                await svc.settle_cost(
                    redis,
                    user_id=str(auth_context.user_id),
                    actual_cost=Decimal("0"),
                    reserved_cost=cost_result.reserved_cost,
                    team_id=str(auth_context.team_id) if auth_context.team_id else None,
                    committed_scopes=cost_result.committed_scopes,
                    cpm_window_ts=cost_result.cpm_window_ts or None,
                    cph_window_ts=cost_result.cph_window_ts or None,
                )
            except Exception:
                logger.warning("cost_unwind_on_budget_reject_failed")
        return _build_budget_429(str(exc), metrics)
    except Exception:
        # 예약 경로 자체의 장애(스크립트 미로드·직렬화 오류 등)는 요청을 막지
        # 않는다 — 예약 없이 진행하면 기존 체크 전용 동작으로 회귀할 뿐이다.
        logger.exception("budget_reserve_unexpected_failure")
        budget_reservation = []

    # 통과 — settle용 정보 주입. 윈도우/커밋 스코프도 싣는다 — settle 이
    # **예약이 실제로 들어간** 버킷/스코프 키를 치게 하는 근거이다(윈도우 경계를
    # 넘은 스트리밍 요청의 환불이 다음 버킷에 새는 결함 + 무한도 스코프에 TTL
    # 없는 음수 팬텀 키가 생기던 결함).
    state["rate_limit_state"] = {
        "tpm_descriptors": _only_tpm(descriptors),
        "tpm_reserved": reserved,
        "tpm_reserved_at": tpm_reserved_at,
        "cost_reserved": cost_result.reserved_cost,
        "cost_committed_scopes": cost_result.committed_scopes,
        "cost_cpm_window_ts": cost_result.cpm_window_ts,
        "cost_cph_window_ts": cost_result.cph_window_ts,
        "budget_reservation": budget_reservation,
    }
    return None


def _estimate_cost(
    model_config: ModelConfigSchema, estimated_input: int, max_output: int
) -> Decimal:
    """Pre-reserve용 보수적 비용 추정 (USD).

    ``pricing.input_per_1k × input + pricing.output_per_1k × max_output`` 로
    *과대* 예약. post-settle에서 실제 비용과의 차액이 환불됨.
    Cache 단가는 Pre-reserve 단계에서 판정 불가하므로 제외.
    """
    pricing = model_config.pricing
    input_cost = (Decimal(estimated_input) / Decimal(1000)) * pricing.input_per_1k
    output_cost = (Decimal(max_output) / Decimal(1000)) * pricing.output_per_1k
    return (input_cost + output_cost).quantize(Decimal("0.000001"))


def _estimate_worst_cost(
    model_config: ModelConfigSchema, estimated_input: int, max_output: int
) -> Decimal:
    """월예산 선예약용 **최악비용** 추정 (USD).

    ``_estimate_cost`` 와 달리 입력을 단가가 가장 비싼 버킷(1h cache write
    포함)으로 청구되는 경우로 잡는다 — 캐시 쓰기 단가는 입력 단가보다 높아,
    일반 추정으로는 실비가 예약을 초과해 하드캡이 새는 경계가 남는다.
    프롬프트 추정치가 long-context 임계를 넘으면 long 요율을 적용한다.

    잔여 구멍: ``_estimate_input_tokens`` 의 bytes/4 휴리스틱은 다바이트 언어
    프롬프트를 과소 추정할 수 있어 완벽한 상한은 아니다 — 실입력이 추정을
    크게 넘으면 그 차이만큼 한도 초과가 가능하다. 출력(max_tokens)은
    API 강제 상한이라 완전히 덮인다.
    """
    p = model_config.pricing
    long_ctx = (
        p.long_context_threshold_tokens is not None
        and estimated_input > p.long_context_threshold_tokens
    )

    def _rate(short: Decimal | None, long: Decimal | None) -> Decimal:
        if long_ctx and long is not None:
            return long
        return short or Decimal("0")

    # 입력 계열 4개 버킷 중 최고 단가 — 어떤 캐시 형태로 청구돼도 덮이게.
    input_rate = max(
        _rate(p.input_per_1k, p.long_input_per_1k),
        _rate(p.cache_write_per_1k, p.long_cache_write_per_1k),
        _rate(p.cache_write_1h_per_1k, p.long_cache_write_1h_per_1k),
    )
    output_rate = _rate(p.output_per_1k, p.long_output_per_1k)

    cost = (Decimal(estimated_input) / 1000) * input_rate + (
        Decimal(max_output) / 1000
    ) * output_rate
    return cost.quantize(Decimal("0.000001"))


def _build_budget_429(reason: str, metrics=None) -> JSONResponse:
    """월예산 거절 429 — middleware/budget.py 의 에러 어휘와 맞춘다."""
    messages = {
        "no_team_assigned": "No team assigned. Contact your admin.",
        "team_budget_unset": "팀 예산이 설정되지 않았습니다. 관리자에게 문의하세요.",
    }
    record_rate_limit_hit(metrics, scope="budget", limit_type="monthly")
    return JSONResponse(
        status_code=429,
        content=anthropic_error(
            "rate_limit_error",
            messages.get(reason, "Budget limit exceeded."),
            code=reason,
        ),
        headers={"Retry-After": "60"},
    )


def _build_cost_429(result, metrics=None) -> JSONResponse:
    scope = result.scope or "USER"
    limit_type = result.limit_type or "cpm"
    # ⚠️ 429 를 내는 **모든** 경로에서 올려야 한다. 한 경로만 빠지면 그 한도 위반은
    #    지표에서 사라지고, "스로틀 없음" 으로 읽힌다(observability/provider_metrics.py
    #    record_rate_limit_hit 주석 참조).
    record_rate_limit_hit(metrics, scope=scope, limit_type=limit_type)
    code = f"{scope.lower()}_{limit_type}_exceeded"
    retry_after = str(result.retry_after or 60)
    limit_value = str(result.limit) if result.limit is not None else ""
    remaining_value = str(result.remaining) if result.remaining is not None else "0"

    return JSONResponse(
        status_code=429,
        content=anthropic_error(
            "rate_limit_error",
            f"Cost rate limit exceeded at {scope} scope ({limit_type}). "
            f"Please retry after {retry_after} seconds.",
            code=code,
            scope=result.scope,
            limit_type=result.limit_type,
            retry_after=result.retry_after,
        ),
        headers={
            "Retry-After": retry_after,
            "X-RateLimit-Scope": scope,
            "X-RateLimit-Type": limit_type,
            "X-RateLimit-Remaining": remaining_value,
            "X-RateLimit-Limit": limit_value,
        },
    )


def _only_tpm(descriptors: list[ScopeDescriptor]) -> list[ScopeDescriptor]:
    """TPM 한도가 있는 스코프만 필터 (settle 대상 축소)."""
    return [d for d in descriptors if d.tpm_limit and d.tpm_limit > 0]


def _build_429(result, metrics=None) -> JSONResponse:
    scope = result.scope or "USER"
    limit_type = result.limit_type or "rpm"
    record_rate_limit_hit(metrics, scope=scope, limit_type=limit_type)
    code = f"{scope.lower()}_{limit_type}_exceeded"
    retry_after = str(result.retry_after or 60)

    return JSONResponse(
        status_code=429,
        content=anthropic_error(
            "rate_limit_error",
            f"Rate limit exceeded at {scope} scope ({limit_type}). "
            f"Please retry after {retry_after} seconds.",
            code=code,
            scope=result.scope,
            limit_type=result.limit_type,
            retry_after=result.retry_after,
        ),
        headers={
            "Retry-After": retry_after,
            "X-RateLimit-Scope": scope,
            "X-RateLimit-Type": limit_type,
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Limit": str(result.limit),
            "X-RateLimit-Reset": str(result.window_reset),
        },
    )
