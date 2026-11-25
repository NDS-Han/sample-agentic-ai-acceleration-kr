# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""요청 수명 원장 — admission 'start' 이벤트, 행 없는 종료의 'close', 프로세스 생존 신호 (wave 3b,
B2).

문제: 비용 행은 finalize 의 XADD 로만 생긴다. 파드가 SIGKILL/OOM 으로 스트림 도중에 죽으면 finalize
가
없고 행도 없다 — 업스트림은 이미 프롬프트를 처리해 청구했다.

해법(워커가 sweeper 로 메운다):
  * **start** — admission(예약 통과) 직후 ``cost:stream`` 에 ``kind="start"`` 이벤트. 워커가
    ``usage.request_starts`` 에 적는다. 같은 request_id 의 완료(end) 행이 오면 지운다.
  * **close** — 행 없이 끝난 요청(0 토큰 성공, 상류 4xx 로 예약만 되돌린 경로 등)은 요청이 끝날 때
    ``kind="close"`` 를 보낸다. 이것이 없으면 sweeper 가 그 요청에 추정 행을 만든다.
  * **생존 신호** — 프로세스마다 ``cost:live:<instance_id>`` 를 주기적으로 갱신한다(TTL). sweeper 는
    "완료 없이 오래된 시작" 중 **그 프로세스의 신호가 끊긴 것**만 추정 행으로 쓴다 — 살아 있는
    파드의
    긴 스트림(수십 분 extended thinking)을 잘못 쓸어 담지 않는다.

요청 경계: OTel 미들웨어가 요청마다 ``RequestLedger`` 를 ContextVar 로 묶는다. 스트림 드레인처럼
요청 뒤까지 사는 태스크는 ``streaming._spawn`` 이 ``hold()`` 로 붙잡는다 — 요청 처리가 끝나고 붙잡은
태스크가 모두 끝났는데 end 를 보내지 않았으면 그때 close 를 보낸다.

발행 경로는 finalize 의 end 와 같다(``CostRecorder.publish_event`` — XADD 실패 시 spool → DB 폴백).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import structlog

logger = structlog.get_logger(__name__)

#: 이 프로세스(uvicorn 워커)의 식별자. 재시작마다 바뀐다 — 죽은 프로세스의 시작은 그 신호와 함께
#: 끊긴다.
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
HEARTBEAT_PREFIX = "cost:live:"

START_SCHEMA_VERSION = 4

_current: ContextVar[RequestLedger | None] = ContextVar("cost_request_ledger", default=None)


class _Config:
    recorder = None          # CostRecorder — publish_event
    redis = None             # raw client
    degradation = None       # DegradationManager — 열화면 XADD 없이 바로 spool
    enabled = False
    metrics = None


CONFIG = _Config()
#: close 발행 태스크(강한 참조) — 종료 시 drain_tasks 가 기다린다.
_TASKS: set[asyncio.Task] = set()


def configure(*, recorder, redis, degradation_manager=None, enabled: bool = True,
              metrics=None) -> None:
    CONFIG.recorder = recorder
    CONFIG.redis = redis
    CONFIG.degradation = degradation_manager
    CONFIG.enabled = bool(enabled)
    CONFIG.metrics = metrics


def reset() -> None:
    """테스트용 — 전역 설정을 지운다."""
    CONFIG.recorder = None
    CONFIG.redis = None
    CONFIG.degradation = None
    CONFIG.enabled = False
    CONFIG.metrics = None


def _effective_redis():
    dm = CONFIG.degradation
    if dm is not None:
        from app.schemas.domain import DegradationLevel

        if dm.level in (DegradationLevel.REDIS_DEGRADED, DegradationLevel.BOTH_DEGRADED):
            return None
    return CONFIG.redis


class RequestLedger:
    """요청 하나의 수명 상태(프로세스 메모리). 단일 이벤트 루프라 잠금이 필요 없다."""

    __slots__ = ("request_id", "path", "started", "ended", "closed", "holds", "done")

    def __init__(self, request_id: str, path: str = "") -> None:
        self.request_id = request_id
        self.path = path
        self.started = False   # start 를 한 번 이상 발행했다
        self.ended = False     # end(비용 행) 를 발행했다 — close 가 필요 없다
        self.closed = False    # close 를 발행했다(또는 발행 중)
        self.holds = 0         # 요청 뒤까지 사는 태스크 수(_spawn)
        self.done = False      # 요청 처리(ASGI 호출)가 끝났다

    def _maybe_close(self) -> None:
        if not (self.done and self.holds <= 0 and self.started) or self.ended or self.closed:
            return
        self.closed = True
        try:
            t = asyncio.get_running_loop().create_task(_publish_close(self.request_id))
        except RuntimeError:
            logger.warning("cost_close_spawn_loop_closed", request_id=self.request_id)
            return
        _TASKS.add(t)
        t.add_done_callback(_TASKS.discard)


def bind(request_id: str, path: str = "") -> Token:
    """요청 시작 — OTel 미들웨어가 부른다."""
    return _current.set(RequestLedger(request_id, path))


def request_done(token: Token) -> None:
    """요청 처리 끝(미들웨어 finally). 붙잡은 태스크가 없으면 필요 시 close 를 보낸다."""
    led = _current.get()
    try:
        _current.reset(token)
    except ValueError:
        pass  # 다른 컨텍스트에서 불림 — 상태 표시만 한다
    if led is not None:
        led.done = True
        led._maybe_close()


def current() -> RequestLedger | None:
    return _current.get()


def hold():
    """요청 뒤까지 사는 태스크가 요청을 붙잡는다.

    반환된 함수를 태스크 종료 시 부른다(요청 경계가 없으면 None).
    """
    led = _current.get()
    if led is None or led.closed:
        return None
    led.holds += 1
    released = False

    def _release(*_a) -> None:
        nonlocal released
        if released:
            return
        released = True
        led.holds -= 1
        led._maybe_close()

    return _release


def mark_ended(request_id: str) -> None:
    """finalize 가 비용 행(end)을 발행했다 — 이 요청에는 close 가 필요 없다."""
    led = _current.get()
    if led is not None and (not request_id or led.request_id == request_id):
        led.ended = True


async def _publish(payload: dict, request_id: str, kind: str) -> None:
    rec = CONFIG.recorder
    if rec is None:
        return
    try:
        await rec.publish_event(_effective_redis(), json.dumps(payload, separators=(",", ":")),
                                request_id=request_id)
        m = CONFIG.metrics
        if m is not None and hasattr(m, "cost_lifecycle_events_total"):
            m.cost_lifecycle_events_total.add(1, {"kind": kind})
    except Exception:
        logger.exception("cost_lifecycle_publish_failed", kind=kind, request_id=request_id)


async def _publish_close(request_id: str) -> None:
    await _publish({
        "kind": "close", "request_id": request_id, "instance_id": INSTANCE_ID,
        "closed_at": datetime.now(UTC).isoformat(), "schema_version": START_SCHEMA_VERSION,
    }, request_id, "close")


async def publish_start(*, state: dict, auth_context, model_config, request_id: str,
                        body: dict | None = None) -> None:
    """admission 통과 직후 — 라우터용 ``enforce_rate_limits`` 래퍼가 부른다.

    폴백 루프는 후보마다 다시 admission 하므로 여러 번 불릴 수 있다 — 매번 보낸다(워커는 열린 시작의
    페이로드를 마지막 것으로 덮어 sweeper 가 **현재** 후보의 예약을 정산하게 한다).
    실패는 요청을 막지 않는다(로그만).
    """
    if not CONFIG.enabled or not request_id or auth_context is None:
        return
    led = _current.get()
    # 요청 경계(미들웨어)가 없으면 close 를 보낼 주체도 없다 — 시작도 보내지 않는다(행 없는 종료를
    # 추정 행으로 오인하는 쪽보다 낫다). 운영 경로는 항상 OTel 미들웨어를 탄다.
    if led is None or led.request_id != request_id or led.ended or led.closed:
        return
    try:
        from app.periods import date_at, period_at, request_started_at
        from app.schemas.domain import TokenUsage
        from app.services.cost_recorder import DEFAULT_DEPT_ID, DEFAULT_TEAM_ID, calculate_cost

        rls = state.get("rate_limit_state") or {}
        est_in = int(rls.get("estimated_input") or 0)
        if not est_in and body:
            # Redis 열화로 한도 집행을 건너뛴 통과(rate_limit_state 없음) — 본문에서 같은 방식으로
            # 추정한다.
            from app.services.rate_limit_enforcement import _estimate_input_tokens

            try:
                est_in = int(_estimate_input_tokens(body))
            except Exception:
                est_in = 0
        try:
            est_cost = calculate_cost(
                TokenUsage(input_tokens=est_in, output_tokens=0, total_tokens=est_in),
                model_config) if est_in else Decimal("0")
        except Exception:
            est_cost = Decimal("0")
        started = request_started_at() or datetime.now(UTC)
        provider = getattr(model_config.provider, "value", None) or str(model_config.provider)
        path = led.path
        payload = {
            "kind": "start",
            "request_id": request_id,
            "instance_id": INSTANCE_ID,
            "user_id": str(auth_context.user_id),
            "team_id": str(auth_context.team_id or DEFAULT_TEAM_ID),
            "teamless": not auth_context.team_id,
            "dept_id": str(auth_context.dept_id or DEFAULT_DEPT_ID),
            "model_alias": model_config.alias or model_config.provider_model_id,
            "provider": provider,
            "client": state.get("client"),
            "key_prefix": getattr(auth_context, "key_prefix", None),
            "sso_subject": getattr(auth_context, "sso_subject", None),
            "session_id": state.get("session_id"),
            "is_streaming": bool((body or {}).get("stream")) or "stream" in path,
            "estimated_input_tokens": est_in,
            "estimated_cost_usd": str(est_cost),
            "pricing_missing": bool(getattr(model_config.pricing, "pricing_missing", False))
            and est_in > 0,
            "requested_at": started.isoformat(),
            "period": period_at(started),
            "date": date_at(started),
            # sweeper 가 죽은 요청의 월예산 예약을 추정치로 정산한다(마커 기반 멱등 — 게이트웨이가
            # 이미
            # 정산했으면 no-op).
            "budget_reservation": [
                {k: (str(v) if v is not None else None) for k, v in r.items()}
                for r in (rls.get("budget_reservation") or []) if isinstance(r, dict)
            ],
            "schema_version": START_SCHEMA_VERSION,
        }
    except Exception:
        logger.exception("cost_start_build_failed", request_id=request_id)
        return
    led.started = True
    await _publish(payload, request_id, "start")


async def drain_tasks(timeout: float) -> int:
    """종료 시 — 아직 안 끝난 close 발행을 기다린다(취소 없이)."""
    pending = [t for t in _TASKS if not t.done()]
    if not pending:
        return 0
    done, still = await asyncio.wait(pending, timeout=timeout)
    if still:
        logger.warning("cost_close_tasks_timeout", remaining=len(still))
    return len(done)


class Heartbeat:
    """``cost:live:<INSTANCE_ID>`` 를 주기적으로 갱신한다(TTL). 실패는 로그만(다음 틱에 다시)."""

    def __init__(self, redis, *, interval: float = 10.0, ttl: int = 45) -> None:
        self._redis = redis
        self._interval = interval
        self._ttl = int(ttl)
        self._task: asyncio.Task | None = None
        self.key = HEARTBEAT_PREFIX + INSTANCE_ID

    async def beat(self) -> bool:
        try:
            async with asyncio.timeout(2.0):
                await self._redis.set(self.key, str(int(time.time())), ex=self._ttl)
            return True
        except Exception:
            logger.warning("cost_heartbeat_failed")
            return False

    async def _run(self) -> None:
        while True:
            await self.beat()
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="cost-heartbeat")

    async def stop(self, *, clear: bool = True) -> None:
        """루프를 멈추고(선택) 키를 지운다.

        정상 종료 뒤 남은 시작은 TTL 을 기다리지 않고 sweep 대상이 된다.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if clear:
            try:
                async with asyncio.timeout(2.0):
                    await self._redis.delete(self.key)
            except Exception:
                logger.warning("cost_heartbeat_clear_failed")
