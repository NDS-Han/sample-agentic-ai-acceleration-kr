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
            repo.get_current_pricing = AsyncMock(return_value=pricing)
            mock_audit.log = AsyncMock()

            await model_service.update_model(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        assert model.description == "keep me"
        assert model.display_name == "Keep"


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
            repo.close_current_pricing = AsyncMock()
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()

            await model_service.set_pricing(
                mock_session, alias="llama-3-70b", data=data, actor=admin_user
            )

        created = repo.create_pricing.call_args.args[0]
        assert created.cache_creation_5m_price_per_1k_tokens == Decimal("0")
        assert created.cache_read_price_per_1k_tokens == Decimal("0")


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
