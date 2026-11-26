# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""라우터 모델/카탈로그 조회의 프로세스 로컬 캐시 (wave 1d).

예전 동작: 요청마다 ``RouterService._resolve_by_providers`` 가 Redis GET 2~4 회(음수 캐시 2키 +
``model:{ref}`` + ``model:pmid:{ref}``)를 하고, 미스면 메인 풀로 DB 를 읽었다. Redis 행/다운이면
GET 이 socket_timeout × 재시도를 기다린 뒤 RedisError 로 터져 인증 미들웨어가 503
(``auth_backend_error``)을 냈다. DB 가 느리면 미스 요청이 DB 를 기다렸다.

이제 (모두 프로세스 단위, 새 인프라 없음 — wave 1 ``hot_path`` / 1c ``budget_degraded`` 패턴):

1. **로컬 캐시** — 조회 결과(모델 schema / 미등록 / INACTIVE, alias→provider, 활성 모델 목록,
   routing profile)를 키별로 보관한다. ``refresh_sec`` 안이면 Redis/DB 를 전혀 보지 않는다.
2. **백그라운드 재조회** — ``refresh_sec`` 가 지난 항목은 그대로 서빙하고 키당 한 번
   (single-flight) 메인 풀에서 ``refresh_timeout_sec`` 데드라인으로 다시 읽는다. 실패하면
   ``refresh_retry_sec`` 동안 재시도하지 않는다. pub/sub 구독이 끊겨 있으면 무효화를 들을 수
   없으므로 ``refresh_unsubscribed_sec`` 로 더 자주 재조회한다.
3. **제한된 읽기** — 로컬 미스일 때 Redis 는 ``redis_timeout_sec`` + 자체 브레이커, DB 는
   ``hot_db_timeout_sec``(0.3 s) + 프로세스 DB 브레이커/DB_DEGRADED 를 따른다. 둘 다 답하지 못하면
   ``ModelBackendUnavailableError``(ConnectionError → 503). 미등록 모델을 404 로 위장하지 않는다.
4. **마지막으로 알던 값** — DB/Redis 장애 중에는 ``max_stale_sec`` 까지 캐시 값을 쓴다.
5. **무효화** — admin-api 가 모델/alias/단가/routing profile 을 바꾸면 Redis 키를 지운 뒤
   ``gateway:model:invalidate`` 로 publish 한다. 게이트웨이는 해당 항목을 지우고 세대 번호를 올려
   진행 중인 재조회가 옛 값을 다시 넣지 못하게 한다. pub/sub 이 죽어 있으면 재조회 주기가
   상한이다.

``MODEL_CACHE`` 가 None 이면(테스트 기본, ``model_cache_enabled=false``) 라우터는 예전 경로다.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.services.hot_path import DB_BREAKER, DbBreaker, LocalCache, is_redis_infra_error

logger = structlog.get_logger(__name__)

# Entry.kind
OK = "ok"
NX = "nx"              # 미등록 / provider 불일치 / 프로파일 없음 (음수 결과)
INACTIVE = "inactive"  # alias 는 있으나 status=INACTIVE (kill switch)

# gateway_model_cache_total{result} — 카디널리티 고정.
R_FRESH = "fresh"                  # 로컬 캐시 히트 (재조회 불필요)
R_STALE = "stale"                  # 오래된 항목 서빙 + 백그라운드 재조회 예약
R_REDIS = "redis"                  # 로컬 미스 → Redis 에서 채움
R_DB = "db"                        # 로컬 미스 → DB 에서 채움
R_NO_DB = "no_db"                  # 로컬·Redis 미스, 호출자가 DB 불가(열화) → 레거시 폴백
R_UNAVAILABLE = "unavailable"      # 로컬 미스 + Redis/DB 모두 실패 → 503
R_REFRESH_OK = "refresh_ok"
R_REFRESH_FAILED = "refresh_failed"
R_INVALIDATED = "invalidated"


class ModelBackendUnavailableError(ConnectionError):
    """모델 카탈로그를 로컬 캐시·Redis·DB 어디서도 얻지 못했다(인프라 장애).

    ConnectionError 하위 클래스 — 인증 미들웨어의 인프라 분기가 503 으로 바꾼다
    (예전에 RedisError/DBAPIError 가 밟던 경로와 같다). LookupError 가 아니므로 404 로
    오인되지 않는다.
    """


@dataclass(frozen=True)
class Entry:
    kind: str
    value: Any = None
    #: 이 항목을 무효화하는 모델 ref(alias / provider_model_id) 집합.
    refs: frozenset = field(default_factory=frozenset)
    #: routing profile 항목이면 client 이름.
    client: str | None = None


#: (op, key, ttl, payload) — op ∈ {"setex", "delete"}. DB 결과를 Redis 에 반영(다른 파드와 공유).
RedisWrite = tuple[str, str, int, str | None]


class ModelCatalogCache:
    def __init__(self, *, session_factory=None, refresh_sec: float = 60.0,
                 refresh_unsubscribed_sec: float = 10.0, max_stale_sec: float = 3600.0,
                 negative_ttl_sec: float = 15.0, max_entries: int = 5000,
                 db_timeout_sec: float = 0.3, redis_timeout_sec: float = 0.3,
                 refresh_timeout_sec: float = 2.0, refresh_retry_sec: float = 10.0,
                 redis_breaker_cooldown_sec: float = 2.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.session_factory = session_factory
        self.refresh_sec = refresh_sec
        self.refresh_unsubscribed_sec = min(refresh_unsubscribed_sec, refresh_sec)
        self.negative_ttl_sec = negative_ttl_sec
        self.db_timeout_sec = db_timeout_sec
        self.redis_timeout_sec = redis_timeout_sec
        self.refresh_timeout_sec = refresh_timeout_sec
        self.refresh_retry_sec = refresh_retry_sec
        self.cache: LocalCache[Entry] = LocalCache(
            ttl=refresh_sec, max_stale=max_stale_sec, max_entries=max_entries, clock=clock
        )
        self.redis_breaker = DbBreaker(cooldown_sec=redis_breaker_cooldown_sec, clock=clock)
        #: 무효화마다 +1 — 무효화 이전에 시작된 읽기는 결과를 캐시에 넣지 않는다.
        self.generation = 0
        self.retry_at: dict[Any, float] = {}
        #: pub/sub 구독 상태 (listener.connected). None = 모름 → 구독 중으로 본다.
        self.subscribed: Callable[[], bool] | None = None
        self._counter = None
        self._clock = clock
        self._last_log: dict[str, float] = {}

    # ── metric / log ──────────────────────────────────────────────────────────
    def set_metric(self, counter) -> None:
        self._counter = counter

    def _count(self, result: str) -> None:
        if self._counter is not None:
            try:
                self._counter.add(1, {"result": result})
            except Exception:
                pass

    def _warn(self, event: str, **fields: Any) -> None:
        """사유별 5초에 한 번 — 장애 중 요청마다 한 줄씩 쓰지 않는다."""
        now = self._clock()
        if now - self._last_log.get(event, -1e9) >= 5.0:
            self._last_log[event] = now
            logger.warning(event, **fields)

    # ── freshness ─────────────────────────────────────────────────────────────
    def _fresh_limit(self, entry: Entry) -> float:
        limit = self.refresh_sec
        if self.subscribed is not None and not self.subscribed():
            limit = self.refresh_unsubscribed_sec
        if entry.kind == NX:
            limit = min(limit, self.negative_ttl_sec)
        return limit

    # ── lookups ───────────────────────────────────────────────────────────────
    async def get(
        self,
        key: Any,
        *,
        db,
        db_load: Callable[[Any], Awaitable[tuple[Entry, list[RedisWrite]]]],
        redis=None,
        redis_load: Callable[[], Awaitable[Entry | None]] | None = None,
        fallback: Callable[[bool, bool], Awaitable[Entry]] | None = None,
    ) -> Entry:
        """``key`` 의 조회 결과.

        ``db``: 호출자의 세션, 또는 None(호출자가 DB 열화로 판단 — DB 를 시도하지 않는다).
        캐시에 세션 팩토리가 있으면 DB 읽기는 그 팩토리의 짧은 세션으로 한다(호출자 세션은 신호용).
        ``fallback(redis_failed, db_failed)``: 로컬·Redis 미스이고 DB 로 못 채울 때의 결과.
        None 이면 ``ModelBackendUnavailableError``.
        """
        hit = self.cache.get_stale(key)
        if hit is not None:
            age = self.cache.age(key) or 0.0
            if age <= self._fresh_limit(hit):
                self._count(R_FRESH)
                return hit
            self._count(R_STALE)
            self._schedule_refresh(key, db_load, redis, db_allowed=db is not None)
            return hit

        gen = self.generation
        redis_failed = False
        if redis is not None and redis_load is not None:
            entry, redis_failed = await self._redis_call(redis_load, what="model_cache.read")
            if entry is not None:
                self._put(key, entry, gen)
                self._count(R_REDIS)
                # Redis 값은 최대 300 s 묵었을 수 있다 — DB 진실로 곧바로 한 번 맞춘다.
                self._schedule_refresh(key, db_load, redis, db_allowed=db is not None, force=True)
                return entry

        db_failed = False
        if db is not None:
            try:
                entry, writes = await self._run_db(db_load, db, timeout=self.db_timeout_sec,
                                                   what="model_cache.load")
            except ModelBackendUnavailableError:
                db_failed = True
            else:
                self._put(key, entry, gen)
                self._count(R_DB)
                await self._apply_writes(redis, writes)
                return entry

        if fallback is not None and not (redis_failed or db_failed):
            self._count(R_NO_DB)
            return await fallback(redis_failed, db_failed)
        if fallback is not None:
            try:
                return await fallback(redis_failed, db_failed)
            except ModelBackendUnavailableError:
                pass
        self._count(R_UNAVAILABLE)
        self._warn("model_cache_unavailable", key=str(key), redis_failed=redis_failed,
                   db_failed=db_failed, db_skipped=db is None)
        raise ModelBackendUnavailableError(f"model catalog unavailable for {key!r}")

    def _put(self, key, entry: Entry, gen: int) -> None:
        if gen == self.generation:
            self.cache.put(key, entry)

    async def _run_db(self, fn, db, *, timeout: float, what: str):
        if not DB_BREAKER.available():
            raise ModelBackendUnavailableError(f"{what}: db unavailable (degraded or breaker open)")
        try:
            async with asyncio.timeout(timeout):
                if self.session_factory is not None:
                    async with self.session_factory() as s:
                        return await fn(s)
                if db is None:
                    raise ModelBackendUnavailableError(f"{what}: no session")
                return await fn(db)
        except ModelBackendUnavailableError:
            raise
        except TimeoutError as e:
            DB_BREAKER.trip(f"{what}:timeout")
            raise ModelBackendUnavailableError(f"{what}: timed out after {timeout}s") from e
        except (SQLAlchemyError, OSError) as e:
            DB_BREAKER.trip(f"{what}:{type(e).__name__}")
            raise ModelBackendUnavailableError(f"{what}: {type(e).__name__}") from e

    async def redis_call(self, fn: Callable[[], Awaitable[Any]], *, what: str) -> tuple[Any, bool]:
        """``fn()`` 을 Redis 데드라인 + 브레이커 안에서. (결과, 실패 여부)."""
        return await self._redis_call(fn, what=what)

    async def _redis_call(self, fn, *, what: str) -> tuple[Any, bool]:
        if not self.redis_breaker.available():
            return None, True
        try:
            async with asyncio.timeout(self.redis_timeout_sec):
                out = await fn()
            self.redis_breaker.record_success()
            return out, False
        except (TimeoutError, RedisError, OSError) as e:
            if is_redis_infra_error(e):  # 풀 고갈·ResponseError 는 Redis 장애가 아니다
                self.redis_breaker.trip(f"{what}:{type(e).__name__}")
            return None, True

    async def _apply_writes(self, redis, writes: Iterable[RedisWrite]) -> None:
        writes = list(writes or ())
        if redis is None or not writes:
            return

        async def _do():
            for op, k, ttl, payload in writes:
                if op == "setex":
                    await redis.setex(k, ttl, payload)
                elif op == "delete":
                    await redis.delete(k)

        await self._redis_call(_do, what="model_cache.write")

    # ── background refresh ────────────────────────────────────────────────────
    def _schedule_refresh(self, key, db_load, redis, *, db_allowed: bool,
                          force: bool = False) -> bool:
        if not db_allowed or self.session_factory is None or not DB_BREAKER.available():
            return False
        if not force and self._clock() < self.retry_at.get(key, 0.0):
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        gen = self.generation

        async def _refresh():
            try:
                entry, writes = await self._run_db(db_load, None, timeout=self.refresh_timeout_sec,
                                                   what="model_cache.refresh")
            except ModelBackendUnavailableError as e:
                if len(self.retry_at) > 50000:
                    self.retry_at.clear()
                self.retry_at[key] = self._clock() + self.refresh_retry_sec
                self._count(R_REFRESH_FAILED)
                self._warn("model_cache_refresh_failed", key=str(key), error=str(e))
                return
            self.retry_at.pop(key, None)
            self._put(key, entry, gen)
            self._count(R_REFRESH_OK)
            await self._apply_writes(redis, writes)

        from app.services.hot_path import single_flight

        return single_flight(f"model-cache:{key!r}", _refresh)

    # ── invalidation (admin-api → gateway) ────────────────────────────────────
    def invalidate(self, payload: dict) -> None:
        """``{"model_refs": [...], "model_list": bool, "routing_clients": [...], "all": bool}``."""
        self.generation += 1
        self._count(R_INVALIDATED)
        if payload.get("all"):
            self.cache.clear()
            self.retry_at.clear()
            logger.info("model_cache_invalidated", scope="all")
            return
        refs = {str(r) for r in payload.get("model_refs") or [] if r}
        clients = {str(c) for c in payload.get("routing_clients") or [] if c}
        drop_list = bool(payload.get("model_list")) or bool(refs)

        def _doomed(k, e: Entry) -> bool:
            if e.client is not None:
                return e.client in clients
            if k == LIST_KEY:
                return drop_list
            return not refs.isdisjoint(e.refs)

        n = self.cache.evict_where(_doomed)
        logger.info("model_cache_invalidated", refs=len(refs), clients=len(clients),
                    model_list=drop_list, evicted=n)

    def reset(self) -> None:
        self.cache.clear()
        self.retry_at.clear()
        self.redis_breaker.reset()
        self.generation += 1


LIST_KEY = ("list",)

#: Set at startup (``configure``); None = legacy router behaviour (no local cache).
MODEL_CACHE: ModelCatalogCache | None = None


def configure(cache: ModelCatalogCache | None) -> None:
    global MODEL_CACHE
    MODEL_CACHE = cache


def get() -> ModelCatalogCache | None:
    return MODEL_CACHE


def from_settings(settings, session_factory) -> ModelCatalogCache:
    cache = ModelCatalogCache(
        session_factory=session_factory,
        refresh_sec=settings.model_cache_refresh_sec,
        refresh_unsubscribed_sec=settings.model_cache_refresh_unsubscribed_sec,
        max_stale_sec=settings.model_cache_max_stale_sec,
        negative_ttl_sec=settings.model_cache_negative_ttl_sec,
        max_entries=settings.model_cache_max_entries,
        db_timeout_sec=settings.hot_db_timeout_sec,
        redis_timeout_sec=settings.model_cache_redis_timeout_sec,
        refresh_timeout_sec=settings.model_cache_refresh_timeout_sec,
        refresh_retry_sec=settings.model_cache_refresh_retry_sec,
        redis_breaker_cooldown_sec=settings.hot_db_breaker_cooldown_sec,
    )
    # p1-hotpath-redis: 요청 경로 Redis 는 프로세스 공용 브레이커 하나를 쓴다.
    from app.services.hot_path import REDIS_BREAKER

    cache.redis_breaker = REDIS_BREAKER
    return cache
