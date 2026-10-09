# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.cache_invalidation import (
    CacheInvalidationManager,
    invalidate_after_commit,
)
from app.core.exceptions import ConflictError, NotFoundError
from app.models.model import ApiFormat, ModelAlias, ModelPricing, ModelStatus, Provider
from app.repositories.model_repository import ModelRepository
from app.schemas.models import (
    ModelCreateRequest,
    ModelDeleteResponse,
    ModelDeletionImpactResponse,
    ModelPricingResponse,
    ModelResponse,
    ModelUpdateRequest,
    PricingRequest,
    StatusPatchRequest,
)

logger = structlog.get_logger()


class ModelService:
    def __init__(self, cache_mgr: CacheInvalidationManager) -> None:
        self._cache_mgr = cache_mgr

    async def list_models(self, session: AsyncSession) -> list[ModelResponse]:
        repo = ModelRepository(session)
        models = await repo.list_all()
        result: list[ModelResponse] = []
        for m in models:
            pricing = await repo.get_current_pricing(m.alias)
            result.append(self._to_response(m, pricing))
        return result

    async def create_model(
        self,
        session: AsyncSession,
        *,
        data: ModelCreateRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> ModelResponse:
        repo = ModelRepository(session)

        # BR-MOD-01: Case-insensitive alias uniqueness
        if await repo.alias_exists_ci(data.alias):
            raise ConflictError(f"Model alias already exists: {data.alias}")

        model = ModelAlias(
            alias=data.alias,
            provider=Provider(data.provider.value),
            provider_model_id=data.provider_model_id,
            endpoint_url=data.endpoint_url,
            api_format=ApiFormat(data.api_format.value),
            status=ModelStatus.ACTIVE,
            description=data.description,
            display_name=data.display_name,
            context_window=data.context_window,
            max_output_tokens=data.max_output_tokens,
            # None = 제한 없음(하위호환 기본값), [] = 허용 앱 없음, 목록 = 그 앱만.
            allowed_clients=data.allowed_clients,
            created_by=actor.user_id,
        )
        await repo.create_model(model)

        # Initial pricing
        pricing = ModelPricing(
            id=uuid.uuid4(),
            model_alias=data.alias,
            input_price_per_1k_tokens=data.input_price_per_1k_tokens,
            output_price_per_1k_tokens=data.output_price_per_1k_tokens,
            cache_creation_5m_price_per_1k_tokens=data.cache_creation_5m_price_per_1k_tokens,
            cache_creation_1h_price_per_1k_tokens=data.cache_creation_1h_price_per_1k_tokens,
            cache_read_price_per_1k_tokens=data.cache_read_price_per_1k_tokens,
            effective_from=datetime.now(timezone.utc),
            created_by=actor.user_id,
        )
        await repo.create_pricing(pricing)

        # BR-MOD-04 / P0-④: invalidate-only (do NOT pre-seed model:{alias}).
        # Pre-seeding a flat, TTL-less cache entry here was the cache-poison
        # pattern: DEL both keys and let the gateway populate model:{alias} with
        # the correct nested shape + TTL on first cache-miss (router_service
        # self-heal). Keeps admin writes and gateway reads on one cache contract.
        await invalidate_after_commit(
            session, self._cache_mgr, [f"model:{model.alias}", "model:list"]
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="CREATE_MODEL",
            resource_type="ModelAlias",
            resource_id=model.alias,
            changes={"after": {"alias": model.alias, "provider": model.provider.value}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return self._to_response(model, pricing)

    async def update_model(
        self,
        session: AsyncSession,
        *,
        alias: str,
        data: ModelUpdateRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> ModelResponse:
        repo = ModelRepository(session)
        update_kwargs = {k: v for k, v in data.model_dump().items() if v is not None}
        model = await repo.update_model(alias, **update_kwargs)
        if model is None:
            raise NotFoundError("ModelAlias", alias)

        # ── allowed_clients 의 "명시적 null = 제한 해제" ──
        #
        # 위 필터(`if v is not None`)는 이 저장소의 관례다: null 은 "생략" 과 같이
        # 취급해 값을 유지한다. 그런데 allowed_clients 는 그 관례에서 **한 칸 더**
        # 필요하다. canonical 의미가 `None`=제한 없음 / `[]`=허용 앱 없음 이므로,
        # null 을 필터로 버리면 **한 번 목록이 박힌 모델을 "제한 없음" 으로 되돌릴
        # API 가 사라진다.** 그러면 콘솔은 그 목적으로 `[]` 를 보낼 수밖에 없고,
        # `[]` 는 전면 거부이므로 "제한 해제" 버튼이 그 모델을 통째로 막는다.
        #
        # pydantic 의 `model_fields_set` 이 "키를 안 보냄" 과 "null 을 보냄" 을
        # 구별해 주므로, **명시적 null 만** 해제로 취급한다.
        #
        # ⚠️ 나머지 nullable 필드 중 endpoint_url 만 "null = 무시" 를 유지한다 —
        #    endpoint 가 필요한 provider(Mantle/Runtime OpenAI/OPENMODEL)에서
        #    지워지면 어댑터가 호스트/서명 리전을 못 잡아 런타임에만 깨진다.
        #    description/display_name 은 아래 블록에서 명시적 null = 삭제로 처리한다.
        if "allowed_clients" in data.model_fields_set and data.allowed_clients is None:
            model.allowed_clients = None
            await session.flush()
            update_kwargs["allowed_clients"] = None

        # ── description / display_name 의 "명시적 null = 값 삭제" ──
        #
        # admin-ui 편집 폼은 두 필드를 기존값으로 미리 채워 보낸다 — 폼에서 지운
        # 경우만 null 이 오므로, 명시적 null 을 삭제 의도로 취급해도 안전하다
        # (생략과의 구별은 allowed_clients 와 같은 model_fields_set 규칙).
        # endpoint_url 은 제외 — endpoint 가 필요한 provider 에서 지우면
        # 런타임에만 깨지므로 "삭제" 의도 자체를 허용하지 않는 게 안전하다.
        _cleared = False
        # context_window/max_output_tokens 도 같은 규칙 — 명시적 null 은
        # "스펙 미상으로 되돌림" 으로 취급(편집 폼에서 필드를 비운 경우).
        for field in ("description", "display_name", "context_window", "max_output_tokens"):
            if field in data.model_fields_set and getattr(data, field) is None:
                setattr(model, field, None)
                update_kwargs[field] = None
                _cleared = True
        if _cleared:
            await session.flush()

        pricing = await repo.get_current_pricing(alias)

        # BR-MOD-04: Cache invalidation — 게이트웨이는 resolve 성공 시
        # model:{alias} 와 model:{provider_model_id} **두 키**를 쓴다(router_service).
        # pmid 키를 빠지면 pmid 경유 조회가 최대 300s 동안 옛 설정을 본다.
        await invalidate_after_commit(
            session,
            self._cache_mgr,
            [f"model:{alias}", f"model:{model.provider_model_id}", "model:list"],
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="UPDATE_MODEL",
            resource_type="ModelAlias",
            resource_id=alias,
            changes={"after": update_kwargs},
            ip_address=ip_address,
            request_id=request_id,
        )

        return self._to_response(model, pricing)

    async def set_pricing(
        self,
        session: AsyncSession,
        *,
        alias: str,
        data: PricingRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> ModelResponse:
        repo = ModelRepository(session)
        model = await repo.get_by_alias(alias)
        if model is None:
            raise NotFoundError("ModelAlias", alias)

        # BR-MOD-02: Close current pricing, preserve history
        await repo.close_current_pricing(alias, data.effective_from)

        pricing = ModelPricing(
            id=uuid.uuid4(),
            model_alias=alias,
            input_price_per_1k_tokens=data.input_price_per_1k_tokens,
            output_price_per_1k_tokens=data.output_price_per_1k_tokens,
            cache_creation_5m_price_per_1k_tokens=data.cache_creation_5m_price_per_1k_tokens,
            cache_creation_1h_price_per_1k_tokens=data.cache_creation_1h_price_per_1k_tokens,
            cache_read_price_per_1k_tokens=data.cache_read_price_per_1k_tokens,
            effective_from=data.effective_from,
            created_by=actor.user_id,
        )
        await repo.create_pricing(pricing)

        await invalidate_after_commit(
            session,
            self._cache_mgr,
            [f"model:{alias}", f"model:{model.provider_model_id}", "model:list"],
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_PRICING",
            resource_type="ModelPricing",
            resource_id=alias,
            changes={"after": {
                "input_price": str(data.input_price_per_1k_tokens),
                "output_price": str(data.output_price_per_1k_tokens),
                "cache_creation_price": str(data.cache_creation_5m_price_per_1k_tokens),
                "cache_creation_1h_price": str(data.cache_creation_1h_price_per_1k_tokens),
                "cache_read_price": str(data.cache_read_price_per_1k_tokens),
            }},
            ip_address=ip_address,
            request_id=request_id,
        )

        return self._to_response(model, pricing)

    async def preview_price_sync(
        self,
        session: AsyncSession,
        *,
        pricing_sync_service,
        quantize: Decimal = Decimal("0.000001"),
    ):
        """AWS Price List 단가 vs DB 현재가 diff 미리보기(쓰기 없음, deepdive 가격동기화).

        BEDROCK provider 모델만 대상. AWS 에서 못 찾으면 matched=False 로 표시(스킵 후보).
        """
        from app.schemas.models import (
            PriceSyncDiff,
            PriceSyncPreviewResponse,
        )

        repo = ModelRepository(session)
        models = await repo.list_all()
        fetched = await pricing_sync_service.fetch_bedrock_prices()

        diffs: list[PriceSyncDiff] = []
        matched = 0
        changed = 0
        for m in models:
            if m.provider != Provider.BEDROCK:
                continue  # OpenModel/vLLM 은 AWS 단가 없음
            cur = await repo.get_current_pricing(m.alias)
            cur_resp = self._to_response(m, cur).current_pricing
            np = fetched.lookup(m.provider_model_id)
            if np is None:
                diffs.append(PriceSyncDiff(
                    alias=m.alias,
                    provider_model_id=m.provider_model_id,
                    matched=False,
                    note="AWS Price List 에서 단가 미발견(모델ID 매칭 실패 또는 미게시)",
                    current=cur_resp,
                ))
                continue
            matched += 1
            p_in = np.input_per_1k.quantize(quantize)
            p_out = np.output_per_1k.quantize(quantize)
            p_5m = np.cache_5m_per_1k.quantize(quantize)
            p_1h = np.cache_1h_per_1k.quantize(quantize)
            p_rd = np.cache_read_per_1k.quantize(quantize)
            is_changed = cur is None or any([
                cur.input_price_per_1k_tokens != p_in,
                cur.output_price_per_1k_tokens != p_out,
                cur.cache_creation_5m_price_per_1k_tokens != p_5m,
                cur.cache_creation_1h_price_per_1k_tokens != p_1h,
                cur.cache_read_price_per_1k_tokens != p_rd,
            ])
            if is_changed:
                changed += 1
            # 스펙(context_window/max_output_tokens) 차이도 별도 플래그 —
            # 단가 동일해도 스펙만 새로 채워지는 경우가 있다.
            spec_changed = bool(
                (np.context_window and m.context_window != np.context_window)
                or (np.max_output_tokens and m.max_output_tokens != np.max_output_tokens)
            )
            note = "캐시 단가 일부 파생(AWS 미게시 → input 기반 추정)" if np.cache_derived else None
            if spec_changed:
                note = (note + " · " if note else "") + "스펙 갱신(context/max output)"
            diffs.append(PriceSyncDiff(
                alias=m.alias,
                provider_model_id=m.provider_model_id,
                matched=True,
                note=note,
                current=cur_resp,
                proposed_input_per_1k=p_in,
                proposed_output_per_1k=p_out,
                proposed_cache_5m_per_1k=p_5m,
                proposed_cache_1h_per_1k=p_1h,
                proposed_cache_read_per_1k=p_rd,
                changed=is_changed,
                spec_changed=spec_changed,
            ))

        return PriceSyncPreviewResponse(
            region=getattr(pricing_sync_service, "region", "us-east-1"),
            diffs=diffs,
            matched_count=matched,
            changed_count=changed,
        )

    async def apply_price_sync(
        self,
        session: AsyncSession,
        *,
        pricing_sync_service,
        aliases: list[str],
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
        quantize: Decimal = Decimal("0.000001"),
    ):
        """승인된 alias 목록만 AWS 단가로 적용 — 기존 set_pricing 재사용(시계열·감사·캐시).

        자동 전체적용 금지: 호출자가 preview 후 명시 선택한 aliases 만.
        """
        from app.schemas.models import PriceSyncApplyResponse, PricingRequest

        repo = ModelRepository(session)
        fetched = await pricing_sync_service.fetch_bedrock_prices()
        now = datetime.now(timezone.utc)

        applied: list[str] = []
        skipped: list[str] = []
        errors: list[str] = list(fetched.errors)

        for alias in aliases:
            model = await repo.get_by_alias(alias)
            if model is None:
                errors.append(f"{alias}: 모델 없음")
                continue
            if model.provider != Provider.BEDROCK:
                skipped.append(alias)
                continue
            np = fetched.lookup(model.provider_model_id)
            if np is None:
                skipped.append(alias)  # AWS 단가 미발견 → 적용 안 함
                continue
            req = PricingRequest(
                input_price_per_1k_tokens=np.input_per_1k.quantize(quantize),
                output_price_per_1k_tokens=np.output_per_1k.quantize(quantize),
                cache_creation_5m_price_per_1k_tokens=np.cache_5m_per_1k.quantize(quantize),
                cache_creation_1h_price_per_1k_tokens=np.cache_1h_per_1k.quantize(quantize),
                cache_read_price_per_1k_tokens=np.cache_read_per_1k.quantize(quantize),
                effective_from=now,
            )
            # 기존 set_pricing 재사용 → close_current_pricing + 새 행 + 캐시무효화 + SET_PRICING 감사
            await self.set_pricing(
                session, alias=alias, data=req, actor=actor,
                ip_address=ip_address, request_id=request_id,
            )
            # 카탈로그가 스펙을 주면 같이 채운다 — LiteLLM만 제공, AWS 소스는 None.
            # 모델 행은 캐시 키(model:{alias})를 공유하므로 변경 시 무효화 필요.
            spec_changed = False
            if np.context_window and model.context_window != np.context_window:
                model.context_window = np.context_window
                spec_changed = True
            if np.max_output_tokens and model.max_output_tokens != np.max_output_tokens:
                model.max_output_tokens = np.max_output_tokens
                spec_changed = True
            if spec_changed:
                await session.flush()
                await invalidate_after_commit(
                    session,
                    self._cache_mgr,
                    [f"model:{alias}", f"model:{model.provider_model_id}"],
                )
            applied.append(alias)

        return PriceSyncApplyResponse(applied=applied, skipped=skipped, errors=errors)

    async def patch_status(
        self,
        session: AsyncSession,
        *,
        alias: str,
        data: StatusPatchRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> ModelResponse:
        repo = ModelRepository(session)
        new_status = ModelStatus.ACTIVE if data.active else ModelStatus.INACTIVE
        model = await repo.patch_status(alias, new_status)
        if model is None:
            raise NotFoundError("ModelAlias", alias)

        pricing = await repo.get_current_pricing(alias)

        # BR-MOD-03/04: Immediate cache invalidation on INACTIVE — pmid 키도 함께
        # (kill switch 가 provider_model_id 조회 경로에는 최대 300s 지연됐다).
        await invalidate_after_commit(
            session,
            self._cache_mgr,
            [f"model:{alias}", f"model:{model.provider_model_id}", "model:list"],
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="PATCH_MODEL_STATUS",
            resource_type="ModelAlias",
            resource_id=alias,
            changes={"after": {"status": new_status.value}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return self._to_response(model, pricing)

    async def deletion_impact(
        self, session: AsyncSession, *, alias: str
    ) -> ModelDeletionImpactResponse:
        """삭제 확인 다이얼로그용 사전 영향 조회 — delete_model 과 같은 카운트를
        뽑아야 화면에 보인 숫자가 실제 삭제되는 숫자와 일치한다."""
        from sqlalchemy import func, select

        from app.models.budget import DowngradePolicy
        from app.models.model import RateLimitConfig, TeamAllowedModel, UserAllowedModel
        from app.models.usage import UsageLog

        repo = ModelRepository(session)
        if await repo.get_by_alias(alias) is None:
            raise NotFoundError("ModelAlias", alias)

        async def _count(stmt) -> int:
            return int((await session.execute(stmt)).scalar_one())

        impact = ModelDeletionImpactResponse(
            alias=alias,
            usage_logs=await _count(
                select(func.count()).select_from(UsageLog).where(UsageLog.model_alias == alias)
            ),
            pricings=await _count(
                select(func.count()).select_from(ModelPricing).where(ModelPricing.model_alias == alias)
            ),
            team_allowed=await _count(
                select(func.count()).select_from(TeamAllowedModel).where(TeamAllowedModel.model_alias == alias)
            ),
            user_allowed=await _count(
                select(func.count()).select_from(UserAllowedModel).where(UserAllowedModel.model_alias == alias)
            ),
            rate_limits=await _count(
                select(func.count()).select_from(RateLimitConfig).where(RateLimitConfig.model_alias == alias)
            ),
            downgrade_from=await _count(
                select(func.count()).select_from(DowngradePolicy).where(DowngradePolicy.from_model_alias == alias)
            ),
            downgrade_to=await _count(
                select(func.count()).select_from(DowngradePolicy).where(DowngradePolicy.to_model_alias == alias)
            ),
        )
        impact.blocked = impact.downgrade_to > 0
        return impact

    async def delete_model(
        self,
        session: AsyncSession,
        *,
        alias: str,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> ModelDeleteResponse:
        """모델 카탈로그 hard delete — deprecated 모델 정리용.

        규칙(합의된 삭제 정책):
          - usage_logs 는 건드리지 않는다 — 컬럼이 FK 없는 String 이라 이력과
            분석(raw alias fallback)은 그대로 남는다.
          - downgrade_policies.to_model_alias 참조는 409 — 이 모델이 다른 모델의
            살아있는 fallback 목적지라, 몰래 지우면 예산 초과 시 전환 대신 에러.
            operator 가 /budgets 에서 정책을 먼저 해제해야 한다.
          - 나머지 참조(pricing 이력·허용목록·rate limit·from 방향 정책)는 모델
            없이는 의미 없는 dead config — 같은 트랜잭션에서 같이 지운다.
        """
        from sqlalchemy import delete

        from app.models.budget import DowngradePolicy
        from app.models.model import RateLimitConfig, TeamAllowedModel, UserAllowedModel

        repo = ModelRepository(session)
        model = await repo.get_by_alias(alias)
        if model is None:
            raise NotFoundError("ModelAlias", alias)

        impact = await self.deletion_impact(session, alias=alias)
        if impact.blocked:
            raise ConflictError(
                f"모델 '{alias}' 은(는) 다운그레이드 정책 {impact.downgrade_to}건의 "
                "전환 목적지입니다 — 예산 관리에서 정책을 먼저 해제하세요."
            )

        # before 스냅샷 — 감사 로그가 삭제된 설정을 복원 추적할 수 있게.
        before = {
            "alias": model.alias,
            "provider": model.provider.value,
            "provider_model_id": model.provider_model_id,
            "status": model.status.value,
            "display_name": model.display_name,
        }

        # 자식 참조 → 모델 순으로 한 트랜잭션에서 삭제 (커밋은 CommittingRoute).
        await session.execute(delete(ModelPricing).where(ModelPricing.model_alias == alias))
        await session.execute(delete(TeamAllowedModel).where(TeamAllowedModel.model_alias == alias))
        await session.execute(delete(UserAllowedModel).where(UserAllowedModel.model_alias == alias))
        await session.execute(delete(RateLimitConfig).where(RateLimitConfig.model_alias == alias))
        await session.execute(
            delete(DowngradePolicy).where(DowngradePolicy.from_model_alias == alias)
        )
        # ORM delete(session.delete) 는 쓰지 않는다 — pricings 는 lazy=selectin 이라
        # 부모 삭제 시 자식 FK 를 NULL 로 밀려 하고(NOT NULL 위반), 위의 bulk delete 가
        # 세션을 우회해 identity map 과 어긋난다. 같은 Core delete 로 통일.
        await session.execute(delete(ModelAlias).where(ModelAlias.alias == alias))
        await session.flush()

        # BR-MOD-04 와 같은 규칙 — DEL 만 하고 재적재는 게이트웨이의 cache-miss 에 맡긴다.
        # pmid 키도 지운다 — resolve 가 model:{provider_model_id} 도 쓰므로, 이걸
        # 놔두면 삭제된 모델이 pmid 조회로 최대 300s 더 살아난다.
        await invalidate_after_commit(
            session,
            self._cache_mgr,
            [f"model:{alias}", f"model:{model.provider_model_id}", "model:list"],
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="DELETE_MODEL",
            resource_type="ModelAlias",
            resource_id=alias,
            changes={
                "before": before,
                "deleted_children": {
                    "pricings": impact.pricings,
                    "team_allowed": impact.team_allowed,
                    "user_allowed": impact.user_allowed,
                    "rate_limits": impact.rate_limits,
                    "downgrade_from": impact.downgrade_from,
                },
                # 사용 이력은 남는다는 것을 감사에 명시 — 지워진 줄 아는 오해 방지.
                "usage_logs_retained": impact.usage_logs,
            },
            ip_address=ip_address,
            request_id=request_id,
        )

        return ModelDeleteResponse(alias=alias, deleted=impact)

    @staticmethod
    def _to_response(model: ModelAlias, pricing: ModelPricing | None) -> ModelResponse:
        pricing_resp = None
        if pricing:
            pricing_resp = ModelPricingResponse(
                input_price_per_1k_tokens=pricing.input_price_per_1k_tokens,
                output_price_per_1k_tokens=pricing.output_price_per_1k_tokens,
                cache_creation_5m_price_per_1k_tokens=pricing.cache_creation_5m_price_per_1k_tokens,
                cache_creation_1h_price_per_1k_tokens=pricing.cache_creation_1h_price_per_1k_tokens,
                cache_read_price_per_1k_tokens=pricing.cache_read_price_per_1k_tokens,
                effective_from=pricing.effective_from,
                effective_until=pricing.effective_until,
            )
        return ModelResponse(
            alias=model.alias,
            provider=model.provider,
            provider_model_id=model.provider_model_id,
            endpoint_url=model.endpoint_url,
            api_format=model.api_format,
            status=model.status.value,
            # 3-상태를 그대로 노출한다 — [] 를 None 으로 뭉개면 운영자가 자기가 만든
            # 전면 거부를 화면에서 볼 수 없다.
            allowed_clients=model.allowed_clients,
            description=model.description,
            # display_name 은 표시 전용 — 비어 있으면 alias 로 대체해 모든 API
            # 소비자(목록·피커·정책 표시)가 같은 이름을 보게 한다. DB 는 NULL 유지.
            display_name=model.display_name or model.alias,
            context_window=model.context_window,
            max_output_tokens=model.max_output_tokens,
            current_pricing=pricing_resp,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )
