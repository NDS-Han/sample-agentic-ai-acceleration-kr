# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""예산 체크 열화 모드 — DB/Redis 장애 중 **마지막으로 알던 값**으로 판정한다 (wave 1c, 옵션 B).

예전 동작: Redis 경로 안의 DB 조회(설정 재수화·개인 설정 부재 확인·카운터 복원) 하나만 실패해도
DB 폴백으로 갔고, DB 폴백도 실패하면 503 이었다. DB 장애 동안 거의 모든 요청이 503 이 됐다.

이제 (모두 프로세스 단위, 새 인프라 없음):

1. **정책 캐시** (``PolicyCache``) — 팀/사용자/앱 예산 설정(한도·정책·임계값·D cap)을 프로세스 안에
   보관한다. 요청 경로가 DB 를 읽을 때마다 갱신되고, ``budget_policy_refresh_sec`` 가 지나면
   백그라운드(single-flight, 데드라인)로 다시 읽는다. 요청 경로의 DB 조회는 전부
   ``hot_db_timeout_sec``(0.3 s) 데드라인 + 프로세스 DB 브레이커를 따르고, 실패하면 이 캐시의
   값을 ``budget_stale_max_seconds``(900 s) 까지 쓴다.
2. **지출**은 기존 Redis 예산 카운터(Lua)에서 온다. DB 는 Redis 경로가 실패했을 때의 제한된
   폴백일 뿐이다.
3. **마지막 판정 상태** (``KnownState``) — 계층(팀/사용자/앱)별로 마지막에 본 한도·사용액·정책을
   기억한다. Redis 와 DB 가 둘 다 답하지 못하면 이 값으로 다시 판정한다 — 알던 초과/차단 상태는
   장애 중에도 그대로 429 다.
4. **유예 창** (``Grace``) — 정책도 상태도 모를 때만(예: 콜드 파드 + DB 다운) 프로세스당
   ``budget_failopen_grace_seconds``(300 s) 동안 허용하고, 그 뒤에는 503(fail-closed)이다.
   유예 시계는 예산 경로의 DB 읽기가 다시 성공하면 리셋된다.

모든 열화 판정은 ``gateway_budget_degraded_total{reason}`` 으로 세고 warning 으로 남긴다.
비용 기록(cost_recorder / worker)은 바뀌지 않는다 — 장애 중 드리프트는 이후 reconcile 이 잡는다.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from app.services.hot_path import DbBreaker, LocalCache

logger = structlog.get_logger(__name__)

#: 정책 캐시의 "행 없음" 표지 — ``None``(모름)과 구분한다.
ABSENT: dict = {"__absent__": True}

# reason 라벨 (gateway_budget_degraded_total) — 카디널리티 고정.
R_STALE_POLICY = "stale_policy"          # DB 불가 → 캐시된 정책으로 Redis 판정
R_LAST_KNOWN_ALLOW = "last_known_allow"  # Redis+DB 불가 → 마지막 상태로 허용
R_LAST_KNOWN_BLOCK = "last_known_block"  # Redis+DB 불가 → 마지막 상태로 차단(429)
R_GRACE_ALLOW = "grace_allow"            # 아무것도 모름 → 유예 창 안이라 허용
R_GRACE_EXPIRED = "grace_expired"        # 아무것도 모름 → 유예 창 만료 → 503
R_REBUILD_SKIPPED = "rebuild_skipped"    # DB 불가 → 카운터 복원 생략(Redis 값으로 판정)


class PolicyCache:
    """(scope, id[, client]) → Redis 설정 JSON 과 같은 dict, 또는 ``ABSENT``."""

    def __init__(self, refresh_sec: float, stale_max_sec: float, max_entries: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cache: LocalCache[dict] = LocalCache(
            ttl=refresh_sec, max_stale=stale_max_sec, max_entries=max_entries, clock=clock
        )

    @staticmethod
    def team_key(team_id: str) -> tuple:
        return ("team", team_id)

    @staticmethod
    def user_key(user_id: str) -> tuple:
        return ("user", user_id)

    @staticmethod
    def client_key(user_id: str, client: str) -> tuple:
        return ("client", user_id, client)

    def put(self, key: tuple, value: dict | None) -> None:
        self.cache.put(key, ABSENT if value is None else value)

    def fresh(self, key: tuple) -> dict | None:
        return self.cache.get_fresh(key)

    def stale(self, key: tuple) -> dict | None:
        return self.cache.get_stale(key)

    def needs_refresh(self, key: tuple) -> bool:
        return self.cache.get_fresh(key) is None


class KnownState:
    """(scope, id[, client], period) → 마지막 판정 계층 dict (Lua 결과와 같은 shape)."""

    def __init__(self, stale_max_sec: float, max_entries: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cache: LocalCache[dict] = LocalCache(
            ttl=stale_max_sec, max_stale=stale_max_sec, max_entries=max_entries, clock=clock
        )

    def put(self, key: tuple, tier: dict) -> None:
        self.cache.put(key, dict(tier))

    def get(self, key: tuple) -> dict | None:
        return self.cache.get_stale(key)


class Grace:
    """정책·상태를 모를 때의 프로세스 단위 fail-open 창."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = seconds
        self._clock = clock
        self.started_at: float | None = None

    def admit(self) -> tuple[bool, float]:
        """(허용 여부, 유예 시작 후 경과 초)."""
        now = self._clock()
        if self.started_at is None:
            self.started_at = now
        elapsed = now - self.started_at
        return elapsed <= self.seconds, elapsed

    def reset(self) -> None:
        if self.started_at is not None:
            logger.info("budget_degraded_grace_reset",
                        elapsed=round(self._clock() - self.started_at, 1))
        self.started_at = None


class BudgetDegraded:
    """열화 모드 상태 묶음 — 프로세스당 하나(``DEGRADED``)."""

    def __init__(self, *, refresh_sec: float = 60.0, stale_max_sec: float = 900.0,
                 grace_sec: float = 300.0, max_entries: int = 20000,
                 redis_breaker_cooldown_sec: float = 2.0,
                 refresh_retry_sec: float = 10.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.policy = PolicyCache(refresh_sec, stale_max_sec, max_entries, clock)
        self.known = KnownState(stale_max_sec, max_entries, clock)
        self.grace = Grace(grace_sec, clock)
        #: 예산 Redis 경로 전용 브레이커 — Redis 행/다운 시 요청마다 소켓 타임아웃을 기다리지 않게.
        self.redis_breaker = DbBreaker(cooldown_sec=redis_breaker_cooldown_sec, clock=clock)
        self.stale_max_sec = stale_max_sec
        self.refresh_retry_sec = refresh_retry_sec
        #: 백그라운드 재조회가 실패한 키 → 다음 시도 가능 시각(DB 장애 중 재시도 폭주 방지).
        self.refresh_retry_at: dict[tuple, float] = {}
        self._counter = None
        self._last_log: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._clock = clock

    # ── metric + log ──────────────────────────────────────────────────────────
    def set_metric(self, counter) -> None:
        self._counter = counter

    def record(self, reason: str, *, always_log: bool = False, **fields: Any) -> None:
        """``gateway_budget_degraded_total{reason}`` +1 과 warning 로그.

        유예 판정(grace_*)은 매번 남긴다(누가 한도 확인 없이 통과했는지의 감사 기록).
        나머지는 사유별 5초에 한 번 + 억제된 건수로 남긴다 — 장애 중 요청마다 한 줄씩 쓰면
        로그 파이프라인이 두 번째 장애가 된다. 메트릭은 매번 센다.
        """
        if self._counter is not None:
            try:
                self._counter.add(1, {"reason": reason})
            except Exception:
                pass
        now = self._clock()
        if always_log or now - self._last_log.get(reason, -1e9) >= 5.0:
            suppressed = self._suppressed.pop(reason, 0)
            self._last_log[reason] = now
            logger.warning("budget_degraded", reason=reason, suppressed=suppressed, **fields)
        else:
            self._suppressed[reason] = self._suppressed.get(reason, 0) + 1

    def refresh_allowed(self, key: tuple) -> bool:
        return self._clock() >= self.refresh_retry_at.get(key, 0.0)

    def refresh_failed(self, key: tuple) -> None:
        if len(self.refresh_retry_at) > 50000:
            self.refresh_retry_at.clear()
        self.refresh_retry_at[key] = self._clock() + self.refresh_retry_sec

    def reset(self) -> None:
        self.refresh_retry_at.clear()
        self.policy.cache.clear()
        self.known.cache.clear()
        self.grace.started_at = None
        self.redis_breaker.reset()
        self._last_log.clear()
        self._suppressed.clear()


DEGRADED = BudgetDegraded()


def configure(settings) -> None:
    """startup 에서 설정값으로 프로세스 싱글톤을 다시 만든다(메트릭 연결은 유지)."""
    global DEGRADED
    counter = DEGRADED._counter
    DEGRADED = BudgetDegraded(
        refresh_sec=settings.budget_policy_refresh_sec,
        stale_max_sec=settings.budget_stale_max_seconds,
        grace_sec=settings.budget_failopen_grace_seconds,
        max_entries=settings.budget_local_cache_max_entries,
        redis_breaker_cooldown_sec=settings.hot_db_breaker_cooldown_sec,
        refresh_retry_sec=settings.budget_policy_refresh_retry_sec,
    )
    DEGRADED.set_metric(counter)


def get() -> BudgetDegraded:
    return DEGRADED


def spawn_refresh(key: tuple, factory: Callable[[], Awaitable[Any]]) -> bool:
    """백그라운드 정책 재조회 — 키당 한 번만(single-flight). 루프가 없으면 무시."""
    from app.services.hot_path import single_flight

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return single_flight("budget-policy:" + ":".join(key), factory)
