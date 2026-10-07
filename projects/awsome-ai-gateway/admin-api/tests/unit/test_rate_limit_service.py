# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import ValidationError
from app.models.model import RateLimitScope
from app.schemas.rate_limits import RateLimitSetRequest
from app.services.rate_limit_service import RateLimitService


@pytest.fixture
def rate_limit_service(cache_mgr: CacheInvalidationManager) -> RateLimitService:
    return RateLimitService(cache_mgr=cache_mgr)


class TestCPMCPHScope:
    async def test_team_scope_allows_cpm_cph(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        data = RateLimitSetRequest(rpm=100, tpm=50000, cpm=Decimal("10.00"), cph=Decimal("50.00"))

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            result = await rate_limit_service.set_team_rate_limit(
                mock_session, team_id=uuid.uuid4(), data=data, actor=admin_user
            )

        assert result.cpm_limit_usd == Decimal("10.00")
        assert result.cph_limit_usd == Decimal("50.00")

    async def test_global_scope_rejects_cpm(
        self, rate_limit_service: RateLimitService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        data = RateLimitSetRequest(rpm=100, cpm=Decimal("10.00"))

        with pytest.raises(ValidationError, match="CPM/CPH"):
            await rate_limit_service.set_global_rate_limit(
                mock_session, model_alias="claude-sonnet", data=data, actor=admin_user
            )

    async def test_global_scope_rejects_cph(
        self, rate_limit_service: RateLimitService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        data = RateLimitSetRequest(rpm=100, cph=Decimal("50.00"))

        with pytest.raises(ValidationError, match="CPM/CPH"):
            await rate_limit_service.set_global_rate_limit(
                mock_session, model_alias="claude-sonnet", data=data, actor=admin_user
            )

    async def test_user_scope_allows_cpm_cph(
        self, rate_limit_service: RateLimitService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        data = RateLimitSetRequest(rpm=100, tpm=50000, cpm=Decimal("10.00"), cph=Decimal("50.00"))

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            result = await rate_limit_service.set_user_rate_limit(
                mock_session, user_id=uuid.uuid4(), data=data, actor=admin_user
            )

        assert result.rpm_limit == 100
        assert result.cpm_limit_usd == Decimal("10.00")
        assert result.cph_limit_usd == Decimal("50.00")
        assert result.is_active is True


class TestRateLimitCaching:
    async def test_set_rate_limit_writes_to_redis(
        self, rate_limit_service: RateLimitService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        data = RateLimitSetRequest(rpm=60, tpm=10000)
        user_id = uuid.uuid4()

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            await rate_limit_service.set_user_rate_limit(
                mock_session, user_id=user_id, data=data, actor=admin_user
            )

        mock_redis.set.assert_called_once()
        key = mock_redis.set.call_args[0][0]
        assert key == f"ratelimit:config:user:{user_id}"


class TestGatewayCacheInvalidation:
    """Verify admin-api invalidates gateway-proxy's rl:config:* cache on policy update.

    Without this, gateway can serve stale policies for up to the 5-min TTL
    (`_CONFIG_CACHE_TTL_SEC` in gateway-proxy/.../rate_limit_config_loader.py).
    """

    @staticmethod
    def _scan_iter_returning(keys: list[str]):
        async def _gen(*_args, **_kwargs):
            for k in keys:
                yield k
        return _gen

    async def test_user_set_invalidates_per_model_keys(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        data = RateLimitSetRequest(rpm=60, tpm=10000)
        user_id = uuid.uuid4()

        # Simulate gateway-proxy having cached policies for two models.
        cached_keys = [
            f"rl:config:USER:{user_id}:claude-opus",
            f"rl:config:USER:{user_id}:claude-sonnet",
        ]
        mock_redis.scan_iter = MagicMock(
            side_effect=lambda *a, **kw: self._scan_iter_returning(cached_keys)()
        )

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            await rate_limit_service.set_user_rate_limit(
                mock_session, user_id=user_id, data=data, actor=admin_user
            )

        mock_redis.scan_iter.assert_called_once()
        match_arg = mock_redis.scan_iter.call_args.kwargs.get("match") \
            or mock_redis.scan_iter.call_args.args[0]
        assert match_arg == f"rl:config:USER:{user_id}:*"

        deleted_keys = [c.args[0] for c in mock_redis.delete.call_args_list]
        for k in cached_keys:
            assert k in deleted_keys

    async def test_team_set_invalidates_per_model_keys(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        data = RateLimitSetRequest(rpm=60, tpm=10000)
        team_id = uuid.uuid4()
        cached_keys = [f"rl:config:TEAM:{team_id}:claude-opus"]
        mock_redis.scan_iter = MagicMock(
            side_effect=lambda *a, **kw: self._scan_iter_returning(cached_keys)()
        )

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            await rate_limit_service.set_team_rate_limit(
                mock_session, team_id=team_id, data=data, actor=admin_user
            )

        match_arg = mock_redis.scan_iter.call_args.kwargs.get("match") \
            or mock_redis.scan_iter.call_args.args[0]
        assert match_arg == f"rl:config:TEAM:{team_id}:*"
        assert cached_keys[0] in [c.args[0] for c in mock_redis.delete.call_args_list]

    async def test_global_set_invalidates_specific_model_key(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        # GLOBAL scope always carries a model_alias → exact-key delete, no SCAN.
        data = RateLimitSetRequest(rpm=1000, tpm=500000)

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            await rate_limit_service.set_global_rate_limit(
                mock_session, model_alias="claude-opus", data=data, actor=admin_user
            )

        deleted_keys = [c.args[0] for c in mock_redis.delete.call_args_list]
        assert "rl:config:GLOBAL:NULL:claude-opus" in deleted_keys
        mock_redis.scan_iter.assert_not_called()


class TestGetLiveUsage:
    """get_live_usage — tracked 플래그.

    proxy 는 RPM 한도가 설정된 scope 에만 카운터를 적재하므로, 한도 미설정
    scope 의 0 은 '요청 없음'이 아니라 '계량 안 함'이다. UI 가 두 상태를
    구분하도록 tracked 플래그를 내려준다 (설정 조회 실패 시 None=미상).
    """

    @staticmethod
    def _scan(keys: list[str]):
        async def _gen(*_args, **_kwargs):
            for k in keys:
                yield k
        return _gen

    async def test_tracked_when_rpm_limit_configured(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        mock_redis: AsyncMock,
    ):
        team_id = uuid.uuid4()
        cfg = MagicMock(rpm_limit=100)
        mock_redis.scan_iter = MagicMock(
            side_effect=lambda *a, **kw: self._scan(
                [f"{{TEAM:{team_id}:claude-sonnet}}:rpm"]
            )()
        )
        mock_redis.zcount = AsyncMock(return_value=5)

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo:
            MockRepo.return_value.get_active = AsyncMock(return_value=cfg)
            out = await rate_limit_service.get_live_usage(
                mock_session, "TEAM", str(team_id)
            )

        assert out["available"] is True
        assert out["tracked"] is True
        assert out["rpm_used_total"] == 5
        assert out["by_model"] == [{"model_alias": "claude-sonnet", "rpm_used": 5}]
        MockRepo.return_value.get_active.assert_awaited_once_with(
            RateLimitScope.TEAM, team_id
        )

    async def test_untracked_when_no_config(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        mock_redis: AsyncMock,
    ):
        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo:
            MockRepo.return_value.get_active = AsyncMock(return_value=None)
            out = await rate_limit_service.get_live_usage(
                mock_session, "TEAM", str(uuid.uuid4())
            )

        assert out["available"] is True
        assert out["tracked"] is False
        assert out["rpm_used_total"] == 0
        assert out["by_model"] == []

    async def test_untracked_when_rpm_null(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        mock_redis: AsyncMock,
    ):
        # TPM 만 설정된 경우 — rpm 카운터는 적재되지 않으므로 untracked.
        cfg = MagicMock(rpm_limit=None)
        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo:
            MockRepo.return_value.get_active = AsyncMock(return_value=cfg)
            out = await rate_limit_service.get_live_usage(
                mock_session, "USER", str(uuid.uuid4())
            )

        assert out["tracked"] is False

    async def test_tracked_none_on_config_lookup_error(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        mock_redis: AsyncMock,
    ):
        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo:
            MockRepo.return_value.get_active = AsyncMock(
                side_effect=RuntimeError("db down")
            )
            out = await rate_limit_service.get_live_usage(
                mock_session, "USER", str(uuid.uuid4())
            )

        assert out["available"] is True
        assert out["tracked"] is None

    async def test_global_scope_uses_null_scope_id(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        mock_redis: AsyncMock,
    ):
        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo:
            MockRepo.return_value.get_active = AsyncMock(return_value=None)
            out = await rate_limit_service.get_live_usage(
                mock_session, "GLOBAL", "claude-sonnet"
            )

        assert out["tracked"] is False
        MockRepo.return_value.get_active.assert_awaited_once_with(
            RateLimitScope.GLOBAL, None
        )


class TestGetUsageTrend:
    """get_usage_trend — usage_logs 버킷 집계 + 60s 캐시 + fail-soft."""

    @staticmethod
    def _rows_result(rows: list):
        result = MagicMock()
        result.all.return_value = rows
        return result

    @staticmethod
    def _row(ts: int, req: int, tok: int, cost):
        from datetime import datetime, timezone

        r = MagicMock()
        r.b = datetime.fromtimestamp(ts, tz=timezone.utc)
        r.req = req
        r.tok = tok
        r.cost = cost
        return r

    async def test_invalid_scope_returns_unavailable(
        self, rate_limit_service: RateLimitService, mock_session: AsyncMock
    ):
        for scope in ("GLOBAL", "DEPT", "bogus"):
            out = await rate_limit_service.get_usage_trend(
                mock_session, scope, str(uuid.uuid4()), "24h"
            )
            assert out["available"] is False
            assert out["reason"] == "invalid scope"
        mock_session.execute.assert_not_called()

    async def test_invalid_window_returns_unavailable(
        self, rate_limit_service: RateLimitService, mock_session: AsyncMock
    ):
        out = await rate_limit_service.get_usage_trend(
            mock_session, "USER", str(uuid.uuid4()), "3h"
        )
        assert out["available"] is False
        assert out["reason"] == "invalid window"
        mock_session.execute.assert_not_called()

    async def test_invalid_scope_id_returns_unavailable(
        self, rate_limit_service: RateLimitService, mock_session: AsyncMock
    ):
        out = await rate_limit_service.get_usage_trend(
            mock_session, "USER", "not-a-uuid", "24h"
        )
        assert out["available"] is False
        assert out["reason"] == "invalid scope_id"
        mock_session.execute.assert_not_called()

    async def test_happy_path_fills_zero_buckets_and_caches(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        mock_redis: AsyncMock,
    ):
        import time
        from decimal import Decimal

        user_id = uuid.uuid4()
        # 1h 창 → 60s 버킷. 현재 시각 정렬 지점 하나만 행이 있다고 가정.
        bucket_ts = int(time.time()) - (int(time.time()) % 60)
        mock_session.execute.return_value = self._rows_result(
            [self._row(bucket_ts, 3, 900, Decimal("0.5"))]
        )

        out = await rate_limit_service.get_usage_trend(
            mock_session, "USER", str(user_id), "1h"
        )

        assert out["available"] is True
        assert out["bucket_sec"] == 60
        # 1h/60s = 60 또는 61 포인트(경계 포함)
        assert len(out["points"]) in (60, 61)
        hit = [p for p in out["points"] if p["requests"] == 3]
        assert len(hit) == 1
        assert hit[0]["tokens"] == 900
        assert hit[0]["cost_usd"] == 0.5
        # 나머지 포인트는 0 채움
        assert sum(p["requests"] for p in out["points"]) == 3
        mock_redis.setex.assert_called_once()
        assert mock_redis.setex.call_args.args[0] == f"trend:rl:USER:{user_id}:1h"

    async def test_team_scope_uses_team_column(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        mock_redis: AsyncMock,
    ):
        team_id = uuid.uuid4()
        mock_session.execute.return_value = self._rows_result([])

        out = await rate_limit_service.get_usage_trend(
            mock_session, "TEAM", str(team_id), "7d"
        )

        assert out["available"] is True
        assert out["bucket_sec"] == 3600
        sql = str(mock_session.execute.call_args.args[0].text)
        assert "team_id" in sql
        assert "user_id" not in sql

    async def test_cache_hit_skips_db(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        mock_redis: AsyncMock,
    ):
        import json

        user_id = uuid.uuid4()
        cached = {"available": True, "scope": "USER", "points": []}
        mock_redis.get = AsyncMock(return_value=json.dumps(cached))

        out = await rate_limit_service.get_usage_trend(
            mock_session, "USER", str(user_id), "24h"
        )

        assert out == cached
        mock_session.execute.assert_not_called()

    async def test_db_error_fails_soft(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        mock_redis: AsyncMock,
    ):
        mock_session.execute.side_effect = RuntimeError("db down")

        out = await rate_limit_service.get_usage_trend(
            mock_session, "USER", str(uuid.uuid4()), "24h"
        )

        assert out["available"] is False
        assert out["reason"] == "RuntimeError"
