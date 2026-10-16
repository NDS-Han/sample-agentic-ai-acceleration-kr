# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import CacheInvalidationFailure

logger = structlog.get_logger()


class CacheInvalidationManager:
    """Best-effort Redis DEL with failure logging and manual retry support."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def invalidate(
        self,
        keys: list[str],
        *,
        session: AsyncSession | None = None,
    ) -> None:
        """Delete Redis cache keys. On failure, log to cache_invalidation_failures table."""
        if not keys:
            return

        for key in keys:
            try:
                await self._redis.delete(key)
                logger.debug("cache.invalidated", key=key)
            except Exception:
                logger.warning("cache.invalidation_failed", key=key, exc_info=True)
                if session:
                    await self._record_failure(session, key)

    async def invalidate_pattern(
        self,
        pattern: str,
        *,
        session: AsyncSession | None = None,
    ) -> int:
        """Delete all Redis keys matching ``pattern`` via SCAN+DEL. Returns deleted count.

        Use for fan-out invalidation when a single source-of-truth update should
        invalidate many cached keys (e.g. one rate-limit policy change → all
        per-model cache entries for that user/team). On failure, logs the
        pattern as a single failure entry; partial deletes are not rolled back.
        """
        try:
            deleted = await self._scan_delete(pattern)
            logger.debug("cache.invalidated_pattern", pattern=pattern, deleted=deleted)
            return deleted
        except Exception:
            logger.warning("cache.invalidation_pattern_failed", pattern=pattern, exc_info=True)
            if session is not None:
                await self._record_failure(session, f"pattern:{pattern}")
            return 0

    async def _scan_delete(self, pattern: str) -> int:
        """SCAN+DEL — 예외를 삼키지 않아 retry 경로에서도 쓸 수 있다."""
        deleted = 0
        async for key in self._redis.scan_iter(match=pattern, count=200):
            await self._redis.delete(key)
            deleted += 1
        return deleted

    async def retry_failed(self, session: AsyncSession) -> int:
        """Retry all unresolved cache invalidation failures. Returns count of resolved."""
        from sqlalchemy import select

        # LIMIT + 재시도 횟수 상한 — 무제한 조회는 한 요청이 모든 미해소
        # 실패를 들고 와 retry 폭풍/장기 트랜잭션을 만든다(R21 S9).
        # 영구 실패 키는 retry_count 상한 이후 매 요청 재시도하지 않는다.
        stmt = (
            select(CacheInvalidationFailure)
            .where(CacheInvalidationFailure.resolved_at.is_(None))
            .where(CacheInvalidationFailure.retry_count < 100)
            .order_by(CacheInvalidationFailure.failed_at)
            .limit(500)
            # SKIP LOCKED — 스케줄러와 수동 호출이 동시에 돌면 같은 미해소 행을
            # 둘이 잡아 DEL 을 이중 실행한다(R21 r2).
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        failures = result.scalars().all()

        resolved_count = 0
        for failure in failures:
            try:
                # cache_key 에는 실패 유형이 접두어로 박혀 있다 — 리터럴 DEL 로
                # "해소" 처리하면 진짜 대상 키들은 stale 한 채로 남는다(R16-V8).
                await self._retry_one(failure)
                failure.resolved_at = datetime.now(timezone.utc)
                resolved_count += 1
                logger.info("cache.retry_resolved", key=failure.cache_key)
            except Exception:
                failure.retry_count += 1
                failure.last_retry_at = datetime.now(timezone.utc)
                logger.warning("cache.retry_failed", key=failure.cache_key, retry_count=failure.retry_count)

        return resolved_count

    async def _retry_one(self, failure: CacheInvalidationFailure) -> None:
        """실패 유형별 재시도 — pattern:/swap: 접두어를 복원해 실행한다."""
        key = failure.cache_key
        if key.startswith("pattern:"):
            await self._scan_delete(key[len("pattern:"):])
            return
        if key.startswith("swap:"):
            spec = key[len("swap:"):]
            old_key, new_key = spec.split("->", 1)
            members = (failure.context or {}).get("members") or []
            if not members:
                # 멤버 목록을 모르는 구형 실패행은 재시도 불가 — 예외로 두고
                # unresolved 상태를 유지해 수동 처리 대상으로 남긴다.
                raise RuntimeError(
                    f"swap retry needs member list: {key}"
                )
            pipe = self._redis.pipeline(transaction=False)
            pipe.srem(old_key, *members)
            pipe.sadd(new_key, *members)
            await pipe.execute()
            return
        await self._redis.delete(key)

    async def live_vk_hashes(self, team_id: uuid.UUID) -> list[str]:
        """``team:vk_hashes:{tid}`` 역인덱스에서 **살아있는** VK 해시만 반환한다.

        멤버는 발급 시 sadd 되고 폐기·만료·팀이동 시 srem 되지만, 자연 TTL 만료는
        아무도 srem 하지 않아 집합이 무한히 자란다(죽은 해시가 smembers 마다
        재처리된다). 읽는 지점에서 ``key:vk:{h}`` EXISTS 를 한 파이프라인으로
        확인해 죽은 멤버를 srem 정리 — self-healing 으로 상한을 둔다.
        읽기 실패 시 빈 리스트(호출부는 TTL 자연 만료에 맡긴다).
        """
        index_key = f"team:vk_hashes:{team_id}"
        try:
            raw = await self._redis.smembers(index_key)
            members = [
                m.decode() if isinstance(m, bytes) else m for m in raw
            ]
            if not members:
                return []
            pipe = self._redis.pipeline(transaction=False)
            for h in members:
                pipe.exists(f"key:vk:{h}")
            alive = await pipe.execute()
            dead = [h for h, ok in zip(members, alive) if not ok]
            if dead:
                try:
                    await self._redis.srem(index_key, *dead)
                except Exception:
                    logger.warning(
                        "vk_reverse_index.srem_failed",
                        team_id=str(team_id), exc_info=True,
                    )
            return [h for h, ok in zip(members, alive) if ok]
        except Exception:
            logger.warning(
                "vk_reverse_index.read_failed", team_id=str(team_id), exc_info=True,
            )
            return []

    async def swap_reverse_index_membership(
        self,
        *,
        old_key: str,
        new_key: str,
        members: list[str],
        session: AsyncSession | None = None,
    ) -> None:
        """SREM old_key + SADD new_key in one pipeline. Failure → audit + raise."""
        if not members:
            return
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.srem(old_key, *members)
            pipe.sadd(new_key, *members)
            await pipe.execute()
        except Exception:
            logger.warning(
                "cache.swap_reverse_index_failed",
                old_key=old_key,
                new_key=new_key,
                member_count=len(members),
                exc_info=True,
            )
            if session is not None:
                # 멤버 목록을 함께 기록해 retry_failed 가 SREM/SADD 를 재실행
                # 할 수 있게 한다 — 없으면 swap 실패행은 재시도 불가로 남는다.
                await self._record_failure(
                    session,
                    f"swap:{old_key}->{new_key}",
                    context={"members": list(members)},
                )
            raise

    async def _record_failure(
        self,
        session: AsyncSession,
        cache_key: str,
        *,
        context: dict | None = None,
    ) -> None:
        entry = CacheInvalidationFailure(
            id=uuid.uuid4(),
            cache_key=cache_key,
            failed_at=datetime.now(timezone.utc),
            retry_count=0,
            context={"source": "admin-api", **(context or {})},
        )
        session.add(entry)


async def invalidate_after_commit(
    session: AsyncSession,
    cache_mgr: CacheInvalidationManager,
    keys: list[str] | None = None,
    *,
    pattern: str | None = None,
    extra: list | None = None,
) -> None:
    """cache DEL 을 ``session`` 의 커밋 이후로 지연한다 (R4: commit-전 DEL race).

    왜 DEL 도 미뤄야 하는가 — CommittingRoute 는 핸들러 반환 후에 commit 한다.
    DEL→commit 창에 게이트웨이 요청이 cache-miss 로 들어오면 **아직 커밋 안 된**
    구 정책 행을 읽어 ≤TTL 동안 재캐시한다: 방금 내린 예산/끈 모델이 수 분간
    살아있는다(R3-8 부류 — team/user allowed-models·apps·routing 은 이미
    post-commit 패턴). 롤백 자가치유 논리는 **rollback 케이스**만 커버하고
    이 commit-성공 재캐시 창은 커버하지 못한다.

    실패 기록(``cache_invalidation_failures``)은 요청 세션과 경합하지 않도록
    별도 세션에서 커밋한다 — 지연 콜백은 ``asyncio.create_task`` 로 응답 경로와
    동시에 실행될 수 있고 AsyncSession 은 태스크 간 공유가 불가능하다.
    세션이 이벤트를 지원하지 않으면(mock 테스트) 즉시 실행해 기존 동작을 유지.

    ``extra``: DEL 이후 같은 타이밍에 실행할 부가 Redis 연산 callable 목록
    (예: per-app 키 직접 DEL, 역인덱스 srem). 실패해도 로그만 남기고 계속한다.
    """
    from app.core.budget_cache import defer_redis_write_until_commit
    from app.core.db import AsyncSessionLocal

    async def _do() -> None:
        try:
            async with AsyncSessionLocal() as s2:
                if keys:
                    await cache_mgr.invalidate(keys, session=s2)
                if pattern:
                    await cache_mgr.invalidate_pattern(pattern, session=s2)
                await s2.commit()
        except Exception:
            logger.warning("cache.deferred_invalidate_failed", exc_info=True)
        for op in extra or []:
            try:
                await op()
            except Exception:
                logger.warning("cache.deferred_op_failed", exc_info=True)

    await defer_redis_write_until_commit(session, _do)
