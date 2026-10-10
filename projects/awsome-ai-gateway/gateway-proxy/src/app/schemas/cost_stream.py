# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.periods import date_at, period_at


class CostStreamEntry(BaseModel):
    """Redis Stream `cost:stream`에 XADD 되는 단일 비용 레코드.

    gateway-proxy는 요청 완료 시점에 이 레코드를 XADD하고,
    cost-recorder-worker가 XREADGROUP으로 배치 소비 → DB INSERT/UPSERT.

    **Idempotency**: `request_id` UNIQUE 제약이 usage_logs에 있어 중복 소비되어도
    `ON CONFLICT DO NOTHING` 으로 dedup됨.
    """

    request_id: str
    user_id: str
    team_id: str
    dept_id: str
    model_alias: str
    provider: str

    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # Reasoning/thinking tokens — visibility submetric (already inside output_tokens
    # for GPT-5.x; Anthropic extended-thinking lands here too). NOT a billing input.
    reasoning_tokens: int = 0
    # Server-side web search calls for this request — attribution metric, NOT billing.
    web_search_count: int = 0
    cost_usd: Decimal

    latency_ms: int
    # TTFT(time to first token) in ms. 스트리밍 첫 콘텐츠 델타까지의 시간.
    # 비스트리밍/미검출은 latency_ms와 동일 값. 구버전(schema_version 1) 엔트리는 부재 → None.
    ttft_ms: int | None = None
    is_streaming: bool = False
    estimated_usage: bool = False
    downgraded_from: str | None = None
    availability_fallback_from: str | None = None

    requested_at: str  # ISO format
    completed_at: str  # ISO format
    period: str  # YYYY-MM (for budget_usages)
    date: str  # YYYY-MM-DD (for daily counter + daily_aggregates)

    threshold_triggered: int | None = None
    # "user" | "team" — 어느 스코프의 크로싱이 threshold_triggered 를 채웠는지.
    # 이전엔 TEAM 스코프 crossing 결과를 버려서, 개인 예산 없이 팀 예산만 쓰는
    # 케이스에서 budget_threshold 알림이 전혀 발행되지 않는 버그가 있었다.
    threshold_scope: str | None = None
    threshold_policy: str | None = None

    sso_subject: str | None = None  # OIDC sub or stable user identifier for Bedrock metadata
    bedrock_request_id: str | None = None
    client: str | None = None  # "claude-code" | "cowork" | "other" — identification tag
    # 청구 티어 감사 (마이그레이션 0039). "short"|"long"|None(티어 없는 모델).
    # 게이트웨이 resolve_context_tier 가 청구와 동일 판정으로 채운다.
    context_tier: str | None = None

    schema_version: int = Field(default=2)

    @classmethod
    def make(
        cls,
        *,
        request_id: str,
        user_id: str,
        team_id: str,
        dept_id: str,
        model_alias: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
        cost_usd: Decimal,
        reasoning_tokens: int = 0,
        web_search_count: int = 0,
        latency_ms: int,
        ttft_ms: int | None = None,
        is_streaming: bool,
        estimated_usage: bool,
        downgraded_from: str | None,
        availability_fallback_from: str | None = None,
        threshold_triggered: int | None = None,
        threshold_scope: str | None = None,
        threshold_policy: str | None = None,
        sso_subject: str | None = None,
        bedrock_request_id: str | None = None,
        client: str | None = None,
        context_tier: str | None = None,
        requested_at: datetime | None = None,
    ) -> CostStreamEntry:
        now = datetime.now(tz=UTC)
        # 요청 시작 절대시각 — 미들웨어가 심은 값을 cost_recorder 가 넘긴다.
        # 미설정(테스트·미들웨어 우회)이면 완료 시각으로 폴백한다.
        started = requested_at or now
        return cls(
            request_id=request_id,
            user_id=user_id,
            team_id=team_id,
            dept_id=dept_id,
            model_alias=model_alias,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            reasoning_tokens=reasoning_tokens,
            web_search_count=web_search_count,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            is_streaming=is_streaming,
            estimated_usage=estimated_usage,
            downgraded_from=downgraded_from,
            availability_fallback_from=availability_fallback_from,
            # requested_at 은 **요청 시작** 시각이다 — 게이트웨이의 Redis 예산
            # 카운터가 request_period()(시작 월)에 귀속되므로, budget_usages 행과
            # usage:daily:* 카운터도 같은 시작 시각 버킷에 두어야 두 소스가
            # 월/일 경계에서 어긋나지 않는다. 예전엔 둘 다 완료 시각의 버킷을 썼다.
            requested_at=started.isoformat(),
            completed_at=now.isoformat(),
            # ⚠️ period/date 는 UTC 가 아니라 **KST** 경계다(app.periods 참조).
            # 이 두 값이 cost-recorder-worker 에서 그대로 키가 된다:
            #   period → budget.budget_usages.period 행 + budget:*:{period} Redis 키
            #   date   → usage:daily:user:{uid}:{date} Redis 카운터
            # 집계 데이터는 전부 KST 로 버킷되고(daily_aggregator 는 AT TIME ZONE
            # 'Asia/Seoul') admin-api 는 KST 월로 읽으므로(§59), 여기서 UTC 로 쓰면
            # 매월/매일 경계에서 9시간 어긋난다. requested_at/completed_at 은 절대시각
            # (timestamptz)이므로 UTC 그대로가 맞다 — 버킷 라벨만 KST 다.
            period=period_at(started),
            date=date_at(started),
            threshold_triggered=threshold_triggered,
            threshold_scope=threshold_scope,
            threshold_policy=threshold_policy,
            sso_subject=sso_subject,
            bedrock_request_id=bedrock_request_id,
            client=client,
            context_tier=context_tier,
        )
