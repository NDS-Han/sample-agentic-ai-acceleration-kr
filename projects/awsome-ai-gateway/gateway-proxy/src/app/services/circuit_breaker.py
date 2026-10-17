# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_LUA_PATH = Path(__file__).resolve().parent.parent / "redis_scripts" / "circuit_breaker.lua"


class CircuitBreakerService:
    """Distributed circuit breaker (sliding-window error-rate), keyed per provider_model_id.

    Redis-down => FAIL-OPEN (treat every circuit as closed) to match the rest of the
    gateway's degradation policy: an unavailable breaker must never block inference.
    """

    def __init__(
        self,
        window_sec: int = 30,
        min_calls: int = 5,
        error_rate: float = 0.5,
        open_sec: int = 30,
        halfopen_ttl_ms: int = 8000,
        open_jitter_sec: int = 5,
    ) -> None:
        self.window_sec = window_sec
        self.min_calls = min_calls
        self.error_rate = error_rate
        self.open_sec = open_sec
        self.halfopen_ttl_ms = halfopen_ttl_ms
        self.open_jitter_sec = open_jitter_sec
        self._lua = _LUA_PATH.read_text(encoding="utf-8")

    @staticmethod
    def _keys(pmid: str) -> tuple[str, str, str]:
        # Redis Cluster 해시태그 — 세 키(와 lua 가 파생하는 :sec 버킷,
        # halfopen 슬롯)가 같은 슬롯에 있어야 EVAL 이 CROSSSLOT 로 거부되지
        # 않는다. 태그 없는 키는 prod(Valkey cluster.on)에서 브레이커가 영구
        # fail-open 으로 죽는다(R22 r1 M2). 구(태그 없는) 키는 TTL 로 자연 소멸.
        return (
            f"cb:{{{pmid}}}:fail",
            f"cb:{{{pmid}}}:total",
            f"cb:{{{pmid}}}:open",
        )

    async def is_open(self, redis, pmid: str) -> bool:
        if redis is None:
            return False  # fail-open
        try:
            _f, _t, open_key = self._keys(pmid)
            return bool(await redis.exists(open_key))
        except Exception:
            logger.warning("cb_is_open_failed", pmid=pmid)
            return False  # fail-open

    async def _record(self, redis, pmid: str, is_failure: int) -> bool:
        if redis is None:
            return False
        try:
            fail_key, total_key, open_key = self._keys(pmid)
            sec, micro = await redis.time()  # Redis server time -> ms (avoid host clock skew)
            now_ms = int(sec) * 1000 + int(micro) // 1000
            open_ms = self.open_sec * 1000 + (now_ms % (self.open_jitter_sec * 1000 + 1))  # deterministic jitter seeded by Redis server time; circuit state is shared across pods so per-pod randomness is unnecessary (and would reintroduce host-clock dependence)
            opened = await redis.eval(
                self._lua, 3, fail_key, total_key, open_key,
                str(self.window_sec), str(self.min_calls), str(self.error_rate),
                str(open_ms), str(is_failure), str(now_ms),
            )
            return bool(opened)
        except Exception:
            logger.warning("cb_record_failed", pmid=pmid, is_failure=is_failure)
            return False

    async def record_failure(self, redis, pmid: str) -> bool:
        return await self._record(redis, pmid, 1)

    async def record_success(
        self, redis, pmid: str, *, probe_token: str | None = None
    ) -> bool:
        """성공 기록. 회로 CLOSE 는 half-open 프로브 소유자의 성공에만 허용한다.

        ``probe_token`` 없이(닫힌 회로의 일반 요청)는 카운터만 기록한다 —
        회로가 열리기 **전에** 시작된 in-flight 요청의 늦은 성공이 open 을
        즉시 지워 회로가 열리자마자 닫히는 플랩을 막는다(R22 r1 M4). 프로브
        소유자의 성공만 `halfopen` 슬롯과 `open` 을 지운다. `open` 은 토큰
        소유자가 온 무조건 지운다 — 프로브를 따낸 사실 자체가 이미 검증된
        신호이고, 슬롯 TTL 만료로 재획득이 일어났어도 우리 프로브의 성공은
        upstream 회복의 진짜 증거다(R22 r2 N1). `halfopen` 슬롯은 조건부로만
        지운다 — 슬롯 TTL 만료 뒤 다른 파드가 따낸 프로브를 우리가 지우지
        않는다.
        """
        opened = await self._record(redis, pmid, 0)
        if redis is not None and probe_token is not None:
            try:
                _f, _t, open_key = self._keys(pmid)
                await redis.eval(
                    "redis.call('DEL', KEYS[1]); "
                    "if redis.call('GET', KEYS[2]) == ARGV[1] then "
                    "return redis.call('DEL', KEYS[2]) else return 0 end",
                    2, open_key, f"cb:{{{pmid}}}:halfopen", probe_token,
                )
                # `_record` 의 반환은 기록 **이전** 상태다 — 방금 프로브로
                # 회로를 닫았으니 호출자에게는 닫힘을 돌려준다. 반환값을
                # 그대로 넘기면 is_open=False 인데 opened=True 가 된다
                # (R22 r5 N3 — 현재 호출부는 값을 안 쓰지만 메트릭/로그로
                # 쓰이면 오보가 된다).
                return False
            except Exception:
                logger.warning("cb_close_failed", pmid=pmid)
        return opened

    async def try_acquire_halfopen_probe(
        self, redis, pmid: str, token: str | None = None
    ) -> bool:
        """Elect a single probe across pods via SET NX PX. Winner returns True.

        ``token`` marks the winning holder so ``release_halfopen_probe`` can return
        an UNUSED slot without deleting another pod's fresh acquire.
        """
        if redis is None:
            return True  # fail-open: let the request through
        try:
            won = await redis.set(
                f"cb:{{{pmid}}}:halfopen", token or "1", nx=True, px=self.halfopen_ttl_ms
            )
            return bool(won)
        except Exception:
            logger.warning("cb_halfopen_acquire_failed", pmid=pmid)
            return True

    async def release_halfopen_probe(self, redis, pmid: str, token: str) -> None:
        """Return a probe slot that was acquired but never used (e.g. the request
        was rejected by admission AFTER winning the probe — R20 N1). The window's
        only probe slot must not burn for ``halfopen_ttl_ms`` on a request that
        never invoked upstream. Conditional delete: a TTL-expired slot re-acquired
        by another pod must not be deleted by our stale token."""
        if redis is None:
            return
        try:
            await redis.eval(
                "if redis.call('GET', KEYS[1]) == ARGV[1] "
                "then return redis.call('DEL', KEYS[1]) else return 0 end",
                1, f"cb:{{{pmid}}}:halfopen", token,
            )
        except Exception:
            logger.warning("cb_halfopen_release_failed", pmid=pmid)
