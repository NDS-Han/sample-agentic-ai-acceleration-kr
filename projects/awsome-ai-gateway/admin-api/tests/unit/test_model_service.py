# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import ConflictError, NotFoundError
from app.models.model import ApiFormat, ModelAlias, ModelPricing, ModelStatus, Provider
from app.schemas.models import ModelCreateRequest, ModelUpdateRequest, PricingRequest, StatusPatchRequest
from app.schemas.common import ApiFormatEnum, ProviderEnum
from app.services.model_service import ModelService


@pytest.fixture
def model_service(cache_mgr: CacheInvalidationManager) -> ModelService:
    return ModelService(cache_mgr=cache_mgr)


def _make_model(
    alias: str = "claude-sonnet",
    description: str | None = "test model",
    display_name: str | None = None,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
) -> ModelAlias:
    m = MagicMock(spec=ModelAlias)
    m.alias = alias
    m.provider = Provider.BEDROCK
    m.provider_model_id = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    m.endpoint_url = None
    m.api_format = ApiFormat.BEDROCK_NATIVE
    m.status = ModelStatus.ACTIVE
    m.description = description
    m.display_name = display_name  # _to_response reads display_name; MagicMock would yield a non-str → pydantic error
    m.context_window = context_window
    m.max_output_tokens = max_output_tokens
    m.created_at = datetime.now(timezone.utc)
    m.updated_at = datetime.now(timezone.utc)
    return m


def _make_pricing(alias: str = "claude-sonnet") -> ModelPricing:
    p = MagicMock(spec=ModelPricing)
    p.input_price_per_1k_tokens = Decimal("0.003")
    p.output_price_per_1k_tokens = Decimal("0.015")
    p.cache_creation_5m_price_per_1k_tokens = Decimal("0.00375")
    p.cache_creation_1h_price_per_1k_tokens = Decimal("0.006")
    p.cache_read_price_per_1k_tokens = Decimal("0.0003")
    p.long_context_threshold_tokens = None
    p.long_context_input_price_per_1k_tokens = None
    p.long_context_output_price_per_1k_tokens = None
    p.long_context_cache_creation_5m_price_per_1k_tokens = None
    p.long_context_cache_creation_1h_price_per_1k_tokens = None
    p.long_context_cache_read_price_per_1k_tokens = None
    p.effective_from = datetime.now(timezone.utc)
    p.effective_until = None
    return p


class TestCreateModel:
    async def test_create_model_success(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        data = ModelCreateRequest(
            alias="claude-sonnet",
            provider=ProviderEnum.BEDROCK,
            provider_model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            api_format=ApiFormatEnum.BEDROCK_NATIVE,
            input_price_per_1k_tokens=Decimal("0.003"),
            output_price_per_1k_tokens=Decimal("0.015"),
        )

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.alias_exists_ci = AsyncMock(return_value=False)

            # Simulate DB-default timestamps that a real INSERT flush would set,
            # so _to_response (ModelResponse) validates created_at/updated_at.
            async def _set_timestamps(model):
                model.created_at = datetime.now(timezone.utc)
                model.updated_at = datetime.now(timezone.utc)

            repo.create_model = AsyncMock(side_effect=_set_timestamps)
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()

            result = await model_service.create_model(mock_session, data=data, actor=admin_user)

        assert result.alias == "claude-sonnet"
        assert result.provider == Provider.BEDROCK
        # P0-④: create_model is now invalidate-only (DEL model:{alias} + model:list);
        # it must NOT pre-seed a (previously flat, TTL-less) model cache entry.
        # The gateway populates model:{alias} with the correct nested shape + TTL
        # on first cache-miss. So we assert DEL happened and SET did not.
        assert mock_redis.delete.call_count >= 1
        mock_redis.set.assert_not_called()

    async def test_create_model_duplicate_alias_raises(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        data = ModelCreateRequest(
            alias="claude-sonnet",
            provider=ProviderEnum.BEDROCK,
            provider_model_id="test",
            api_format=ApiFormatEnum.BEDROCK_NATIVE,
            input_price_per_1k_tokens=Decimal("0.003"),
            output_price_per_1k_tokens=Decimal("0.015"),
        )

        with patch("app.services.model_service.ModelRepository") as MockRepo:
            MockRepo.return_value.alias_exists_ci = AsyncMock(return_value=True)

            with pytest.raises(ConflictError):
                await model_service.create_model(mock_session, data=data, actor=admin_user)


class TestUpdateModel:
    async def test_update_model_not_found(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        data = ModelUpdateRequest(description="new desc")

        with patch("app.services.model_service.ModelRepository") as MockRepo:
            MockRepo.return_value.update_model = AsyncMock(return_value=None)

            with pytest.raises(NotFoundError):
                await model_service.update_model(mock_session, alias="missing", data=data, actor=admin_user)

    async def test_update_model_invalidates_cache(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        data = ModelUpdateRequest(description="updated")
        model = _make_model()
        pricing = _make_pricing()

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.update_model = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=pricing)
            mock_audit.log = AsyncMock()

            await model_service.update_model(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        # Cache invalidation called
        assert mock_redis.delete.call_count >= 1

    async def test_update_model_explicit_null_clears_description_and_display_name(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        # 폼이 필드를 비워 보낸 경우(명시적 null) — 기존값을 지우는 게 의도된 동작.
        # "키 생략"과 구별하므로 생략된 필드는 유지돼야 한다.
        data = ModelUpdateRequest(description=None, display_name=None)
        model = _make_model(description="old desc", display_name="Old Name")
        pricing = _make_pricing()

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.update_model = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=pricing)
            mock_audit.log = AsyncMock()

            await model_service.update_model(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        assert model.description is None
        assert model.display_name is None
        mock_session.flush.assert_awaited()

    async def test_update_model_omitted_nullable_fields_are_preserved(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        # 키 자체를 안 보낸 경우 — 기존 "null=무시" 관례대로 값이 유지돼야 한다.
        # (명시적 null 과 생략의 구별이 핵심 — 섞이면 PATCH 가 부분 업데이트를 못 한다.)
        data = ModelUpdateRequest(provider_model_id="new-id")
        model = _make_model(description="keep me", display_name="Keep")
        pricing = _make_pricing()

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.update_model = AsyncMock(return_value=model)
            repo.get_by_alias = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=pricing)
            mock_audit.log = AsyncMock()

            await model_service.update_model(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        assert model.description == "keep me"
        assert model.display_name == "Keep"

    async def test_update_model_pmid_change_invalidates_old_pmid_and_nx(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        # pmid 변경 시 게이트웨이 캐시의 옛 pmid 키(model:pmid:{old})가 남으면
        # 최대 300s 동안 옛 pmid 참조 요청이 옛 스키마(단가·ACL·pmid)로 서빙된다.
        # 새 pmid 의 15s 음수캐시(model:nx:*:{new})도 함께 지워야 즉시 해석된다.
        data = ModelUpdateRequest(provider_model_id="new-pmid-2")
        before = _make_model()  # provider_model_id = 기본(old pmid)
        after = _make_model()
        after.provider_model_id = "new-pmid-2"
        pricing = _make_pricing()

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.get_by_alias = AsyncMock(return_value=before)
            repo.update_model = AsyncMock(return_value=after)
            repo.get_current_pricing = AsyncMock(return_value=pricing)
            mock_audit.log = AsyncMock()

            await model_service.update_model(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        deleted = {c.args[0] for c in mock_redis.delete.await_args_list}
        assert f"model:pmid:{before.provider_model_id}" in deleted  # 옛 pmid 키 제거
        assert "model:pmid:new-pmid-2" in deleted
        assert "model:nx:new-pmid-2" in deleted  # 레거시 단일 nx 키
        patterns = {c.kwargs.get("match") or c.args[0] for c in mock_redis.scan_iter.call_args_list}
        assert "model:nx:*:new-pmid-2" in patterns  # provider 네임스페이스 nx 패턴

    async def test_update_model_unchanged_pmid_skips_old_key_invalidation(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        # pmid 를 안 바꾸는 update 는 옛 pmid 조회(get_by_alias)도 하지 않는다.
        data = ModelUpdateRequest(description="only desc")
        model = _make_model()
        pricing = _make_pricing()

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.get_by_alias = AsyncMock(return_value=model)
            repo.update_model = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=pricing)
            mock_audit.log = AsyncMock()

            await model_service.update_model(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        repo.get_by_alias.assert_not_awaited()


class TestSpecFields:
    """context_window / max_output_tokens — 생성 시 기록 + 편집 시 fields_set 규칙."""

    async def test_create_model_persists_spec_fields(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        data = ModelCreateRequest(
            alias="claude-sonnet",
            provider=ProviderEnum.BEDROCK,
            provider_model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            api_format=ApiFormatEnum.BEDROCK_NATIVE,
            input_price_per_1k_tokens=Decimal("0.003"),
            output_price_per_1k_tokens=Decimal("0.015"),
            context_window=200000,
            max_output_tokens=64000,
        )

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.alias_exists_ci = AsyncMock(return_value=False)

            async def _set_timestamps(model):
                model.created_at = datetime.now(timezone.utc)
                model.updated_at = datetime.now(timezone.utc)

            repo.create_model = AsyncMock(side_effect=_set_timestamps)
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()

            result = await model_service.create_model(mock_session, data=data, actor=admin_user)

        created = repo.create_model.call_args.args[0]
        assert created.context_window == 200000
        assert created.max_output_tokens == 64000
        assert result.context_window == 200000
        assert result.max_output_tokens == 64000

    async def test_update_model_sets_spec_values_via_repo(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        # 값이 들어오면 repo kwargs 로 넘겨 저장한다.
        data = ModelUpdateRequest(context_window=400000)
        model = _make_model(context_window=200000)

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.update_model = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=_make_pricing())
            mock_audit.log = AsyncMock()

            await model_service.update_model(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        assert repo.update_model.call_args.kwargs["context_window"] == 400000

    async def test_update_model_explicit_null_clears_spec_fields(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        # 편집 폼이 필드를 비워 보낸 경우(명시적 null) — "미상" 으로 되돌리는 게 의도된 동작.
        data = ModelUpdateRequest(context_window=None, max_output_tokens=None)
        model = _make_model(context_window=200000, max_output_tokens=64000)

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.update_model = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=_make_pricing())
            mock_audit.log = AsyncMock()

            await model_service.update_model(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        assert model.context_window is None
        assert model.max_output_tokens is None
        mock_session.flush.assert_awaited()

    async def test_update_model_omitted_spec_fields_are_preserved(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        # 키 자체를 안 보낸 경우 — 기존 값 유지(repo kwargs 에도 안 실린다).
        data = ModelUpdateRequest(provider_model_id="new-id")
        model = _make_model(context_window=200000, max_output_tokens=64000)

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.update_model = AsyncMock(return_value=model)
            repo.get_by_alias = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=_make_pricing())
            mock_audit.log = AsyncMock()

            await model_service.update_model(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        assert "context_window" not in repo.update_model.call_args.kwargs
        assert "max_output_tokens" not in repo.update_model.call_args.kwargs
        assert model.context_window == 200000


class TestSetPricing:
    async def test_set_pricing_preserves_history(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        data = PricingRequest(
            input_price_per_1k_tokens=Decimal("0.005"),
            output_price_per_1k_tokens=Decimal("0.025"),
            effective_from=datetime.now(timezone.utc),
        )
        model = _make_model()

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.get_by_alias = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=None)
            repo.close_current_pricing = AsyncMock()
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()

            await model_service.set_pricing(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        repo.close_current_pricing.assert_called_once()
        repo.create_pricing.assert_called_once()

    async def test_set_pricing_persists_cache_prices(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """Bedrock Prompt Caching 단가(cache_creation/cache_read)가 DB ORM까지 전달되어야 함."""
        data = PricingRequest(
            input_price_per_1k_tokens=Decimal("0.003"),
            output_price_per_1k_tokens=Decimal("0.015"),
            cache_creation_5m_price_per_1k_tokens=Decimal("0.00375"),
            cache_creation_1h_price_per_1k_tokens=Decimal("0.006"),
            cache_read_price_per_1k_tokens=Decimal("0.0003"),
            effective_from=datetime.now(timezone.utc),
        )
        model = _make_model()

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.get_by_alias = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=None)
            repo.close_current_pricing = AsyncMock()
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()

            result = await model_service.set_pricing(
                mock_session, alias="claude-sonnet", data=data, actor=admin_user
            )

        created = repo.create_pricing.call_args.args[0]
        assert created.cache_creation_5m_price_per_1k_tokens == Decimal("0.00375")
        assert created.cache_creation_1h_price_per_1k_tokens == Decimal("0.006")
        assert created.cache_read_price_per_1k_tokens == Decimal("0.0003")
        assert result.current_pricing is not None
        assert result.current_pricing.cache_creation_5m_price_per_1k_tokens == Decimal("0.00375")
        assert result.current_pricing.cache_creation_1h_price_per_1k_tokens == Decimal("0.006")
        assert result.current_pricing.cache_read_price_per_1k_tokens == Decimal("0.0003")

    async def test_set_pricing_defaults_cache_to_zero(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """cache_* 필드 생략 시 기본 0 (하위 호환, OPENMODEL 경로 등)."""
        data = PricingRequest(
            input_price_per_1k_tokens=Decimal("0.001"),
            output_price_per_1k_tokens=Decimal("0.002"),
            effective_from=datetime.now(timezone.utc),
        )
        assert data.cache_creation_5m_price_per_1k_tokens == Decimal("0")
        assert data.cache_creation_1h_price_per_1k_tokens == Decimal("0")
        assert data.cache_read_price_per_1k_tokens == Decimal("0")

        model = _make_model()
        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.get_by_alias = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=None)
            repo.close_current_pricing = AsyncMock()
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()

            await model_service.set_pricing(
                mock_session, alias="llama-3-70b", data=data, actor=admin_user
            )

        created = repo.create_pricing.call_args.args[0]
        assert created.cache_creation_5m_price_per_1k_tokens == Decimal("0")
        assert created.cache_read_price_per_1k_tokens == Decimal("0")


    async def _set_pricing_with_prior(self, model_service, mock_session, admin_user, prior):
        data = PricingRequest(
            input_price_per_1k_tokens=Decimal("0.00012"),
            output_price_per_1k_tokens=Decimal("0.0006"),
            cache_creation_5m_price_per_1k_tokens=Decimal("0.00015"),
            cache_creation_1h_price_per_1k_tokens=Decimal("0.00024"),
            cache_read_price_per_1k_tokens=Decimal("0.000012"),
            effective_from=datetime.now(timezone.utc),
        )
        order: list[str] = []
        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.get_by_alias = AsyncMock(return_value=_make_model("claude-haiku-5-5"))
            repo.get_current_pricing = AsyncMock(
                side_effect=lambda *a, **k: order.append("read") or prior)
            repo.close_current_pricing = AsyncMock(
                side_effect=lambda *a, **k: order.append("close"))
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()
            await model_service.set_pricing(
                mock_session, alias="claude-haiku-5-5", data=data, actor=admin_user
            )
        return repo.create_pricing.call_args.args[0], order

    async def test_set_pricing_inherits_long_context_tier(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """US-19: 단가 수정(관리 화면·가격 동기화)은 구간 단가를 요청에 싣지 않는다. 새 행이
        구간 6열을 NULL 로 만들면 100K 초과 요청이 조용히 1/5 로 기록되므로, 닫는 행의 값을
        이어받아야 한다 — 그리고 그 행은 닫기 **전에** 읽어야 한다."""
        prior = _make_pricing("claude-haiku-5-5")
        prior.long_context_threshold_tokens = 100000
        prior.long_context_input_price_per_1k_tokens = Decimal("0.00055")
        prior.long_context_output_price_per_1k_tokens = Decimal("0.00275")
        prior.long_context_cache_creation_5m_price_per_1k_tokens = Decimal("0.0006875")
        prior.long_context_cache_creation_1h_price_per_1k_tokens = Decimal("0.0011")
        prior.long_context_cache_read_price_per_1k_tokens = Decimal("0.000055")

        created, order = await self._set_pricing_with_prior(
            model_service, mock_session, admin_user, prior)

        assert order == ["read", "close"]
        assert created.long_context_threshold_tokens == 100000
        assert created.long_context_input_price_per_1k_tokens == Decimal("0.00055")
        assert created.long_context_output_price_per_1k_tokens == Decimal("0.00275")
        assert created.long_context_cache_creation_5m_price_per_1k_tokens == Decimal("0.0006875")
        assert created.long_context_cache_creation_1h_price_per_1k_tokens == Decimal("0.0011")
        assert created.long_context_cache_read_price_per_1k_tokens == Decimal("0.000055")
        # 요청이 바꾼 기본 단가는 그대로 새 값
        assert created.input_price_per_1k_tokens == Decimal("0.00012")

    async def test_set_pricing_prior_without_tier_stays_flat(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        prior = _make_pricing()
        prior.long_context_threshold_tokens = None
        prior.long_context_input_price_per_1k_tokens = None
        prior.long_context_output_price_per_1k_tokens = None
        prior.long_context_cache_creation_5m_price_per_1k_tokens = None
        prior.long_context_cache_creation_1h_price_per_1k_tokens = None
        prior.long_context_cache_read_price_per_1k_tokens = None

        created, _ = await self._set_pricing_with_prior(
            model_service, mock_session, admin_user, prior)

        assert created.long_context_threshold_tokens is None
        assert created.long_context_input_price_per_1k_tokens is None

    async def test_set_pricing_first_row_has_no_tier(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        created, _ = await self._set_pricing_with_prior(
            model_service, mock_session, admin_user, None)

        assert created.long_context_threshold_tokens is None
        assert created.long_context_cache_read_price_per_1k_tokens is None


class TestPatchStatus:
    async def test_patch_status_to_inactive_invalidates_cache(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        data = StatusPatchRequest(active=False)
        model = _make_model()
        model.status = ModelStatus.INACTIVE

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.patch_status = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=_make_pricing())
            mock_audit.log = AsyncMock()

            result = await model_service.patch_status(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        assert result.status == "INACTIVE"
        assert mock_redis.delete.call_count >= 1


class TestDisplayName:
    async def test_display_name_falls_back_to_alias(
        self, model_service: ModelService, mock_session: AsyncMock
    ):
        """display_name NULL → 응답엔 alias — '비우면 alias 사용' 안내와 일치."""
        model = _make_model()
        model.display_name = None
        resp = model_service._to_response(model, None)
        assert resp.display_name == model.alias

        model.display_name = "Sonnet 5 (표시명)"
        resp = model_service._to_response(model, None)
        assert resp.display_name == "Sonnet 5 (표시명)"


# ── 모델 삭제 (deprecated 정리) ──────────────────────────────────────────────
#
# 정책: usage_logs 는 남기고 카탈로그만 지운다. 참조별 처리:
#   to_model_alias → 409(다른 모델의 살아있는 fallback 목적지)
#   나머지(pricing/허용목록/rate limit/from 정책) → 같은 트랜잭션에서 정리.


def _count_seq(mock_session: AsyncMock, counts: list[int]) -> None:
    """deletion_impact 의 7개 count 질의에 순서대로 scalar 값을 돌려준다."""
    results = [MagicMock(scalar_one=MagicMock(return_value=n)) for n in counts]
    mock_session.execute.side_effect = results + [MagicMock() for _ in range(16)]


class TestDeleteModel:
    async def test_impact_reports_counts_and_block_flag(
        self, model_service: ModelService, mock_session: AsyncMock
    ):
        model = _make_model()
        _count_seq(mock_session, [1234, 2, 3, 1, 2, 1, 1])

        with patch("app.services.model_service.ModelRepository") as MockRepo:
            MockRepo.return_value.get_by_alias = AsyncMock(return_value=model)
            impact = await model_service.deletion_impact(mock_session, alias="claude-sonnet")

        assert impact.usage_logs == 1234
        assert impact.downgrade_to == 1
        assert impact.blocked is True

    async def test_impact_404_on_unknown_alias(
        self, model_service: ModelService, mock_session: AsyncMock
    ):
        with patch("app.services.model_service.ModelRepository") as MockRepo:
            MockRepo.return_value.get_by_alias = AsyncMock(return_value=None)
            with pytest.raises(NotFoundError):
                await model_service.deletion_impact(mock_session, alias="ghost")

    async def test_delete_blocked_when_model_is_downgrade_target(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """to_model_alias 참조가 있으면 409 — 정책을 몰래 지우면 다른 모델의
        예산 초과 시 전환이 아니라 에러가 난다."""
        model = _make_model()
        _count_seq(mock_session, [0, 0, 0, 0, 0, 0, 2])  # downgrade_to=2

        with patch("app.services.model_service.ModelRepository") as MockRepo:
            MockRepo.return_value.get_by_alias = AsyncMock(return_value=model)
            with pytest.raises(ConflictError):
                await model_service.delete_model(
                    mock_session, alias="claude-sonnet", actor=admin_user
                )
        # delete 계열 실행이 없어야 한다 — count 질의 7건만.
        assert mock_session.execute.call_count == 7

    async def test_delete_cascades_children_and_keeps_usage_logs(
        self, model_service: ModelService, mock_session: AsyncMock,
        admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        """사용 이력이 있어도 삭제 가능 — 자식 설정만 정리, usage_logs 는 유지."""
        model = _make_model()
        _count_seq(mock_session, [500, 3, 2, 1, 1, 1, 0])  # downgrade_to=0

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            mock_audit.log = AsyncMock()
            MockRepo.return_value.get_by_alias = AsyncMock(return_value=model)
            out = await model_service.delete_model(
                mock_session, alias="claude-sonnet", actor=admin_user
            )

        # 7 counts + 6 deletes (pricing/team/user/rate_limit/downgrade_from/alias)
        assert mock_session.execute.call_count == 13
        assert out.alias == "claude-sonnet"
        assert out.deleted.usage_logs == 500

        # 캐시 무효화 — 게이트웨이가 삭제된 모델을 계속 라우팅하면 안 된다.
        mock_redis.delete.assert_called()
        deleted_keys = {c.args[0] for c in mock_redis.delete.call_args_list}
        assert "model:claude-sonnet" in deleted_keys or mock_redis.execute.called

        mock_audit.log.assert_called_once()
        kw = mock_audit.log.call_args.kwargs
        assert kw["action"] == "DELETE_MODEL"
        assert kw["changes"]["usage_logs_retained"] == 500

    async def test_delete_404_on_unknown_alias(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        with patch("app.services.model_service.ModelRepository") as MockRepo:
            MockRepo.return_value.get_by_alias = AsyncMock(return_value=None)
            with pytest.raises(NotFoundError):
                await model_service.delete_model(
                    mock_session, alias="ghost", actor=admin_user
                )


# ── F6: endpoint_url SSRF 검증 ──────────────────────────────────────────────


def _create_kwargs(**over):
    base = dict(
        alias="m1",
        provider=ProviderEnum.BEDROCK,
        provider_model_id="anthropic.test-v1:0",
        api_format=ApiFormatEnum.BEDROCK_NATIVE,
        input_price_per_1k_tokens=Decimal("0.003"),
        output_price_per_1k_tokens=Decimal("0.015"),
    )
    base.update(over)
    return base


class TestEndpointUrlValidation:
    """endpoint_url 은 어댑터가 그대로 POST 대상으로 쓴다 — SSRF 표면 차단."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",       # EC2 메타데이터
            "http://169.254.170.2/v2/credentials",            # ECS 태스크 메타데이터
            "http://127.0.0.1:8080/admin",                    # loopback
            "http://localhost/v1",                            # loopback 이름
            "http://0.0.0.0/",                                # unspecified
            "file:///etc/passwd",                             # 비-http 스킴
            "gopher://internal/",                             # 비-http 스킴
            "ftp://host/",                                    # 비-http 스킴
            "http://user:pass@model.internal/v1",             # userinfo 자격증명
            "https://model.internal/v1#frag",                 # fragment
            "http://2130706433/",                             # decimal → 127.0.0.1
            "http://0x7f000001/",                             # hex → 127.0.0.1
            "http://127.1/",                                  # 축약 → 127.0.0.1
            "http://localhost./",                             # trailing dot → 127.0.0.1
            "http://0/",                                      # → 0.0.0.0
            "http://[fd00:ec2::254]/",                        # EC2 IMDS IPv6 (ULA)
        ],
    )
    def test_rejects_dangerous_endpoint(self, url):
        with pytest.raises(Exception):
            ModelCreateRequest(**_create_kwargs(endpoint_url=url))

    def test_rejects_dns_name_resolving_to_metadata_ip(self, monkeypatch):
        """nip.io 류 — 이름은 평범해 보여도 리졸브 결과가 메타데이터 IP면 차단."""
        import socket as _socket

        def _fake_getaddrinfo(host, port, *a, **kw):
            return [
                (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
            ]

        monkeypatch.setattr(_socket, "getaddrinfo", _fake_getaddrinfo)
        with pytest.raises(Exception):
            ModelCreateRequest(
                **_create_kwargs(endpoint_url="http://sneaky.example/v1")
            )

    def test_unresolvable_internal_name_allowed(self, monkeypatch):
        """admin-api 에서 리졸브 안 되는 사내 이름 — 리터럴 검사만 적용하고 허용."""
        import socket as _socket

        def _fail_getaddrinfo(host, port, *a, **kw):
            raise _socket.gaierror("name or service not known")

        monkeypatch.setattr(_socket, "getaddrinfo", _fail_getaddrinfo)
        req = ModelCreateRequest(**_create_kwargs(endpoint_url="http://vllm.internal:8000"))
        assert req.endpoint_url == "http://vllm.internal:8000"

    @pytest.mark.parametrize(
        "url",
        [
            "https://bedrock-mantle.ap-south-1.amazonaws.com",
            "http://vllm.internal:8000",                      # 사내 vLLM (정당)
            "http://10.0.1.20:8080/v1",                       # RFC1918 사설 IP (정당)
            "https://models.example.com:8443/api",
        ],
    )
    def test_accepts_legit_endpoint(self, url):
        req = ModelCreateRequest(**_create_kwargs(endpoint_url=url))
        assert req.endpoint_url == url

    def test_update_rejects_metadata_ip(self):
        with pytest.raises(Exception):
            ModelUpdateRequest(endpoint_url="http://169.254.169.254/")


class TestPriceSyncProviderGate:
    """PRICE_SYNC_PROVIDERS 게이트 — provider 가 "plane" 이름이라 Mantle/runtime
    행도 Bedrock 단가로 sync 된다. `!= BEDROCK` 게이트가 BEDROCK_MANTLE_OPENAI
    (gpt-6.1-sol)·BEDROCK_RUNTIME_OPENAI(gpt-5.6-*) 전부를 스킵하던 실측 사고."""

    def _repo_with(self, models):
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=models)
        repo.get_current_pricing = AsyncMock(return_value=_make_pricing())
        return repo

    def _sync_src(self, hit: bool = True):
        from app.services.pricing_sync_service import FetchResult, NormalizedPrice

        src = MagicMock()
        src.region = "ap-south-1"  # getattr(src, "region") 이 MagicMock 을 돌려주면 pydantic str 검증 실패
        fetched = FetchResult()
        if hit:
            fetched.prices["global.openai.gpt-6.1-sol"] = NormalizedPrice(
                input_per_1k=Decimal("0.002"),
                output_per_1k=Decimal("0.01"),
                cache_5m_per_1k=Decimal("0.0025"),
                cache_1h_per_1k=Decimal("0.004"),
                cache_read_per_1k=Decimal("0.0002"),
            )
        src.fetch_bedrock_prices = AsyncMock(return_value=fetched)
        return src

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "provider",
        [Provider.BEDROCK_MANTLE, Provider.BEDROCK_MANTLE_OPENAI, Provider.BEDROCK_RUNTIME_OPENAI],
    )
    async def test_bedrock_plane_providers_are_synced(self, provider, monkeypatch):
        """Mantle/runtime 계열도 preview 대상 — LiteLLM 카탈로그에 해당 id 의 단가 존재."""
        m = _make_model(alias="gpt-6.1-sol")
        m.provider = provider
        m.provider_model_id = "global.openai.gpt-6.1-sol"
        m.allowed_clients = []
        repo = self._repo_with([m])
        monkeypatch.setattr(
            "app.services.model_service.ModelRepository", lambda sess: repo
        )

        svc = ModelService(MagicMock())
        out = await svc.preview_price_sync(
            MagicMock(), pricing_sync_service=self._sync_src()
        )

        assert len(out.diffs) == 1
        assert out.diffs[0].matched is True, "Bedrock-계열 provider 가 sync 게이트에서 스킵됐다"

    @pytest.mark.asyncio
    async def test_openmodel_still_skipped(self, monkeypatch):
        m = _make_model(alias="llama-3-70b")
        m.provider = Provider.OPENMODEL
        m.allowed_clients = []
        repo = self._repo_with([m])
        monkeypatch.setattr(
            "app.services.model_service.ModelRepository", lambda sess: repo
        )
        svc = ModelService(MagicMock())
        out = await svc.preview_price_sync(
            MagicMock(), pricing_sync_service=self._sync_src()
        )
        assert out.diffs == []


class TestPriceSyncApply:
    """apply 안전 계약 — 미래 예약 행 보호(F3), 승인값 드리프트 거부(F4),
    장문 구간 값이 set_pricing 까지 흐름(F2)."""

    def _session(self):
        """begin_nested savepoint 를 정직하게 모사 — MagicMock 기본 __aexit__ 의
        반환값은 truthy 라 예외를 삼켜버리므로 False 를 명시한다."""
        sess = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=None)
        ctx.__aexit__ = AsyncMock(return_value=False)
        sess.begin_nested = MagicMock(return_value=ctx)
        return sess

    def _repo_with(self, model, pricing=None):
        repo = MagicMock()
        repo.get_by_alias = AsyncMock(return_value=model)
        repo.get_current_pricing = AsyncMock(return_value=pricing or _make_pricing())
        return repo

    def _sync_src(self, np=None):
        from app.services.pricing_sync_service import FetchResult, NormalizedPrice

        src = MagicMock()
        fetched = FetchResult()
        fetched.prices["global.openai.gpt-6.1-sol"] = np or NormalizedPrice(
            input_per_1k=Decimal("0.002"),
            output_per_1k=Decimal("0.01"),
            cache_5m_per_1k=Decimal("0.0025"),
            cache_1h_per_1k=Decimal("0.004"),
            cache_read_per_1k=Decimal("0.0002"),
        )
        src.fetch_bedrock_prices = AsyncMock(return_value=fetched)
        return src

    def _model(self):
        m = _make_model(alias="gpt-6.1-sol")
        m.provider = Provider.BEDROCK_MANTLE_OPENAI
        m.provider_model_id = "global.openai.gpt-6.1-sol"
        m.allowed_clients = []
        m.context_window = 272000
        m.max_output_tokens = 32000
        return m

    def _expected_for_gpt(self):
        """_sync_src 기본값과 일치하는 승인값 — expected 게이트 통과용."""
        from app.schemas.models import PriceSyncExpected

        return {
            "gpt-6.1-sol": PriceSyncExpected(
                input_per_1k=Decimal("0.002"),
                output_per_1k=Decimal("0.01"),
                cache_5m_per_1k=Decimal("0.0025"),
                cache_1h_per_1k=Decimal("0.004"),
                cache_read_per_1k=Decimal("0.0002"),
            )
        }

    @pytest.mark.asyncio
    async def test_future_dated_row_blocks_apply(self, monkeypatch):
        """미래 시작 예약 단가가 있으면 자동 적용 보류 — 역전 행·예약 소실 금지."""
        from datetime import timedelta

        future_row = _make_pricing()
        future_row.effective_from = datetime.now(timezone.utc) + timedelta(days=7)
        m = self._model()
        repo = self._repo_with(m, future_row)
        monkeypatch.setattr(
            "app.services.model_service.ModelRepository", lambda sess: repo
        )
        svc = ModelService(MagicMock())
        svc.set_pricing = AsyncMock()

        out = await svc.apply_price_sync(
            self._session(),
            pricing_sync_service=self._sync_src(),
            aliases=["gpt-6.1-sol"],
            actor=MagicMock(),
            expected=self._expected_for_gpt(),
        )
        assert out.applied == []
        assert "gpt-6.1-sol" in out.skipped
        assert any("예약" in e for e in out.errors)
        svc.set_pricing.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_expected_alias_rejects(self, monkeypatch):
        """승인값 맵에 없는 alias 는 적용 거부 — 승인 없는 재수집 값 기입 차단."""
        m = self._model()
        repo = self._repo_with(m)
        monkeypatch.setattr(
            "app.services.model_service.ModelRepository", lambda sess: repo
        )
        svc = ModelService(MagicMock())
        svc.set_pricing = AsyncMock()

        out = await svc.apply_price_sync(
            self._session(),
            pricing_sync_service=self._sync_src(),
            aliases=["gpt-6.1-sol"],
            actor=MagicMock(),
            expected={"other-alias": self._expected_for_gpt()["gpt-6.1-sol"]},
        )
        assert out.applied == []
        assert "gpt-6.1-sol" in out.skipped
        assert any("승인된 단가" in e for e in out.errors)
        svc.set_pricing.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_long_cache_drift_rejects(self, monkeypatch):
        """구간 캐시 단가만 바뀌어도 승인값 대조가 잡는다 — 보이지 않는 필드의
        드리프트가 조용히 통과하면 안 된다."""
        from app.schemas.models import PriceSyncExpected
        from app.services.pricing_sync_service import NormalizedPrice

        np = NormalizedPrice(
            input_per_1k=Decimal("0.002"),
            output_per_1k=Decimal("0.01"),
            cache_5m_per_1k=Decimal("0.0025"),
            cache_1h_per_1k=Decimal("0.004"),
            cache_read_per_1k=Decimal("0.0002"),
            long_context_threshold_tokens=272000,
            long_input_per_1k=Decimal("0.004"),
            long_output_per_1k=Decimal("0.015"),
            long_cache_5m_per_1k=Decimal("0.006"),   # 승인값과 다름
            long_cache_1h_per_1k=Decimal("0.008"),
            long_cache_read_per_1k=Decimal("0.0004"),
        )
        m = self._model()
        repo = self._repo_with(m)
        monkeypatch.setattr(
            "app.services.model_service.ModelRepository", lambda sess: repo
        )
        svc = ModelService(MagicMock())
        svc.set_pricing = AsyncMock()

        out = await svc.apply_price_sync(
            self._session(),
            pricing_sync_service=self._sync_src(np),
            aliases=["gpt-6.1-sol"],
            actor=MagicMock(),
            expected={
                "gpt-6.1-sol": PriceSyncExpected(
                    input_per_1k=Decimal("0.002"),
                    output_per_1k=Decimal("0.01"),
                    cache_5m_per_1k=Decimal("0.0025"),
                    cache_1h_per_1k=Decimal("0.004"),
                    cache_read_per_1k=Decimal("0.0002"),
                    long_context_threshold_tokens=272000,
                    long_input_per_1k=Decimal("0.004"),
                    long_output_per_1k=Decimal("0.015"),
                    long_cache_5m_per_1k=Decimal("0.005"),  # preview 당시 값
                    long_cache_1h_per_1k=Decimal("0.008"),
                    long_cache_read_per_1k=Decimal("0.0004"),
                )
            },
        )
        assert out.applied == []
        assert any("승인값" in e for e in out.errors)
        svc.set_pricing.assert_not_awaited()

    def test_pricing_request_threshold_requires_tier_prices(self):
        """threshold 만 싣고 in/out 결손인 요청은 거부 — 구간 단가 없는 구간
        선언은 기본 단가로 과소청구되는 조용한 상태를 만든다."""
        import pytest as _p

        from app.schemas.models import PricingRequest

        base = dict(
            input_price_per_1k_tokens=Decimal("0.001"),
            output_price_per_1k_tokens=Decimal("0.002"),
            effective_from=datetime.now(timezone.utc),
        )
        with _p.raises(Exception):
            PricingRequest(**base, long_context_threshold_tokens=272000)
        # in/out 동반 시 통과
        req = PricingRequest(
            **base,
            long_context_threshold_tokens=272000,
            long_context_input_price_per_1k_tokens=Decimal("0.002"),
            long_context_output_price_per_1k_tokens=Decimal("0.004"),
        )
        assert req.long_context_threshold_tokens == 272000
        # threshold 0 은 무의미(모든 토큰이 구간 요율) — 거부
        with _p.raises(Exception):
            PricingRequest(**base, long_context_threshold_tokens=0)

    @pytest.mark.asyncio
    async def test_one_bad_alias_does_not_abort_batch(self, monkeypatch):
        """예약 행 충돌 alias 가 있어도 나머지 alias 는 적용돼야 한다 —
        배치가 단일 예외로 통째 롤백되면 안 된다(per-alias savepoint)."""
        from datetime import timedelta

        future_row = _make_pricing()
        future_row.effective_from = datetime.now(timezone.utc) + timedelta(days=7)
        normal_row = _make_pricing()
        m1, m2 = self._model(), self._model()
        m2.alias = "gpt-5.6-sol"
        m2.provider_model_id = "global.openai.gpt-6.1-sol"  # 같은 카탈로그 키

        repo = MagicMock()
        repo.get_by_alias = AsyncMock(
            side_effect=lambda a: {"gpt-6.1-sol": m1, "gpt-5.6-sol": m2}.get(a)
        )
        repo.get_current_pricing = AsyncMock(
            side_effect=lambda a: future_row if a == "gpt-6.1-sol" else normal_row
        )
        monkeypatch.setattr(
            "app.services.model_service.ModelRepository", lambda sess: repo
        )
        svc = ModelService(MagicMock())
        svc.set_pricing = AsyncMock()

        from app.schemas.models import PriceSyncExpected

        exp = PriceSyncExpected(
            input_per_1k=Decimal("0.002"),
            output_per_1k=Decimal("0.01"),
            cache_5m_per_1k=Decimal("0.0025"),
            cache_1h_per_1k=Decimal("0.004"),
            cache_read_per_1k=Decimal("0.0002"),
        )
        out = await svc.apply_price_sync(
            self._session(),
            pricing_sync_service=self._sync_src(),
            aliases=["gpt-6.1-sol", "gpt-5.6-sol"],
            actor=MagicMock(),
            expected={"gpt-6.1-sol": exp, "gpt-5.6-sol": exp},
        )
        assert out.applied == ["gpt-5.6-sol"]
        assert out.skipped == ["gpt-6.1-sol"]
        svc.set_pricing.assert_awaited_once()  # 정상 행만

    @pytest.mark.asyncio
    async def test_set_pricing_rejects_backdated_or_scheduled_conflict(self, monkeypatch):
        """수동 PUT 포함 모든 경로에서 시간 역전 금지 — open 행이 미래 시작
        예약 행이면 now 적용은 역전 행을 만들어 예약을 소실시킨다."""
        from datetime import timedelta

        from app.core.exceptions import ConflictError
        from app.schemas.models import PricingRequest

        prior = _make_pricing()
        prior.effective_from = datetime.now(timezone.utc) + timedelta(days=7)
        m = self._model()
        repo = MagicMock()
        repo.get_by_alias = AsyncMock(return_value=m)
        repo.get_current_pricing = AsyncMock(return_value=prior)
        monkeypatch.setattr(
            "app.services.model_service.ModelRepository", lambda sess: repo
        )
        svc = ModelService(MagicMock())
        with pytest.raises(ConflictError):
            await svc.set_pricing(
                MagicMock(),
                alias="gpt-6.1-sol",
                data=PricingRequest(
                    input_price_per_1k_tokens=Decimal("0.001"),
                    output_price_per_1k_tokens=Decimal("0.002"),
                    effective_from=datetime.now(timezone.utc),
                ),
                actor=MagicMock(),
            )

    @pytest.mark.asyncio
    async def test_expected_drift_rejects(self, monkeypatch):
        """preview 승인값과 재수집 값이 다르면 적용 거부."""
        from app.schemas.models import PriceSyncExpected

        m = self._model()
        repo = self._repo_with(m)
        monkeypatch.setattr(
            "app.services.model_service.ModelRepository", lambda sess: repo
        )
        svc = ModelService(MagicMock())
        svc.set_pricing = AsyncMock()

        out = await svc.apply_price_sync(
            self._session(),
            pricing_sync_service=self._sync_src(),
            aliases=["gpt-6.1-sol"],
            actor=MagicMock(),
            expected={
                "gpt-6.1-sol": PriceSyncExpected(
                    input_per_1k=Decimal("0.999"),  # 카탈로그가 달라진 상황
                    output_per_1k=Decimal("0.01"),
                    cache_5m_per_1k=Decimal("0.0025"),
                    cache_1h_per_1k=Decimal("0.004"),
                    cache_read_per_1k=Decimal("0.0002"),
                )
            },
        )
        assert out.applied == []
        assert any("승인값" in e for e in out.errors)
        svc.set_pricing.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expected_match_applies(self, monkeypatch):
        """승인값이 일치하면 적용 진행."""
        from app.schemas.models import PriceSyncExpected

        m = self._model()
        repo = self._repo_with(m)
        monkeypatch.setattr(
            "app.services.model_service.ModelRepository", lambda sess: repo
        )
        svc = ModelService(MagicMock())
        svc.set_pricing = AsyncMock()

        out = await svc.apply_price_sync(
            self._session(),
            pricing_sync_service=self._sync_src(),
            aliases=["gpt-6.1-sol"],
            actor=MagicMock(),
            expected={
                "gpt-6.1-sol": PriceSyncExpected(
                    input_per_1k=Decimal("0.002"),
                    output_per_1k=Decimal("0.01"),
                    cache_5m_per_1k=Decimal("0.0025"),
                    cache_1h_per_1k=Decimal("0.004"),
                    cache_read_per_1k=Decimal("0.0002"),
                )
            },
        )
        assert out.applied == ["gpt-6.1-sol"]
        svc.set_pricing.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_long_context_tier_flows_to_set_pricing(self, monkeypatch):
        """카탈로그 장문 구간 단가가 PricingRequest 를 통해 set_pricing 에 전달."""
        from app.services.pricing_sync_service import NormalizedPrice

        np = NormalizedPrice(
            input_per_1k=Decimal("0.002"),
            output_per_1k=Decimal("0.01"),
            cache_5m_per_1k=Decimal("0.0025"),
            cache_1h_per_1k=Decimal("0.004"),
            cache_read_per_1k=Decimal("0.0002"),
            long_context_threshold_tokens=272000,
            long_input_per_1k=Decimal("0.004"),
            long_output_per_1k=Decimal("0.015"),
            long_cache_5m_per_1k=Decimal("0.005"),
            long_cache_1h_per_1k=Decimal("0.006"),
            long_cache_read_per_1k=Decimal("0.0004"),
        )
        m = self._model()
        repo = self._repo_with(m)
        monkeypatch.setattr(
            "app.services.model_service.ModelRepository", lambda sess: repo
        )
        svc = ModelService(MagicMock())
        captured = {}

        async def _capture(session, *, alias, data, actor, ip_address, request_id):
            captured["data"] = data

        svc.set_pricing = _capture
        from app.schemas.models import PriceSyncExpected

        out = await svc.apply_price_sync(
            self._session(),
            pricing_sync_service=self._sync_src(np),
            aliases=["gpt-6.1-sol"],
            actor=MagicMock(),
            expected={
                "gpt-6.1-sol": PriceSyncExpected(
                    input_per_1k=Decimal("0.002"),
                    output_per_1k=Decimal("0.01"),
                    cache_5m_per_1k=Decimal("0.0025"),
                    cache_1h_per_1k=Decimal("0.004"),
                    cache_read_per_1k=Decimal("0.0002"),
                    long_context_threshold_tokens=272000,
                    long_input_per_1k=Decimal("0.004"),
                    long_output_per_1k=Decimal("0.015"),
                    long_cache_5m_per_1k=Decimal("0.005"),
                    long_cache_1h_per_1k=Decimal("0.006"),
                    long_cache_read_per_1k=Decimal("0.0004"),
                )
            },
        )
        assert out.applied == ["gpt-6.1-sol"]
        req = captured["data"]
        assert req.long_context_threshold_tokens == 272000
        assert req.long_context_input_price_per_1k_tokens == Decimal("0.00400000")
        assert req.long_context_output_price_per_1k_tokens == Decimal("0.01500000")
        assert req.cache_derived is False
