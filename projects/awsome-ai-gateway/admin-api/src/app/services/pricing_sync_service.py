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
import re
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
    #: 장문 컨텍스트(>threshold) 구간 단가 — LiteLLM `*_cost_per_token_above_{N}k`
    #: 계열. 임계값은 모델별로 다르다(GPT-5.6=272K, Haiku 5.5=100K).
    #: sync 가 이 값을 안 채우면 기존 set_pricing 의 "이전 행 이어받기"가 절대값을
    #: 그대로 남겨, 기본 단가 개정 때 구간 단가만 부패한다(과소청구 ~2×).
    long_context_threshold_tokens: int | None = None
    long_input_per_1k: Decimal | None = None
    long_output_per_1k: Decimal | None = None
    long_cache_5m_per_1k: Decimal | None = None
    long_cache_1h_per_1k: Decimal | None = None
    long_cache_read_per_1k: Decimal | None = None
    #: 구간 캐시 단가 중 파생(추정) 포함 여부 — 1h 구간은 카탈로그에 필드가 없어
    #: 구간이 존재하면 항상 True. 감사 로그 출처 표시용.
    long_cache_derived: bool = False


@dataclass
class FetchResult:
    prices: dict[str, NormalizedPrice] = field(default_factory=dict)  # normalized model_id → 단가
    errors: list[str] = field(default_factory=list)

    def lookup(self, provider_model_id: str) -> NormalizedPrice | None:
        """DB provider_model_id 와 source model_id 간 매칭(best-effort).

        region prefix(global./us./eu./apac./jp./us-gov.)는 같은 모델이라도
        **리전별 단가가 다른 별도 상품**이다 — Global CRIS 는 In-Region 대비
        약 9% 싸고, geo(us./eu./apac./jp.)와 plain(무접두)은 In-Region 단가로
        동일하다. 따라서 **global ↔ 비-global 경계는 절대 건너지 않는다**:
        건너면 조용히 ~9-10% 잘못된 단가로 과금된다.
        매칭 순서: exact → (geo/plain 한정) <base> 폴백.
        """
        if not provider_model_id:
            return None
        mid = provider_model_id.lower().strip()
        if not mid:
            return None
        if mid in self.prices:
            return self.prices[mid]
        base = _strip_region_prefix(mid)
        if mid.startswith("global.") or base == mid:
            # global.* 행이 카탈로그에 없으면 plain(=in-region 가격)으로
            # 채우면 ~10% 과다 청구; plain 행도 geo/global 추측 없이 unmatched
            # 로 남겨 사람이 판단하게 한다.
            return None
        # geo prefix(us./eu./apac./jp./us-gov.)만 plain in-region 단가로 폴백.
        # 스택드 프리픽스("us.global.X")는 base 도 prefix 라 매칭하지 않는다.
        if _strip_region_prefix(base) != base:
            return None
        if base in self.prices:
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

            # 장문(>threshold) 구간 — 카탈로그가 `*_cost_per_token_above_{N}k_tokens`
            # 계열 필드를 줄 때만. 임계값은 모델마다 다르다(GPT-5.6=272K,
            # Haiku 5.5=100K — kiro R18: 272K 하드코딩은 100K 티어를 못 잡는다).
            # 필드명의 N 에서 threshold 를 얻는다 — 차원별로 같은 N 을 쓴다는
            # LiteLLM 규약을 따르되, 한 차원에 N 이 여럿이면 가장 작은(가장 먼저
            # 구간이 시작하는) 것을 고르고, 차원 간 N 이 갈리면 서로 다른 티어의
            # 단가를 한 임계에 섞게 돼 티어 전체를 적용하지 않는다.
            # ultrafast 프리미엄 변종은 제외(별도 상품).
            def _above(field_prefix: str) -> tuple[Decimal | None, int | None]:
                matches = [
                    (int(mm.group(1)) * 1000, val)
                    for key, val in m.items()
                    if (mm := re.fullmatch(
                        rf"{re.escape(field_prefix)}_above_(\d+)k_tokens", key
                    )) and val is not None
                ]
                if not matches:
                    return None, None
                n, val = min(matches, key=lambda t: t[0])
                return self._to_decimal(val), n

            long_in, thr_in = _above("input_cost_per_token")
            long_out, thr_out = _above("output_cost_per_token")
            long_5m, thr_5m = _above("cache_creation_input_token_cost")
            long_read, thr_read = _above("cache_read_input_token_cost")
            thresholds = [t for t in (thr_in, thr_out, thr_5m, thr_read) if t is not None]
            tier_threshold = None
            if thresholds:
                if len(set(thresholds)) == 1:
                    tier_threshold = thresholds[0]
                else:
                    # 차원별 임계가 갈리는 카탈로그 — 임의로 min() 하면 다른
                    # 구간의 단가를 한 임계에 섞어 과대/과소청구. 티어를 버리고
                    # 오류로 남겨 사람이 확인하게 한다.
                    result.errors.append(
                        f"{key}: conflicting above_*k thresholds "
                        f"{sorted(set(thresholds))} — long-context tier skipped"
                    )
                    long_in = long_out = long_5m = long_read = None
            has_long = (
                tier_threshold is not None
                and (long_in is not None or long_out is not None)
            )
            long_in_eff = long_out_eff = long_5m_eff = long_1h_eff = long_read_eff = None
            if has_long:
                # 구간 값은 하나의 단위 — 일부만 채우면 threshold+NULL 조합(구간
                # 미적용 → 기본 단가로 과소청구)이 생기므로 결손은 채운다.
                # 0 가격도 유효 값이라 `is not None` 으로만 판정.
                # 파생은 모델 자체의 발행 비율 스케일(long_in/input × base_x)을
                # 우선한다 — Claude 계열 고정 배수를 GPT 계열(read≈5%)에 그대로
                # 쓰면 과대추정된다. input==0 이면 비율을 못 구해 표준 배수 폴백.
                long_in_eff = long_in if long_in is not None else input_cost
                long_out_eff = long_out if long_out is not None else output_cost
                scale = long_in_eff / input_cost if input_cost != 0 else None
                base_5m_eff = cache_5m if cache_5m is not None else input_cost * _CACHE_5M_MULT
                base_1h_eff = cache_1h if cache_1h is not None else input_cost * _CACHE_1H_MULT
                base_read_eff = cache_read if cache_read is not None else input_cost * _CACHE_READ_MULT
                long_5m_eff = long_5m if long_5m is not None else (
                    base_5m_eff * scale if scale is not None else long_in_eff * _CACHE_5M_MULT
                )
                long_read_eff = long_read if long_read is not None else (
                    base_read_eff * scale if scale is not None else long_in_eff * _CACHE_READ_MULT
                )
                # 구간별 1h 필드는 카탈로그에 없다 — 발행 1h 기본가를 같은 비율로
                # 스케일. scale 은 input==0 일 때만 None(그땐 배수 폴백).
                long_1h_eff = (
                    base_1h_eff * scale if scale is not None else long_in_eff * _CACHE_1H_MULT
                )

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
                long_context_threshold_tokens=tier_threshold if has_long else None,
                long_input_per_1k=(long_in_eff * Decimal(1000) if long_in_eff is not None else None),
                long_output_per_1k=(long_out_eff * Decimal(1000) if long_out_eff is not None else None),
                long_cache_5m_per_1k=(long_5m_eff * Decimal(1000) if long_5m_eff is not None else None),
                long_cache_1h_per_1k=(long_1h_eff * Decimal(1000) if long_1h_eff is not None else None),
                long_cache_read_per_1k=(long_read_eff * Decimal(1000) if long_read_eff is not None else None),
                # 구간 캐시는 출처 추적 대상 — 1h 구간 필드는 카탈로그에 없어
                # 항상 파생(5m/read 도 소스 생략 시 파생)이라 구간 존재 = 파생 포함.
                long_cache_derived=has_long,
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
