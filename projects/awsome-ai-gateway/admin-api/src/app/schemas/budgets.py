# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from decimal import Decimal

from typing import Annotated

from pydantic import BaseModel, Field

from app.schemas.common import BudgetPolicy


# ── Requests ──


# DB 컬럼은 Numeric(12,4) — 상한 없이 두면 asyncpg 가 500 을 던진다.
_BUDGET_MAX = Decimal("99999999.99")


class SetBudgetRequest(BaseModel):
    # ⚠️ decimal_places 제약을 두지 않는다 — 초과 정밀도는 서비스 계층의
    #    _check_cent_precision 이 invalid_amount_precision(spec 코드)으로
    #    거부한다. 스키마가 먼저 422 를 내면 같은 위반이 두 가지 코드로 갈라진다.
    max_budget_usd: Decimal = Field(ge=0, le=_BUDGET_MAX)
    policy: BudgetPolicy = BudgetPolicy.HARD_BLOCK
    # ⚠️ 범위를 여기서도 강제한다. DB 는 ``budget.alert_pct`` 도메인(1..100)으로 막고
    #    admin-ui 는 zod 로 막는데, API 스키마만 비어 있었다 — 즉 UI 를 거치지 않는
    #    호출이 ``[150]`` 을 보내면 스키마를 통과해 DB 에서 터진다(422 여야 할 것이
    #    500 이 된다). 세 계층의 범위가 **같아야** 한다.
    #
    #    빈 목록은 허용한다 — "이 예산에는 알림을 보내지 않는다" 는 유효한 설정이고,
    #    막으면 알림을 끌 방법이 없다.
    alert_thresholds: list[Annotated[int, Field(ge=1, le=100)]] = Field(
        default=[80, 90, 100],
        description="Budget usage % thresholds for alert notifications (1-100; empty = no alerts)",
    )
    # TEAM scope 에서만 의미. 키 자체를 보내면 D 를 설정/해제(null)하고,
    # 보내지 않으면 기존 D 를 보존한다(model_fields_set 으로 구분, §3-1).
    default_user_cap_usd: Decimal | None = Field(default=None, ge=0, le=_BUDGET_MAX)
    # confirmation_required(409) 응답 뒤의 확인 재요청 표시 (§3-0).
    confirm: bool = False


class SetDefaultCapRequest(BaseModel):
    """PUT /team/{id}/default-cap — 팀 기본 유저 cap D 만 변경.

    value=null → D 해제(미설정 유저는 팀 한도만 적용), 0 → 미설정 유저 차단.
    """
    value: Decimal | None = Field(default=None, ge=0, le=_BUDGET_MAX)
    confirm: bool = False


class EqualSplitRequest(BaseModel):
    """POST /team/{id}/equal-split — D = floor_cent(T/N) 일괄 설정 (§4-3).

    clear_individual=True 면 모든 개별 A_u·app_c 를 같은 트랜잭션에서 제거하고
    전원 D 로 통일한다.
    """
    clear_individual: bool = False
    confirm: bool = False


class AllocateBudgetItem(BaseModel):
    user_id: str
    allocated_usd: Decimal = Field(ge=0, le=_BUDGET_MAX)


class AllocateBudgetRequest(BaseModel):
    allocations: list[AllocateBudgetItem] = Field(min_length=1)
    # confirmation_required(409) 응답 뒤의 확인 재요청 표시.
    confirm: bool = False


class SeedSpentItem(BaseModel):
    scope: str
    scope_id: str
    client: str | None = None
    period: str
    spent_usd: Decimal = Field(ge=0, decimal_places=4)


class SeedSpentRequest(BaseModel):
    items: list[SeedSpentItem] = Field(min_length=1)


class SeedSpentResult(BaseModel):
    scope: str
    scope_id: str
    client: str | None = None
    period: str
    status: str  # "ok" | "error"
    before_usd: Decimal | None = None
    after_usd: Decimal | None = None
    error: str | None = None


class SeedSpentResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    results: list[SeedSpentResult]


# ── Responses ──


class BudgetSummaryItem(BaseModel):
    target_type: str
    target_id: str
    target_name: str | None = None
    team_id: str | None = None
    is_active: bool = True
    limit_usd: Decimal | None  # None = 개인 예산 미설정 (팀 예산 적용)
    used_usd: Decimal
    remaining_usd: Decimal | None
    usage_pct: Decimal | None
    department_id: str | None = None
    department_name: str | None = None
    # TEAM 행만: 기본 유저 cap D(§3-2). USER 행은 항상 None.
    default_user_cap_usd: Decimal | None = None
    # USER 행만: 적용 중인 cap 의 출처 — "individual"(A_u) | "team_default"(D) |
    # None(팀 한도만). UI 의 '미설정(D)' 표시용.
    cap_source: str | None = None
    # TEAM 행만: 다운그레이드 최신 배치 규칙 수 + 활성 여부.
    # "최신 배치" 기준은 get_current_rules(펼친 패널이 보는 것)와 동일 — 꺼진
    # (is_active=false) 배치도 규칙이 화면에 보이므로 is_active 집계가 아니다.
    downgrade_rule_count: int | None = None
    downgrade_enabled: bool | None = None
    # 저장된 알림 임계값. ⚠️ 예전에는 쓰기 전용이었다 — UI 는 값을 보낼 수 있지만 되읽을
    # 수 없어서, 다이얼로그를 다시 열면 항상 [80,90,100] 이 보였다. 예산 미설정 대상은
    # None(설정 자체가 없다)이고 빈 목록은 "알림 없음" 이라는 유효한 설정이다.
    alert_thresholds: list[int] | None = None


class BudgetSummaryResponse(BaseModel):
    period: str
    summary: list[BudgetSummaryItem]


class BudgetConfigDetailResponse(BaseModel):
    """GET /{scope}/{scope_id} — 예산 설정 다이얼로그 prefill 용 현재 설정.

    다이얼로그가 금액만 바꿔 저장해도 policy/thresholds 가 프론트 기본값으로
    리셋되지 않으려면, 열 때 현재값을 미리 채워야 한다.

    - ``configured=False``: 활성 총액 config 없음 → 프론트는 기본값으로 시작.
    - ``alert_thresholds=None``: thresholds 의 유일한 저장소인 Redis 키가
      만료/미스라 서버값을 모른다 → 프론트는 기본값을 **표시만** 하고,
      관리자가 thresholds 를 건드리지 않으면 PUT 에서 생략해 보존한다.
    """
    configured: bool
    max_budget_usd: Decimal | None = None
    policy: BudgetPolicy | None = None
    alert_thresholds: list[int] | None = None
    default_user_cap_usd: Decimal | None = None


class AppBudgetItem(BaseModel):
    client: str
    max_budget_usd: Decimal
    policy: BudgetPolicy


class UserAppBudgetsResponse(BaseModel):
    user_id: str
    apps: list[AppBudgetItem]


# ── Team Allocation ──


class AllocationEntry(BaseModel):
    target_id: str
    target_name: str
    target_type: str  # TEAM | USER
    # USER 행의 계정 역할(ADMIN|TEAM_LEADER|USER) — Type 열을 역할 뱃지로 구분하기 위함.
    # TEAM 행은 None.
    target_role: str | None = None
    allocated_usd: Decimal
    used_usd: Decimal
    remaining_usd: Decimal
    alert_level: str  # NORMAL | WARNING | CRITICAL
    # USER 행만: cap 의 출처 — "individual"(A_u 명시 설정) | "team_default"(D 상속)
    # | None(개인 cap 없음, 팀 한도만). TEAM 행은 None.
    cap_source: str | None = None
    # USER 행만: 실효 cap = A_u ?? D. 개인 cap 이 없으면 None(팀 한도만 적용).
    effective_cap_usd: Decimal | None = None


class TeamBudgetAllocation(BaseModel):
    team_id: str
    team_name: str
    total_budget_usd: Decimal
    entries: list[AllocationEntry]
    # 팀 기본 유저 cap D(§3-2). null=미설정.
    default_user_cap_usd: Decimal | None = None
    # 초과 약정(overcommit) 지표 — ΣA_u 가 T 를 넘어도 되는 CAP 모델의 참고값.
    sum_allocated_usd: Decimal | None = None
    overcommit_ratio: Decimal | None = None  # ΣA_u / T (T=0/None 이면 None)


# ── Auto-Downgrade ──


class DowngradeRuleItem(BaseModel):
    from_model_alias: str
    to_model_alias: str
    threshold_pct: int = Field(ge=1, le=100)


class AutoDowngradeConfigRequest(BaseModel):
    enabled: bool = True
    # enabled=False(끄기 저장)는 규칙을 요구하지 않는다 — service 계층이
    # enabled=True 일 때 빈 규칙을 거부한다.
    rules: list[DowngradeRuleItem] = Field(default_factory=list)


class DowngradeRuleResponse(BaseModel):
    id: str
    from_model_alias: str
    to_model_alias: str
    threshold_pct: int
    is_active: bool
    created_at: str


class AutoDowngradeConfigResponse(BaseModel):
    scope: str
    scope_id: str
    enabled: bool
    rules: list[DowngradeRuleResponse]
