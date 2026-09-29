# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""모델 단가 동기화 서비스.

목적: 신모델/가격개정 시 사람이 단가를 수동 입력(휴먼에러로 청구 오류)하던 것을, 외부
공식 단가 소스(AWS Price List API 또는 LiteLLM Model Catalog API)에서 가져와 diff 로
보여주고 **승인 후에만** 기존 set_pricing 경로로 커밋한다.

설계 원칙(안전):
- **소스는 AWS Price List API / LiteLLM Model Catalog API** — AgentCore Gateway/Inference Targets 아님(가격 미노출).
- **자동 적용 금지** — preview(읽기·diff) → 사람 승인 → apply(쓰기) 2단계.
- apply 는 기존 ModelService.set_pricing 재사용 → 시계열(effective_from/until) 보존 +
  Redis 캐시 무효화 + SET_PRICING 감사 로그가 공짜로 따라옴.
- **BEDROCK provider 모델만 대상**(OpenModel/vLLM 은 AWS 단가 없음).
- 매칭은 best-effort(외부 source ID ↔ 우리 provider_model_id) — 불확실성은 사람 검토가 흡수.

AWS Price List API 는 us-east-1/ap-south-1/eu-central-1 엔드포인트만 지원(리전 전용).
단위는 SKU 의 'unit'(예: '1K tokens'/'1M tokens') 문자열을 읽어 per-1k 로 정규화한다.
LiteLLM API 는 per-token 단가를 제공하므로 per-1k 로 변환(×1000)한다.
캐시 단가가 source 에 안 나오면 input 기반 파생(5m=×1.25, 1h=×2.0, read=×0.1, seed 산식과 동일).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

import structlog

logger = structlog.get_logger()

# 캐시 단가 파생 배수(03_seed_data.sql 주석과 동일 산식). Price List 가 캐시 SKU 를
# 직접 안 줄 때만 input 단가에서 파생.
_CACHE_5M_MULT = Decimal("1.25")
_CACHE_1H_MULT = Decimal("2.0")
_CACHE_READ_MULT = Decimal("0.1")


@dataclass
class NormalizedPrice:
    """Price List 에서 추출·정규화한 per-1k 단가(USD)."""

    input_per_1k: Decimal
    output_per_1k: Decimal
    cache_5m_per_1k: Decimal
    cache_1h_per_1k: Decimal
    cache_read_per_1k: Decimal
    cache_derived: bool = False  # 캐시 단가가 파생(추정)인지
    #: 스펙 정보 — LiteLLM 카탈로그만 제공(AWS Price List 에는 없어 None 유지).
    context_window: int | None = None
    max_output_tokens: int | None = None


@dataclass
class FetchResult:
    prices: dict[str, NormalizedPrice] = field(default_factory=dict)  # normalized model_id → 단가
    errors: list[str] = field(default_factory=list)

    def lookup(self, provider_model_id: str) -> NormalizedPrice | None:
        """DB provider_model_id 와 source model_id 간 매칭(best-effort).

        region prefix(global./us./eu./apac./jp./us-gov.)는 같은 모델이라도
        **리전별 단가가 다른 별도 상품**이므로 붕괴시키지 않는다.
        매칭 순서: exact → global.<base> → <base>(plain). 다른 리전
        (us.X 가 eu.X 단가를 받는 식의) 교차 fallback 은 하지 않는다.
        """
        if not provider_model_id:
            return None
        mid = provider_model_id.lower().strip()
        if not mid:
            return None
        if mid in self.prices:
            return self.prices[mid]
        base = _strip_region_prefix(mid)
        global_key = f"global.{base}"
        if global_key in self.prices:
            return self.prices[global_key]
        if base != mid and base in self.prices:
            return self.prices[base]
        return None


class PricingSyncSource(Protocol):
    """단가 동기화 소스 추상화."""

    async def fetch_bedrock_prices(self) -> FetchResult: ...


# Bedrock cross-region inference profile prefix. 같은 모델이라도 prefix 별로
# 리전 단가가 다른 별도 상품이므로(예: us. 는 global. 대비 ~1.1×) prices 키는
# full id 를 유지하고, base 추출 시에만 prefix 를 뗀다.
_KNOWN_REGION_PREFIXES = {"global", "us", "eu", "apac", "jp", "us-gov"}


def _strip_region_prefix(model_id: str) -> str:
    parts = model_id.split(".", 1)
    if len(parts) == 2 and parts[0] in _KNOWN_REGION_PREFIXES:
        return parts[1]
    return model_id


class AwsPricingSyncService:
    """AWS Price List API 단가 조회·정규화. boto3 pricing client 주입(테스트 격리)."""

    def __init__(self, pricing_client, *, service_code: str = "AmazonBedrock") -> None:
        self._client = pricing_client
        self._service_code = service_code

    async def fetch_bedrock_prices(self) -> FetchResult:
        """GetProducts(serviceCode=AmazonBedrock) 페이지네이션 → model_id 별 정규화 단가.

        blocking boto3 호출을 asyncio.to_thread 로 감싼다(Cognito sync 패턴).
        """
        try:
            raw = await asyncio.to_thread(self._get_all_products)
        except Exception as e:  # noqa: BLE001 — 외부 API 실패는 결과로 보고(fail-soft)
            logger.warning("price_list_fetch_failed", error=str(e))
            return FetchResult(errors=[f"AWS Price List API 호출 실패: {e}"])
        return self._normalize_products(raw)

    def _get_all_products(self) -> list[dict]:
        """pricing.get_products 페이지네이션(동기). PriceList = JSON 문자열 배열."""
        out: list[dict] = []
        token: str | None = None
        while True:
            kwargs: dict = {"ServiceCode": self._service_code, "MaxResults": 100}
            if token:
                kwargs["NextToken"] = token
            resp = self._client.get_products(**kwargs)
            for item in resp.get("PriceList", []):
                out.append(json.loads(item) if isinstance(item, str) else item)
            token = resp.get("NextToken")
            if not token:
                break
        return out

    def _normalize_products(self, products: list[dict]) -> FetchResult:
        """product/terms 를 파싱해 model_id → NormalizedPrice 로 정규화.

        Price List 구조는 모델/리전/usagetype 마다 별 product 라, 같은 모델의 input/output
        가 다른 product 로 나뉜다. model_id 키로 묶어 input/output 을 합산 매핑한다.
        """
        result = FetchResult()
        # model_id(lower) → {"input": Decimal, "output": Decimal, "cache_*": Decimal}
        acc: dict[str, dict[str, Decimal]] = {}

        for p in products:
            attrs = (p.get("product", {}) or {}).get("attributes", {}) or {}
            model_id = self._extract_model_id(attrs)
            if not model_id:
                continue
            unit_price, per_unit_tokens = self._extract_price(p)
            if unit_price is None or per_unit_tokens is None:
                continue
            per_1k = (unit_price / Decimal(per_unit_tokens)) * Decimal(1000)
            kind = self._classify_usage(attrs)
            if kind is None:
                continue
            acc.setdefault(model_id.lower(), {})[kind] = per_1k

        for mid, d in acc.items():
            inp = d.get("input")
            out = d.get("output")
            if inp is None or out is None:
                # input/output 둘 다 없으면 비용계산 불가 → 스킵(부분 데이터)
                continue
            cache_5m = d.get("cache_write_5m")
            cache_1h = d.get("cache_write_1h")
            cache_read = d.get("cache_read")
            derived = cache_5m is None or cache_1h is None or cache_read is None
            result.prices[mid] = NormalizedPrice(
                input_per_1k=inp,
                output_per_1k=out,
                cache_5m_per_1k=cache_5m if cache_5m is not None else inp * _CACHE_5M_MULT,
                cache_1h_per_1k=cache_1h if cache_1h is not None else inp * _CACHE_1H_MULT,
                cache_read_per_1k=cache_read if cache_read is not None else inp * _CACHE_READ_MULT,
                cache_derived=derived,
            )
        return result

    # ── 파싱 헬퍼(Price List 스키마 변동에 견고하게, best-effort) ──

    @staticmethod
    def _extract_model_id(attrs: dict) -> str | None:
        """product attributes 에서 모델 식별자 추출. AWS 가 쓰는 키가 버전마다 다를 수
        있어 후보를 순서대로 시도(best-effort)."""
        for key in ("model", "modelId", "titanModelId", "inferenceProfile", "usagetype"):
            v = attrs.get(key)
            if v:
                return str(v)
        return None

    @staticmethod
    def _extract_price(product: dict) -> tuple[Decimal | None, int | None]:
        """terms.OnDemand...priceDimensions.pricePerUnit.USD + unit 문자열 파싱.

        unit 예: '1K tokens' → 1000, '1M tokens'/'1000000 tokens' → 1,000,000.
        반환: (USD 단가, 단가 기준 토큰수). 못 읽으면 (None, None)."""
        terms = (product.get("terms", {}) or {}).get("OnDemand", {}) or {}
        for term in terms.values():
            dims = (term.get("priceDimensions", {}) or {})
            for dim in dims.values():
                usd = ((dim.get("pricePerUnit", {}) or {}).get("USD"))
                unit = (dim.get("unit") or dim.get("description") or "").lower()
                if usd is None:
                    continue
                try:
                    price = Decimal(str(usd))
                except Exception:  # noqa: BLE001
                    continue
                per = 1
                if "1m" in unit or "million" in unit or "1000000" in unit:
                    per = 1_000_000
                elif "1k" in unit or "1000" in unit or "thousand" in unit:
                    per = 1000
                else:
                    per = 1  # per-token (드뭄) — per_1k 환산 시 ×1000
                return price, per
        return None, None

    @staticmethod
    def _classify_usage(attrs: dict) -> str | None:
        """usagetype/operation 으로 input/output/cache 종류 분류(best-effort)."""
        blob = " ".join(
            str(attrs.get(k, "")) for k in ("usagetype", "operation", "feature", "description")
        ).lower()
        if not blob:
            return None
        if "cache" in blob and "read" in blob:
            return "cache_read"
        if "cache" in blob and ("1h" in blob or "hour" in blob):
            return "cache_write_1h"
        if "cache" in blob and ("write" in blob or "5m" in blob or "creat" in blob):
            return "cache_write_5m"
        if "output" in blob or "outputtoken" in blob:
            return "output"
        if "input" in blob or "inputtoken" in blob:
            return "input"
        return None


class LambdaCatalogFetcher:
    """Lambda invoke 로 LiteLLM 카탈로그 원본 모델 목록을 받아오는 fetcher.

    llm-gateway-<env>-litellm-pricing Lambda(배포: update-scripts/NDS-01)가
    api.litellm.ai/model_catalog 를 페이지네이션 조회해 {"data": [...]} 를
    반환한다. admin-api 는 lambda:InvokeFunction 만 필요하고 카탈로그
    정규화는 LiteLLMPricingSyncService 에 그대로 둔다.
    """

    def __init__(self, lambda_client, *, function_name: str) -> None:
        self._lambda = lambda_client
        self._function_name = function_name

    async def __call__(self) -> list[dict]:
        resp = await asyncio.to_thread(
            self._lambda.invoke,
            FunctionName=self._function_name,
            InvocationType="RequestResponse",
            Payload=b"{}",
        )
        if resp.get("FunctionError"):
            detail = resp["Payload"].read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Lambda {self._function_name} error: {detail}")
        payload = json.loads(resp["Payload"].read())
        if errors := payload.get("errors"):
            raise RuntimeError("; ".join(str(e) for e in errors))
        return payload.get("data", [])


class LiteLLMPricingSyncService:
    """LiteLLM Model Catalog 단가 정규화.

    LiteLLM 은 per-token 단가를 반환하므로 per-1k USD 로 변환(×1000)한다.
    카탈로그 조회는 fetcher 에 전적으로 위임된다 — 운영에서는
    LambdaCatalogFetcher(LITELLM_PRICING_LAMBDA, NDS-01)를 주입하며,
    admin-api 가 외부 인터넷을 직접 호출하는 경로는 없다.
    """

    def __init__(self, *, fetcher: Callable[[], Awaitable[list[dict]]]) -> None:
        self._fetcher = fetcher

    async def fetch_bedrock_prices(self) -> FetchResult:
        """fetcher 가 돌려준 카탈로그 row → model_id 별 per-1k 정규화 단가."""
        try:
            raw = await self._fetcher()
            return self._normalize_models(raw)
        except Exception as e:  # noqa: BLE001 — 외부 조회 실패는 결과로 보고(fail-soft)
            logger.warning("litellm_fetch_failed", error=str(e))
            return FetchResult(errors=[f"LiteLLM API 호출 실패: {e}"])

    def _normalize_models(self, models: list[dict]) -> FetchResult:
        result = FetchResult()
        for m in models:
            model_id = str(m.get("id") or "").strip()
            if not model_id:
                continue
            key = model_id.lower()
            if key in result.prices:
                continue  # 동일 id 중복만 무시; region variant(us./global./eu. 등)는 별도 상품으로 유지

            input_cost = self._to_decimal(m.get("input_cost_per_token"))
            output_cost = self._to_decimal(m.get("output_cost_per_token"))
            if input_cost is None or output_cost is None:
                continue

            cache_read = self._to_decimal(m.get("cache_read_input_token_cost"))
            cache_5m = self._to_decimal(m.get("cache_creation_input_token_cost"))
            cache_1h = self._to_decimal(m.get("cache_creation_input_token_cost_above_1hr"))
            derived = cache_read is None or cache_5m is None or cache_1h is None

            result.prices[key] = NormalizedPrice(
                input_per_1k=input_cost * Decimal(1000),
                output_per_1k=output_cost * Decimal(1000),
                cache_5m_per_1k=(cache_5m if cache_5m is not None else input_cost * _CACHE_5M_MULT) * Decimal(1000),
                cache_1h_per_1k=(cache_1h if cache_1h is not None else input_cost * _CACHE_1H_MULT) * Decimal(1000),
                cache_read_per_1k=(cache_read if cache_read is not None else input_cost * _CACHE_READ_MULT) * Decimal(1000),
                cache_derived=derived,
                context_window=self._to_int(m.get("max_input_tokens"))
                or self._to_int(m.get("max_tokens")),
                max_output_tokens=self._to_int(m.get("max_output_tokens")),
            )
        return result

    @staticmethod
    def _to_decimal(value) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _to_int(value) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


# Legacy alias: 기존 코드/테스트가 PricingSyncService 이름을 그대로 사용.
PricingSyncService = AwsPricingSyncService
