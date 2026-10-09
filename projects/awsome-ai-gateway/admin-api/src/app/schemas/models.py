# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import ipaddress
import socket
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.clients import validate_clients
from app.schemas.common import ApiFormatEnum, ProviderEnum

# 단가 상한 — DB 컬럼에서 유도한 값이지 임의로 고른 숫자가 아니다.
#   app/models/model.py:100~109  → Numeric(10, 6)
#   db/init/02_create_tables.sql:222~226 → NUMERIC(10,6)  (0003_rename_cache_5m.py:34~38 도 동일)
# NUMERIC(10,6) = 전체 10자리 중 소수 6자리 ⇒ 정수부는 4자리뿐이므로 최대값이 9999.999999 다.
# 상한이 없으면 pydantic 은 통과시키고 asyncpg 가 INSERT 시점에 NumericValueOutOfRange 를
# 던져 그냥 500 이 된다(입력 오류인데 서버 장애처럼 보이고, 어느 필드가 문제인지도 안 나온다).
# ⚠️ 기존 decimal_places=6 **만으로는** 정수부를 전혀 제한하지 못한다(소수 자리 수만 본다).
#    max_digits=10 을 더하는 방법도 있다 — pydantic 2.13.2 실측으로는 max_digits-decimal_places
#    를 정수부 상한(4자리)으로 환산해 같은 결과를 낸다(decimal_whole_digits 에러). 그래도 여기서는
#    le 를 쓴다: pyproject 가 pydantic>=2.0.0 만 요구하므로 그 파생 규칙에 기대지 않고
#    DB 최대값을 그대로 적는 편이 버전에 무관하고 에러 메시지도 사람이 읽을 수 있다.
MAX_PRICE_PER_1K = Decimal("9999.999999")


# ── endpoint_url 검증 ──
#
# endpoint_url 은 gateway 어댑터가 그대로 요청 URL로 쓰는 저장형 값이다
# (mantle_adapter `POST {endpoint}/v1/messages` 등). 검증 없이 저장하면
# ADMIN 이 메타데이터 엔드포인트(169.254.169.254 등)나 게이트웨이 loopback 을
# 등록해, 게이트웨이가 사용자 요청 본문을 그쪽으로 POST 하는 SSRF 경로가 열린다.
#
# 허용/차단 기준:
#   * 스킴은 http/https 만 (file://, gopher:// 등 차단)
#   * userinfo(`http://u:p@h`)·fragment 금지 — 자격증명 내장/파서 혼동 방지
#   * 호스트 필수. link-local(169.254.0.0/16 메타데이터 대역), loopback,
#     unspecified, multicast 리터럴 IP 차단
#   * RFC1918 사설 IP·내부 DNS 는 **허용** — OPENMODEL 같은 사내 vLLM 엔드포인트가
#     정당한 사용처다. 사내망 차단은 프록시/네트워크 정책의 일이다.
_BLOCKED_ENDPOINT_HOSTNAMES = {"localhost", "localhost.localdomain"}

# loopback/link-local 플래그로 잡히지 않는 클라우드 메타데이터 주소를 명시 차단한다.
# fd00:ec2::254 는 EC2 IPv6 IMDS — ULA(fc00::/7, is_private)라 일반 플래그를 통과한다.
_BLOCKED_ENDPOINT_IPS = {
    ipaddress.ip_address("fd00:ec2::254"),  # EC2 IMDS (IPv6)
}


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """IPv6 임베디드 IPv4 껍질 벗기기 (A1-1).

    다음 형태는 IPv4 를 IPv6 안에 싣는데, `is_loopback`/`is_link_local` 등의
    플래그는 껍데기 주소 기준이라 임베디드 목적지를 안 본다 — `::127.0.0.1`
    (IPv4-compatible)과 `64:ff9b::7f00:1`(NAT64)은 `is_global=True` 로 통과했다.
      - IPv4-mapped   ::ffff:a.b.c.d   → ipaddress.ipv4_mapped
      - 6to4          2002:aabb:ccdd:: → ipaddress.sixtofour
      - IPv4-compat   ::a.b.c.d        → ::/96 최하위 32bit (::, ::1 제외)
      - NAT64         64:ff9b::a.b.c.d → 64:ff9b::/96 최하위 32bit
      - Teredo        2001::/32 → client IPv4 = 최하위 32bit 의 bit-flip
    """
    mapped = ip.ipv4_mapped or ip.sixtofour
    if mapped is not None:
        return mapped
    v6 = int(ip)
    if (v6 >> 32) == 0 and (v6 & 0xFFFFFFFF) > 1:  # ::/96, ::/::1 제외
        return ipaddress.IPv4Address(v6 & 0xFFFFFFFF)
    if (v6 >> 32) == (0x64FF9B << 64):  # NAT64 well-known prefix 64:ff9b::/96
        return ipaddress.IPv4Address(v6 & 0xFFFFFFFF)
    if (v6 >> 96) == 0x20010000:  # Teredo 2001:0::/32 — client IPv4 는 low32 bit-flip
        return ipaddress.IPv4Address((v6 & 0xFFFFFFFF) ^ 0xFFFFFFFF)
    return None


# RFC 8215 NAT64 local-use 예약 블록 — 외부 엔드포인트로 정당한 리터럴이 아니며
# PL=48 임베딩 레이아웃(u-octet 분리)이 다르고 복잡하므로 블록 전체를 차단한다.
_BLOCKED_V6_RANGES = (
    ipaddress.ip_network("64:ff9b:1::/48"),   # NAT64 local-use
)


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip in _BLOCKED_ENDPOINT_IPS
    ):
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        if any(ip in net for net in _BLOCKED_V6_RANGES):
            return True
        inner = _embedded_ipv4(ip)
        if inner is not None and _blocked_ip(inner):
            return True
    return False


def _resolved_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """호스트명을 OS 리졸버로 해소해 실제 접속 대상 IP를 돌려준다.

    `ipaddress.ip_address()`는 점표기 리터럴만 인식해 `2130706433`(decimal),
    `0x7f000001`(hex), `127.1`(축약), `localhost.`(trailing dot) 같은 표기를
    DNS 이름으로 오인한다 — 그런데 어댑터의 httpx/OS 리졸버는 이들을 실제
    주소로 해석해 접속한다(127.0.0.1 등). 저장 시점에 리졸브 결과를 검사해야
    이 표기 우회와 메타데이터로 리졸브되는 이름(169.254.169.254.nip.io 류)이
    함께 차단된다.

    잔여 한계(문서화): DNS rebinding — 검증 시점과 어댑터 요청 시점의 리졸브
    결과가 다른 이름은 이 검사로 못 막는다. 그 한계는 배포망 egress 정책의 몫이다.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        # admin-api 컨텍스트에서 리졸브가 안 되는 이름(사내 DNS 차이 등)은
        # 리터럴 검사 결과를 그대로 따른다 — 리졸브 불가 이름은 어차피 어댑터에서도
        # 연결이 안 되며, 정당한 내부 엔드포인트 등록을 막지 않기 위함이다.
        return []
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            ips.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return ips


def _validate_endpoint_url(v: str | None) -> str | None:
    if v is None:
        return None
    parsed = urlparse(v.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("endpoint_url은 http(s) URL이어야 합니다")
    if parsed.username or parsed.password:
        raise ValueError("endpoint_url에 자격증명(userinfo)을 포함할 수 없습니다")
    if parsed.fragment:
        raise ValueError("endpoint_url에 fragment(#)를 포함할 수 없습니다")
    host = parsed.hostname
    if not host:
        raise ValueError("endpoint_url에 유효한 호스트가 없습니다")
    if host.lower().rstrip(".") in _BLOCKED_ENDPOINT_HOSTNAMES:
        raise ValueError("endpoint_url에 loopback 호스트를 사용할 수 없습니다")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # DNS 이름/숫자형 표기 — 아래 리졸브 검사가 실제 목적지를 검증한다
    if ip is not None and _blocked_ip(ip):
        raise ValueError(
            "endpoint_url에 loopback/link-local/메타데이터 IP를 사용할 수 없습니다"
        )
    for resolved in _resolved_ips(host):
        if _blocked_ip(resolved):
            raise ValueError(
                "endpoint_url 호스트가 loopback/link-local/메타데이터 IP로 해석됩니다"
            )
    return v


# ── Requests ──


class ModelCreateRequest(BaseModel):
    # ⚠️ extra="forbid" — 오타 키 하나가 **201 Created 와 함께** 런타임에만 깨지는 행을
    #    만든다. 실측(적대적 검증 PROBE3): `endpoint_ur1` 로 보내면 endpoint_url=NULL 인
    #    BEDROCK_RUNTIME_OPENAI 행이 201 로 생성되는데, 그 어댑터는 endpoint_url 없이는
    #    서명 리전조차 유도할 수 없다. `displayName`(camelCase) → display_name NULL.
    #    0003 이전 이름 `cache_creation_price_per_1k_tokens` → 캐시 생성 단가 0 으로 등록.
    #    전부 등록 시점엔 성공으로 보이고 나중에 장애/오과금으로만 드러난다.
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(max_length=128)
    provider: ProviderEnum
    provider_model_id: str = Field(max_length=512)
    endpoint_url: str | None = None
    api_format: ApiFormatEnum
    description: str | None = None
    display_name: str | None = Field(default=None, max_length=128)
    input_price_per_1k_tokens: Decimal = Field(ge=0, le=MAX_PRICE_PER_1K, decimal_places=6)
    output_price_per_1k_tokens: Decimal = Field(ge=0, le=MAX_PRICE_PER_1K, decimal_places=6)
    cache_creation_5m_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    cache_creation_1h_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    cache_read_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )

    #: 스펙 정보 — ``None`` = 미상(수동 커스텀 모델). 카탈로그 동기화가 채우지 못한
    #: 신규 모델을 운영자가 수동 등록할 때 쓴다. ``ge=1`` — 0 이하 스펙은 무의미.
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)

    #: 이 모델을 쓸 수 있는 앱 허용목록. **3-상태**(models/model.py 주석 참조):
    #:   생략/``null``  제한 없음
    #:   ``[]``         명시적으로 빈 허용목록 = 어떤 앱도 허용되지 않음
    #:   목록           그 앱들만 허용
    allowed_clients: list[str] | None = None

    @field_validator("allowed_clients")
    @classmethod
    def _validate_clients(cls, v: list[str] | None) -> list[str] | None:
        # ⚠️ None 을 그대로 통과시켜야 한다 — [] 로 정규화하면 "제한 없음" 이
        #    "전면 거부" 로 바뀐다(정확히 반대 방향의 사고).
        return validate_clients(v)

    @field_validator("endpoint_url")
    @classmethod
    def _check_endpoint_url(cls, v: str | None) -> str | None:
        return _validate_endpoint_url(v)


class ModelUpdateRequest(BaseModel):
    # ⚠️ extra="forbid" 필수. pydantic 기본값(extra="ignore")이면 여기 선언되지 않은 키가
    #    **조용히 버려진다**. 특히 provider / api_format 은 이 스키마에 없고 update 경로
    #    (services/model_service.py:update_model → repositories/model_repository.py:update_model)
    #    에도 없어서 admin API 로 바꿀 방법이 아예 없는 불변 필드인데, admin-ui 편집 폼은
    #    provider 드롭다운을 그대로 보여준다. 예전엔 운영자가 provider 를 바꿔 저장하면
    #    200 + 성공 토스트가 뜨고 DB 는 새 provider_model_id/endpoint_url + **옛**
    #    provider/api_format 의 반쪽 상태로 남아, 그 alias 의 모든 게이트웨이 호출이
    #    런타임에만 실패했다(편집 시점 경고 0). 이제 422 로 즉시 거부한다.
    #    provider 를 정말 바꾸려면 모델을 새로 등록해야 한다(불변 유지가 의도된 설계).
    #
    #    ⚠️ 정정: 예전 주석은 "ModelCreateRequest/PricingRequest 의 extra 키는 무해한 오타"
    #       라며 그쪽엔 forbid 를 넣지 않았다. **틀렸다** — PricingRequest 는 캐시 단가
    #       default 가 0 이라 오타가 곧 0 원 청구이고 직전 단가 행은 이미 닫힌다.
    #       ModelCreateRequest 는 오타가 201 과 함께 런타임에만 깨지는 행을 만든다.
    #       세 스키마 모두 forbid 로 통일했다(각 클래스 주석에 실측 근거).
    model_config = ConfigDict(extra="forbid")

    provider_model_id: str | None = None
    endpoint_url: str | None = None
    description: str | None = None
    # max_length matches VARCHAR(128); without it an overlong update would 500 at the DB
    # instead of a clean 422 (mirrors ModelCreateRequest.display_name).
    # NOTE: update distinguishes "omitted" (keep) from "explicit null" (clear to NULL)
    # via model_fields_set — same rule as allowed_clients below.
    display_name: str | None = Field(default=None, max_length=128)
    #: 3-상태. ⚠️ 여기서 "생략 = 유지" 와 "명시적 null = 제한 해제" 를 구별해야 한다.
    #:    null 을 생략과 같이 다루면 한 번 목록이 박힌 모델을 "제한 없음" 으로 되돌릴 API
    #:    가 사라지고, 콘솔은 그 목적으로 ``[]`` 를 보내게 된다 — 그런데 ``[]`` 는 전면
    #:    거부이므로 "제한 해제" 버튼이 그 모델을 통째로 막는다.
    #:    구별은 서비스 계층에서 ``model_fields_set`` 으로 한다.
    allowed_clients: list[str] | None = None
    #: 스펙 정보 — 생략=유지, 명시적 null=삭제(미상으로 되돌림). model_fields_set 규칙은
    #: description/display_name 과 동일하게 서비스에서 처리한다.
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)

    @field_validator("allowed_clients")
    @classmethod
    def _validate_update_clients(cls, v: list[str] | None) -> list[str] | None:
        return validate_clients(v)

    @field_validator("endpoint_url")
    @classmethod
    def _check_update_endpoint_url(cls, v: str | None) -> str | None:
        return _validate_endpoint_url(v)


class PricingRequest(BaseModel):
    # ⚠️ extra="forbid" — 여기서는 **돈이 걸린다.** 이 스키마의 캐시 단가 3개는 default 가
    #    Decimal("0") 이라, 키 이름이 틀리면 값이 버려지고 0 이 들어간다. 그리고 이 엔드포인트는
    #    쓰기 전에 직전 단가 행을 close 하므로(services/model_service.py close_current_pricing),
    #    **새 ACTIVE 단가 행이 캐시 생성 비용을 0 으로 청구**하게 되고 되돌아갈 행도 없다.
    #    실측(적대적 검증 PROBE2): 0003 이전 이름 `cache_creation_price_per_1k_tokens` 로
    #    보내면 200, 기록된 행은 5m=0 · 1h=0, 직전 행은 이미 닫힘.
    #    ⇒ "extra 키는 무해한 오타" 라는 이전 판단은 이 스키마에서 반증됐다.
    #
    # le=MAX_PRICE_PER_1K: ModelCreateRequest 와 같은 이유(NUMERIC(10,6) 오버플로 → 500).
    # ⚠️ 이 스키마는 사용자 입력 외에 model_service.apply_price_sync 도 만들어 쓴다.
    #    AWS Price List 값이 비정상이면 DB 쓰기 전에 여기서 걸린다(더 이른 실패가 낫다).
    #    apply_price_sync 는 선언된 필드만 kwargs 로 넘기므로 forbid 의 영향을 받지 않는다.
    model_config = ConfigDict(extra="forbid")

    input_price_per_1k_tokens: Decimal = Field(ge=0, le=MAX_PRICE_PER_1K, decimal_places=6)
    output_price_per_1k_tokens: Decimal = Field(ge=0, le=MAX_PRICE_PER_1K, decimal_places=6)
    cache_creation_5m_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    cache_creation_1h_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    cache_read_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    effective_from: datetime


class StatusPatchRequest(BaseModel):
    active: bool


# ── Responses ──


class ModelPricingResponse(BaseModel):
    input_price_per_1k_tokens: Decimal
    output_price_per_1k_tokens: Decimal
    cache_creation_5m_price_per_1k_tokens: Decimal = Decimal("0")
    cache_creation_1h_price_per_1k_tokens: Decimal = Decimal("0")
    cache_read_price_per_1k_tokens: Decimal = Decimal("0")
    effective_from: datetime
    effective_until: datetime | None = None


class ModelResponse(BaseModel):
    alias: str
    provider: ProviderEnum
    provider_model_id: str
    endpoint_url: str | None = None
    api_format: ApiFormatEnum
    status: str
    description: str | None = None
    display_name: str | None = None
    #: ``None`` = 제한 없음, ``[]`` = 허용 앱 없음, 목록 = 그 앱만. 화면이 이 세 상태를
    #: 구별해 보여줘야 한다 — ``[]`` 를 "제한 없음" 으로 렌더하면 운영자가 자기가 만든
    #: 전면 거부를 보지 못한다.
    allowed_clients: list[str] | None = None
    #: 스펙 정보 — ``None`` = 미상(수동 커스텀 모델).
    context_window: int | None = None
    max_output_tokens: int | None = None
    current_pricing: ModelPricingResponse | None = None
    created_at: datetime
    updated_at: datetime


class ModelListResponse(BaseModel):
    items: list[ModelResponse]


# ── Price sync (AWS Price List API 동기화) ──


class PriceSyncDiff(BaseModel):
    """모델 1개의 현재 단가 vs AWS 공식 단가 diff(미리보기 전용, 쓰기 없음)."""

    alias: str
    provider_model_id: str
    matched: bool  # AWS Price List 에서 단가를 찾았나
    note: str | None = None  # 미매칭/주의 사유
    current: ModelPricingResponse | None = None  # DB 현재가(없을 수 있음)
    # AWS 에서 가져와 per-1k 정규화한 제안 단가(매칭 시)
    proposed_input_per_1k: Decimal | None = None
    proposed_output_per_1k: Decimal | None = None
    proposed_cache_5m_per_1k: Decimal | None = None
    proposed_cache_1h_per_1k: Decimal | None = None
    proposed_cache_read_per_1k: Decimal | None = None
    changed: bool = False  # 현재가와 제안가가 다른가
    #: 카탈로그 스펙(context_window/max_output_tokens)과 DB 값이 다른가.
    #: 단가 변경이 0이어도 이 플래그가 있으면 적용 대상에 포함되어야 한다 —
    #: 스펙 기록이 단가 변경에 묶여 있어 "변경 없음" 일 때 스펙이 영구히 비는
    #: 버그가 있었다.
    spec_changed: bool = False


class PriceSyncPreviewResponse(BaseModel):
    source: str = "aws_price_list_api"  # 출처 명시(IT 아님)
    region: str
    diffs: list[PriceSyncDiff]
    matched_count: int
    changed_count: int


class PriceSyncApplyRequest(BaseModel):
    """승인 후 적용할 alias 목록(명시 선택 — 자동 전체적용 금지)."""

    aliases: list[str] = Field(min_length=1)
    source: str = "aws"  # "aws" | "litellm"


class PriceSyncApplyResponse(BaseModel):
    applied: list[str]
    skipped: list[str]
    errors: list[str] = Field(default_factory=list)


class PriceSyncSourcesResponse(BaseModel):
    """사용 가능한 단가 소스 — litellm 은 Lambda 프록시 배포 시에만 true."""

    sources: dict[str, bool]


# ── Team Allowed Models ──


class AllowedModelsSetRequest(BaseModel):
    """Replace-all semantics: provided list becomes the new full whitelist.

    빈 리스트 = 전체 허용 (엔트리 전부 삭제).
    """

    model_aliases: list[str] = Field(default_factory=list)


class AllowedModelsResponse(BaseModel):
    team_id: str
    model_aliases: list[str]


class ModelDeletionImpactResponse(BaseModel):
    """DELETE /admin/models/{alias} 사전 영향 조회 — 확인 다이얼로그의 재료.

    각 필드는 삭제 시 같이 정리되거나 차단 사유가 되는 참조 수다.
    blocked: downgrade_policies.to_model_alias 참조가 있으면 true — 이 모델이
    다른 모델의 살아있는 fallback 목적지라서, 정책 해제 없이 지우면 예산 초과 시
    전환 대신 에러가 나므로 삭제를 거부한다.
    """

    alias: str
    usage_logs: int = 0
    pricings: int = 0
    team_allowed: int = 0
    user_allowed: int = 0
    rate_limits: int = 0
    downgrade_from: int = 0
    downgrade_to: int = 0
    blocked: bool = False


class ModelDeleteResponse(BaseModel):
    """삭제 결과 — 함께 정리된 자식 행 수를 돌려준다(토스트/감사 표시용)."""

    alias: str
    deleted: ModelDeletionImpactResponse
