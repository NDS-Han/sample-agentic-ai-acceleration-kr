# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.config import get_settings
from app.core.db import get_db_session
from app.schemas.models import (
    ModelCreateRequest,
    ModelListResponse,
    ModelResponse,
    ModelUpdateRequest,
    PriceSyncApplyRequest,
    PriceSyncApplyResponse,
    PriceSyncPreviewResponse,
    PriceSyncSourcesResponse,
    PricingRequest,
    StatusPatchRequest,
)

router = APIRouter(prefix="/admin/models", tags=["Model Management"])


def _build_pricing_sync_service(*, source: str = "aws"):
    """단가 동기화 서비스 생성.

    가격 동기화 소스는 AWS Price List API / LiteLLM Model Catalog API 이며
    AgentCore Gateway/Inference Targets 아님(IT 는 단가를 노출하지 않음).
    region 은 Price List 전용 엔드포인트(us-east-1 등).

    source:
      - "aws": AWS Price List API(boto3 pricing client)
      - "litellm": LiteLLM Model Catalog — Lambda 프록시 경유(NDS-01)
    """
    settings = get_settings()
    if source == "litellm":
        # LiteLLM 카탈로그 조회는 Lambda 프록시만 허용 — admin-api 가 외부 인터넷을
        # 직접 호출하지 않는다. 미배포 시 503 으로 명시 (조용한 폴백 금지).
        if not settings.LITELLM_PRICING_LAMBDA:
            raise HTTPException(
                status_code=503,
                detail="LiteLLM pricing source is not configured. "
                "Deploy the catalog proxy with update-scripts/NDS-01-deploy-litellm-pricing-lambda.sh",
            )
        import boto3

        from app.services.pricing_sync_service import (
            LambdaCatalogFetcher,
            LiteLLMPricingSyncService,
        )

        # Lambda 는 파드와 같은 리전에 배포 — boto3 기본 리전 해석(AWS_REGION) 사용.
        fetcher = LambdaCatalogFetcher(
            boto3.client("lambda"),
            function_name=settings.LITELLM_PRICING_LAMBDA,
        )
        svc = LiteLLMPricingSyncService(fetcher=fetcher)
        svc.region = f"lambda:{settings.LITELLM_PRICING_LAMBDA}"  # preview 응답에 소스 표시용
        return svc

    import boto3

    from app.services.pricing_sync_service import PricingSyncService

    region = settings.PRICING_API_REGION
    client = boto3.client("pricing", region_name=region)
    svc = PricingSyncService(client)
    svc.region = region  # preview 응답에 표시
    return svc


@router.get("", response_model=ModelListResponse)
async def list_models(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.model_service import ModelService

    svc: ModelService = request.app.state.model_service
    items = await svc.list_models(session)
    return ModelListResponse(items=items)


@router.post("", response_model=ModelResponse, status_code=201)
async def create_model(
    request: Request,
    body: ModelCreateRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: ModelService = request.app.state.model_service
    return await svc.create_model(
        session,
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.put("/{alias}", response_model=ModelResponse)
async def update_model(
    request: Request,
    alias: str,
    body: ModelUpdateRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: ModelService = request.app.state.model_service
    return await svc.update_model(
        session,
        alias=alias,
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.put("/{alias}/pricing", response_model=ModelResponse)
async def set_pricing(
    request: Request,
    alias: str,
    body: PricingRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: ModelService = request.app.state.model_service
    return await svc.set_pricing(
        session,
        alias=alias,
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.get("/pricing/sources", response_model=PriceSyncSourcesResponse)
async def price_sync_sources(
    admin: CurrentUser = Depends(require_admin),
):
    """사용 가능한 단가 소스 — UI 가 litellm 옵션을 Lambda 미배포 시 비활성화하는 근거."""
    settings = get_settings()
    return PriceSyncSourcesResponse(
        sources={
            "aws": True,
            "litellm": bool(settings.LITELLM_PRICING_LAMBDA),
        }
    )


@router.get("/pricing/sync-preview", response_model=PriceSyncPreviewResponse)
async def price_sync_preview(
    request: Request,
    source: str = "aws",
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """외부 단가 소스(AWS Price List / LiteLLM Catalog) vs DB 현재가 diff 미리보기.

    source: "aws" | "litellm". 기본값 "aws".
    운영자가 이 diff 를 확인한 뒤 sync-apply 로 명시 적용. 자동 적용 없음.
    """
    from app.services.model_service import ModelService

    svc: ModelService = request.app.state.model_service
    pricing_sync = _build_pricing_sync_service(source=source)
    return await svc.preview_price_sync(session, pricing_sync_service=pricing_sync)


@router.post("/pricing/sync-apply", response_model=PriceSyncApplyResponse)
async def price_sync_apply(
    request: Request,
    body: PriceSyncApplyRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """승인된 alias 목록만 외부 단가 소스(AWS / LiteLLM)로 적용(기존 set_pricing 재사용)."""
    from app.services.model_service import ModelService

    svc: ModelService = request.app.state.model_service
    pricing_sync = _build_pricing_sync_service(source=body.source)
    return await svc.apply_price_sync(
        session,
        pricing_sync_service=pricing_sync,
        aliases=body.aliases,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.patch("/{alias}/status", response_model=ModelResponse)
async def patch_status(
    request: Request,
    alias: str,
    body: StatusPatchRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: ModelService = request.app.state.model_service
    return await svc.patch_status(
        session,
        alias=alias,
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
