# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.cache_invalidation import CacheInvalidationManager


class TestCacheInvalidation:
    async def test_invalidate_deletes_keys(self):
        redis = AsyncMock()
        redis.delete = AsyncMock()
        mgr = CacheInvalidationManager(redis)

        await mgr.invalidate(["model:claude-sonnet", "model:list"])

        assert redis.delete.call_count == 2
        redis.delete.assert_any_call("model:claude-sonnet")
        redis.delete.assert_any_call("model:list")

    async def test_invalidate_empty_list_noop(self):
        redis = AsyncMock()
        mgr = CacheInvalidationManager(redis)

        await mgr.invalidate([])

        redis.delete.assert_not_called()

    async def test_invalidate_failure_logs_to_db(self):
        redis = AsyncMock()
        redis.delete = AsyncMock(side_effect=Exception("Redis down"))
        session = AsyncMock()
        session.add = MagicMock()

        mgr = CacheInvalidationManager(redis)
        await mgr.invalidate(["model:test"], session=session)

        # Failure recorded to DB
        session.add.assert_called_once()
        recorded = session.add.call_args[0][0]
        assert recorded.cache_key == "model:test"

    async def test_invalidate_failure_without_session_only_logs(self):
        redis = AsyncMock()
        redis.delete = AsyncMock(side_effect=Exception("Redis down"))

        mgr = CacheInvalidationManager(redis)
        # Should not raise even without session
        await mgr.invalidate(["model:test"])


class TestSwapReverseIndexMembership:
    async def test_swap_executes_srem_and_sadd(self):
        fake_redis = MagicMock()
        fake_pipe = MagicMock()
        fake_pipe.execute = AsyncMock(return_value=[1, 2])
        fake_pipe.srem = MagicMock()
        fake_pipe.sadd = MagicMock()
        fake_redis.pipeline = MagicMock(return_value=fake_pipe)

        mgr = CacheInvalidationManager(fake_redis)
        await mgr.swap_reverse_index_membership(
            old_key="team:vk_hashes:OLD",
            new_key="team:vk_hashes:NEW",
            members=["h1", "h2"],
        )

        fake_pipe.srem.assert_called_once_with("team:vk_hashes:OLD", "h1", "h2")
        fake_pipe.sadd.assert_called_once_with("team:vk_hashes:NEW", "h1", "h2")
        fake_pipe.execute.assert_awaited_once()
        fake_redis.pipeline.assert_called_once_with(transaction=False)

    async def test_swap_empty_members_noop(self):
        fake_redis = MagicMock()
        fake_redis.pipeline = MagicMock()  # should not be called

        mgr = CacheInvalidationManager(fake_redis)
        await mgr.swap_reverse_index_membership(
            old_key="team:vk_hashes:OLD",
            new_key="team:vk_hashes:NEW",
            members=[],
        )

        fake_redis.pipeline.assert_not_called()

    async def test_swap_records_failure_and_raises(self, mock_session):
        fake_redis = MagicMock()
        fake_pipe = MagicMock()
        fake_pipe.execute = AsyncMock(side_effect=ConnectionError("boom"))
        fake_pipe.srem = MagicMock()
        fake_pipe.sadd = MagicMock()
        fake_redis.pipeline = MagicMock(return_value=fake_pipe)

        mgr = CacheInvalidationManager(fake_redis)
        mgr._record_failure = AsyncMock()

        with pytest.raises(ConnectionError):
            await mgr.swap_reverse_index_membership(
                old_key="team:vk_hashes:OLD",
                new_key="team:vk_hashes:NEW",
                members=["h1"],
                session=mock_session,
            )

        mgr._record_failure.assert_awaited_once()
        args = mgr._record_failure.call_args.args
        # second positional arg is the cache_key string
        assert args[1] == "swap:team:vk_hashes:OLD->team:vk_hashes:NEW"


class TestRetryFailed:
    async def test_retry_resolves_successful(self):
        redis = AsyncMock()
        redis.delete = AsyncMock()  # Succeeds now

        mgr = CacheInvalidationManager(redis)

        from app.models.audit import CacheInvalidationFailure

        failure = MagicMock(spec=CacheInvalidationFailure)
        failure.cache_key = "model:failed-key"
        failure.resolved_at = None
        failure.retry_count = 0

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [failure]
        session.execute = AsyncMock(return_value=mock_result)

        resolved = await mgr.retry_failed(session)

        assert resolved == 1
        assert failure.resolved_at is not None

    async def test_retry_increments_count_on_failure(self):
        redis = AsyncMock()
        redis.delete = AsyncMock(side_effect=Exception("still down"))

        mgr = CacheInvalidationManager(redis)

        from app.models.audit import CacheInvalidationFailure

        failure = MagicMock(spec=CacheInvalidationFailure)
        failure.cache_key = "model:still-failed"
        failure.resolved_at = None
        failure.retry_count = 2

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [failure]
        session.execute = AsyncMock(return_value=mock_result)

        resolved = await mgr.retry_failed(session)

        assert resolved == 0
        assert failure.retry_count == 3


class TestLiveVkHashes:
    """team:vk_hashes 역인덱스 — 자연 만료로 남은 죽은 멤버를 읽는 시점에 정리."""

    async def test_prunes_dead_members_and_returns_live(self):
        """key:vk:{h} 가 없는 멤버는 srem 되고 반환값에서도 빠진다."""
        team_id = uuid.uuid4()
        redis = AsyncMock()
        redis.smembers = AsyncMock(return_value={b"live1", "dead1", b"live2"})
        # smembers 는 set 순서가 비결정 — 질의된 키 순서에 맞춰 생존 여부를 돌려준다
        exists_calls: list[str] = []
        pipe = MagicMock()
        pipe.exists = MagicMock(side_effect=lambda k: exists_calls.append(k))
        pipe.execute = AsyncMock(
            side_effect=lambda: [0 if k == "key:vk:dead1" else 1 for k in exists_calls]
        )
        redis.pipeline = MagicMock(return_value=pipe)
        mgr = CacheInvalidationManager(redis)

        live = await mgr.live_vk_hashes(team_id)

        assert sorted(live) == ["live1", "live2"]
        assert set(exists_calls) == {
            "key:vk:live1", "key:vk:dead1", "key:vk:live2"
        }
        redis.srem.assert_awaited_once_with(f"team:vk_hashes:{team_id}", "dead1")

    async def test_empty_set_returns_empty(self):
        redis = AsyncMock()
        redis.smembers = AsyncMock(return_value=set())
        mgr = CacheInvalidationManager(redis)

        assert await mgr.live_vk_hashes(uuid.uuid4()) == []
        redis.pipeline.assert_not_called()

    async def test_srem_failure_does_not_lose_live_result(self):
        """srem 실패해도 이번 읽기의 live 목록은 반환된다."""
        redis = AsyncMock()
        redis.smembers = AsyncMock(return_value={b"a", b"dead"})
        exists_calls: list[str] = []
        pipe = MagicMock()
        pipe.exists = MagicMock(side_effect=lambda k: exists_calls.append(k))
        pipe.execute = AsyncMock(
            side_effect=lambda: [0 if k == "key:vk:dead" else 1 for k in exists_calls]
        )
        redis.pipeline = MagicMock(return_value=pipe)
        redis.srem = AsyncMock(side_effect=Exception("Redis down"))
        mgr = CacheInvalidationManager(redis)

        assert await mgr.live_vk_hashes(uuid.uuid4()) == ["a"]

    async def test_read_failure_returns_empty(self):
        """smembers 자체가 실패하면 빈 리스트 — 호출부는 TTL 자연 만료에 맡긴다."""
        redis = AsyncMock()
        redis.smembers = AsyncMock(side_effect=Exception("Redis down"))
        mgr = CacheInvalidationManager(redis)

        assert await mgr.live_vk_hashes(uuid.uuid4()) == []
