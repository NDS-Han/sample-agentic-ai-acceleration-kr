# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import BudgetConfig, BudgetScope, BudgetUsage
from app.schemas.domain import BudgetPolicy, BudgetStatus
from app.services.lua_loader import LuaScriptLoader

logger = structlog.get_logger(__name__)


class BudgetBackendUnavailableError(PermissionError):
    """예산 체크 백엔드(DB+Redis 양쪽) 불가 — 예산 거절(429)과 구분한다.

    PermissionError 를 상속해 기존 호출부는 유지되지만, 미들웨어는 이 서브클래스를
    먼저 잡아 503 을 낸다 — 인프라 장애를 'no_budget_assigned' 429 로 오보하면
    사용자가 예산을 문의하고 운영자는 장애 원인을 못 찾는다.
    """


async def _safe_rollback(db) -> None:
    """best-effort DB 구문 실패 후 세션을 되돌린다 — 롤백 자체 실패는 무시.

    Postgres 는 한 구문이 실패하면 트랜잭션이 aborted 상태로 남아 이후 모든
    구문이 InFailedSQLTransaction/PendingRollbackError 로 연쇄 실패한다.
    """
    if db is None:
        return
    try:
        await db.rollback()
    except Exception:
        pass


BUDGET_CONFIG_TTL = None  # 무기한 (Admin 변경 시 DEL로 무효화)

# per-app(client) 예산 대상 클라이언트. client_identifier 토큰과 동일 집합
# (claude-code / cowork / codex). 새 앱 추가 시 여기 + DB CHECK 제약 + admin-api 동기화.
PER_APP_BUDGET_CLIENTS = ("claude-code", "cowork", "codex")


def settle_eval_args(resv: dict) -> tuple:
    """budget_settle eval 의 키 목록 — 예약 스펙에 인덱스 키가 있으면 5키.

    롤링 배포 창에 구버전 파드가 만든 스펙에는 idx_key/idx_field 가 없다 —
    그 경우 4키 호출로 settle 은 그대로 동작하고 인덱스 HDEL 만 건너뛴다
    (인덱스에 안 실린 마커는 rebuild 의 SCAN 폴백이 잡는다, R22 r1 S1).
    호출자는 ARGV 로 ``(actual, fallback, idx_field or "")`` 를 이어 붙인다 —
    4키 경로의 빈 idx_field 는 Lua 가 무시한다. 이 분기를 여기 한 군데에만
    둔다 — cost_recorder 와 settle_budget 이 따로 들고 있으면 계약 변경 시
    한쪽만 갱신돼 인덱스 HDEL 이 조용히 빠진다(R22 r4 S2).
    """
    if resv.get("idx_key") and resv.get("idx_field"):
        return (
            5,
            resv["usage_key"], resv["config_key"], resv["marker_key"],
            resv["resvsum_key"], resv["idx_key"],
        )
    return (
        4,
        resv["usage_key"], resv["config_key"], resv["marker_key"],
        resv["resvsum_key"],
    )

# 정책 파라미터 기본값 — DB 스키마에 없으므로 Python 기본값 사용.
# 향후 budget_configs에 컬럼 추가 시 (requirements.md §6b 장래 추가 항목) DB 값으로 교체.
DEFAULT_SOFT_LIMIT_PCT = 110
DEFAULT_THROTTLE_RPM_PCT = 50
DEFAULT_THRESHOLDS = [80, 90, 100]


def _row_thresholds(config) -> list[int]:
    """``BudgetConfig`` 행의 알림 임계값 — DB 가 진실의 원천이다(migration 0041).

    ⚠️ 빈 배열은 **유효한 설정**이고 "이 예산에는 임계값 알림을 보내지 않는다" 를 뜻한다.
       그래서 ``or DEFAULT_THRESHOLDS`` 로 채우지 않는다 — 그러면 운영자가 의도적으로 비운
       설정을 재수화가 되살린다.

    컬럼이 없는 구 스키마(0037 미적용)에서는 속성 자체가 없으므로 그때만 기본값을 쓴다.
    """
    raw = getattr(config, "alert_thresholds", None)
    if raw is None:
        return list(DEFAULT_THRESHOLDS)
    return sorted({int(v) for v in raw})


def _policy_to_lua_value(policy: BudgetPolicy | str) -> str:
    """Python BudgetPolicy(lowercase value) → Lua/Redis 내부 표현(lowercase)."""
    return policy.value if isinstance(policy, BudgetPolicy) else str(policy).lower()


def _db_policy_to_domain(db_policy) -> BudgetPolicy:
    """DB enum(UPPERCASE) → domain BudgetPolicy(lowercase value).

    admin-api `BudgetPolicy` ORM과 gateway-proxy `schemas.domain.BudgetPolicy`의
    value 표기가 다름(UPPERCASE vs lowercase). 변환 계층.
    """
    raw = db_policy.value if hasattr(db_policy, "value") else str(db_policy)
    return BudgetPolicy(raw.lower())



def _as_client_list(value) -> list[str]:
    """``app_clients`` 필드를 리스트로 정규화한다.

    admin-api 가 이 필드를 Lua 로 병합하는데(core/budget_cache.py — 로그인과 예산
    변경이 서로의 필드를 지우던 클로버를 막기 위해), Redis 의 cjson 은 빈 배열을
    표현할 수 없어 ``[]`` 를 ``{}`` 로 인코딩한다(실측: redis 7.4). 둘 다 "활성
    per-app 예산이 없다" 는 뜻이다.

    ⚠️ 비어 있지 않은 dict 는 **리스트로 바꾸지 않는다** — 그건 예상 밖의 형상이고,
       키를 client 이름으로 착각해 통과시키면 없는 예산을 있다고 판정할 수 있다.
       그런 경우는 빈 리스트로 떨어뜨려(= per-app 평가 없음) 부모 USER 예산만 걸린다.
    """
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, dict) and not value:
        return []
    return []

def _evaluate_layer(
    used: Decimal, limit: Decimal, policy: BudgetPolicy
) -> tuple[str | None, bool, bool]:
    """한 예산 계층의 판정. ``(block_reason_suffix, soft_warning, throttle_active)``.

    ⚠️ 이 함수가 따로 있는 이유. DB 폴백은 오랫동안 **정책을 보기 전에** 무조건
       ``used >= limit`` 에서 차단했다. Redis 경로(``redis_scripts/budget_check.lua``)는
       같은 상태에서 정책을 먼저 본다:

         hard_block    100% 에서 차단
         soft_warning  soft_limit_pct(기본 110%) 까지 허용, 100% 를 넘으면 경고 플래그만
         throttle      **차단하지 않는다** — RPM 만 줄인다

       그래서 SOFT_WARNING 팀이 $1000.50/$1000 인 상태에서 Redis 가 한 번 타임아웃되면,
       Redis 경로였다면 통과했을 요청이 DB 폴백에서는 429 가 됐다 — Redis 가 degrade 된
       동안 팀 전원이 막힌다. THROTTLE 도 같다: 설계상 절대 차단하지 않는 정책인데 폴백에서만
       100% 에서 막혔다.

       부수적으로 ``team_soft_limit_exceeded`` 와 ``soft_warning=True`` 는 폴백 경로에서
       **도달 불가능한 죽은 코드**였다(무조건 raise 가 먼저였다). 즉 ``X-Budget-Warning``
       헤더가 Redis 다운 중에는 나오지 않았다.

    ⚠️ 판정 **순서**가 Lua 와 같아야 한다(hard_block → soft_warning → throttle). 순서가
       달라지면 같은 상태가 경로에 따라 다르게 판정된다 — 그것이 원래의 결함이다.

    ⚠️ 이 변경으로 폴백은 이전보다 **느슨해진다**: Redis 다운 중 SOFT_WARNING 사용자는
       110% 까지 쓸 수 있고 THROTTLE 사용자는 100% 에서 막히지 않는다. 그것이 저장된 정책이
       말하는 바이고 Redis 경로가 이미 그렇게 동작하지만, 돈이 나가는 동작의 변경이다.
    """
    # D-8/I-1: limit = 0 은 정책과 무관하게 항상 차단 — SOFT_WARNING/THROTTLE 의
    # `limit > 0` 가드 뒤로 명시적 0 cap 이 빠져나가던 구멍을 막는다.
    if limit <= 0:
        return "budget_exceeded", False, False

    if policy == BudgetPolicy.HARD_BLOCK and used >= limit:
        return "budget_exceeded", False, False

    soft_warning = False
    if policy == BudgetPolicy.SOFT_WARNING and limit > 0:
        effective_limit = limit * Decimal(DEFAULT_SOFT_LIMIT_PCT) / Decimal(100)
        if used >= effective_limit:
            return "soft_limit_exceeded", False, False
        if used >= limit:
            soft_warning = True

    throttle_active = False
    if policy == BudgetPolicy.THROTTLE:
        usage_pct = int(used / limit * 100) if limit > 0 else 0
        for threshold in sorted(DEFAULT_THRESHOLDS, reverse=True):
            if usage_pct >= threshold:
                throttle_active = True
                break

    return None, soft_warning, throttle_active


def _build_status(tiers: list[tuple[str, dict]], team_result: dict) -> BudgetStatus:
    """평가된 계층 결과들을 §6-3/§6-5 규칙으로 하나의 BudgetStatus 로 조합한다.

    각 tier dict 는 ``limit_usd, used_usd, policy(lowercase), soft_warning,
    throttle_active, throttle_rpm_pct, threshold_pct`` 를 가진다
    (Redis 경로 = budget_check.lua 의 JSON, DB 폴백 = 같은 shape 로 정규화).

    - ``remaining_usd`` = 모든 계층의 "차단 시점까지의 잔여" 최솟값.
      SOFT_WARNING 은 ``limit × soft_limit_pct/100 - used`` (실효 한도 기준),
      THROTTLE 계층은 금액 차단이 없으므로 후보에서 제외(§6-5).
    - ``tier`` = 최솟값을 만든 계층명 — 헤더의 Limit/Used 비노출 게이팅에 사용.
    - ``warning_tiers`` = soft_warning 상태인 계층 전부 (헤더용, §6-3).
    - ``throttle_rpm_pct`` = 활성 계층 중 **가장 작은** 값 (§6-3).
    - ``policy``/``threshold_pct``/``thresholds`` = TEAM 결과 그대로 —
      downgrade middleware·soft_limit_pct 등 기존 소비자의 의미를 유지한다.
    """
    best: tuple[Decimal, str, dict] | None = None
    warning_tiers: list[str] = []
    active_throttle_pcts: list[int] = []

    for name, r in tiers:
        if r.get("soft_warning"):
            warning_tiers.append(name)
        if r.get("throttle_active"):
            active_throttle_pcts.append(int(r.get("throttle_rpm_pct") or 50))

        policy = r.get("policy", "hard_block")
        if policy == BudgetPolicy.THROTTLE.value:
            continue
        limit = Decimal(str(r.get("limit_usd", 0)))
        used = Decimal(str(r.get("used_usd", 0)))
        # 계층별 soft_limit_pct 를 반영 — Lua 는 config 의 값을 쓰므로
        # 조합도 같은 값을 써야 잔여 계산이 판정과 일치한다.
        soft_pct = r.get("soft_limit_pct") or DEFAULT_SOFT_LIMIT_PCT
        effective = (
            limit * Decimal(str(soft_pct)) / Decimal(100)
            if policy == BudgetPolicy.SOFT_WARNING.value
            else limit
        )
        remaining = effective - used
        if best is None or remaining < best[0]:
            best = (remaining, name, r)

    # team 계층은 도달 시 항상 config_present — best 는 비어 있을 수 없다.
    assert best is not None
    remaining, tier, decisive = best
    # 임계값은 팀 계층 dict 에 실린 값을 쓴다 — Redis 경로의 lua 결과는 이 키를
    # 싣지 않으므로(None → 기본값) 예전 동작과 같고, DB degrade 경로는 호출자가
    # ``_row_thresholds(team_config)`` 를 넣어준다. 빈 배열은 유효한 설정
    # (알림 끔)이라 ``or`` 로 기본값을 쓰지 않는다(_row_thresholds 주석 참조).
    thresholds = team_result.get("thresholds")
    return BudgetStatus(
        remaining_usd=remaining,
        limit_usd=Decimal(str(decisive.get("limit_usd", 0))),
        used_usd=Decimal(str(decisive.get("used_usd", 0))),
        policy=BudgetPolicy(team_result.get("policy", "hard_block")),
        throttle_rpm_pct=min(active_throttle_pcts) if active_throttle_pcts else 50,
        threshold_pct=team_result.get("threshold_pct", 0),
        thresholds=thresholds if thresholds is not None else list(DEFAULT_THRESHOLDS),
        throttle_active=bool(active_throttle_pcts),
        soft_warning=bool(warning_tiers),
        tier=tier,
        warning_tiers=warning_tiers,
    )


class BudgetService:
    """예산 정책 확인 서비스."""

    @staticmethod
    async def _index_in_warmup(redis) -> bool:
        """pendingidx 인덱스가 모든 마커를 커버한다고 신뢰할 수 있는 창인가.

        롤링 배포·신규 도입 직후 ``budget_reservation_ttl_seconds``(마커 수명)
        동안은 구버전 파드가 만든 인덱스 없는 마커가 살아 있을 수 있다 —
        그 창에는 rebuild 가 인덱스 대신 키스페이스 SCAN 을 써야 한다
        (R22 r2 S6). 첫 신버전 관측 시각을 ``budget:pendingidx:since`` 에
        NX 로 세우고, 그 시각부터 마커 TTL 이 지나면 웜업 종료 — 그때 살아남은
        마커는 전부 신버전이 쓴 인덱스 포함분이다.
        """
        from app.config import get_settings

        settings = get_settings()
        if not settings.budget_pendingidx_warmup:
            # 롤아웃 완료 선언 — 인덱스 미포함 구버전 파드가 없으므로 웜업 불필요.
            # Redis 플러시 직후의 rebuild 폭풍 창에서도 SCAN 폴백으로 돌아가지
            # 않는다(R22 r4 N5).
            return False
        ttl = settings.budget_reservation_ttl_seconds
        try:
            raw = await redis.get("budget:pendingidx:since")
            if raw is None:
                # 만료 없이 둔다 — ttl*2 로 만료를 걸면 만료 시점부터 웜업이
                # 다시 시작돼 무한 반복된다(시간의 절반 가까이 SCAN 폴백,
                # R22 r3 S3). Redis 전체가 플러시되면 키가 사라져 웜업이
                # 재시작되는데, 그 창은 rebuild 가 가장 몰리는 시점이라 SCAN
                # 폭풍이 된다 — 롤아웃 완료 후에는 BUDGET_PENDINGIDX_WARMUP=false
                # 로 이 경로 자체를 끈다(R22 r4 N5).
                await redis.set(
                    "budget:pendingidx:since",
                    str(int(time.time())),
                    nx=True,
                )
                return True
            first = float(raw.decode() if isinstance(raw, bytes) else raw)
            return (time.time() - first) < ttl
        except Exception:
            return True  # 불확실하면 보수적으로 SCAN

    @staticmethod
    async def _pending_reservation_sum(redis, pattern: str) -> Decimal:
        """살아있는 예약 마커(budget:pending:*) 값의 합 (키스페이스 SCAN).

        카운터 유실 복원 시 DB 실비에 미정산 예약을 다시 얹기 위한 최선 노력
        조회다 — SCAN 은 best-effort 라, 마커/카운터가 동시에 유실된 경우에는
        0 을 돌려주고 settle 은 마커 부재로 안전하게 no-op 이 된다.
        정상 상태에서는 rebuild lua 가 pendingidx 인덱스로 pending 을 계산해
        이 SCAN 이 돌지 않는다(R22 r2 — 인덱스 웜업 창에서만 폴백).
        """
        total = Decimal("0")
        try:
            async for key in redis.scan_iter(match=pattern, count=200):
                # 이행기 잔재 — 구현이 만든 `:done` 툼스톤은 값이 실비라
                # 라이브 마커로 오인해 합산되면 안 된다(R23 r5 N-1).
                kstr = key.decode() if isinstance(key, bytes) else key
                if kstr.endswith(":done"):
                    continue
                val = await redis.get(key)
                if val is None:
                    continue
                try:
                    # ⚠️ bytes 디코딩 필수 — 클라이언트는 decode_responses=False 라
                    #    get() 이 bytes 를 돌려주고, str(b'1.5')="b'1.5'" 는
                    #    InvalidOperation 으로 조용히 0 합산이 됐다(진행 중
                    #    예약이 재수화/교정에서 전부 누락 → 카운터 얇아짐).
                    total += Decimal(
                        val.decode() if isinstance(val, bytes) else str(val)
                    )
                except InvalidOperation:
                    continue
        except Exception:
            logger.exception("pending_reservation_scan_failed", pattern=pattern)
            return Decimal("0")
        return total

    async def _rebuild_counter(
        self,
        redis,
        *,
        usage_key: str,
        resvsum_key: str,
        idx_key: str,
        marker_prefix: str,
        field_prefix: str,
        db_base,
    ) -> bool:
        """budget_rebuild.lua 호출. 트리거가 이미 해소됐으면 False.

        정상 상태에서는 pending 을 Lua 가 pendingidx+마커로 원자 계산한다 —
        Python 이 읽고 eval 하기까지의 창에 들어온 예약도 잡힌다. 인덱스
        웜업 창(롤링 배포·도입 직후 marker_ttl)에만 Python 이 SCAN 한 값을
        넘긴다 — 그 창에는 구버전 파드가 만든 인덱스 없는 마커가 살아
        있을 수 있다(R22 r2 S6).
        """
        pending_arg = ""
        if await self._index_in_warmup(redis):
            pending_arg = str(
                await self._pending_reservation_sum(
                    redis, marker_prefix + field_prefix + "*"
                )
            )
        applied = await redis.eval(
            LuaScriptLoader.get("budget_rebuild"),
            3,
            usage_key,
            resvsum_key,
            idx_key,
            str(db_base),
            marker_prefix,
            field_prefix,
            pending_arg,
        )
        return bool(applied)

    async def check_budget(
        self,
        redis,
        db: AsyncSession | None,
        user_id: str,
        team_id: str,
        period: str,
        client: str | None = None,
    ) -> BudgetStatus:
        """사용자 예산 확인 (Redis 우선, DB fallback).
        예산 미설정 시 PermissionError 발생 (429 no_budget_assigned).
        client 가 'claude-code' 또는 'cowork' 이면 앱별 예산도 추가로 확인한다.
        """
        # D-14/§6-1 단계 0: 팀 미배정 유저는 fail-closed — team_id='' 로 만들어진
        # `budget:team:{}` 키 부재가 team_budget_unset 과 구분되도록 별도 코드.
        if not team_id:
            raise PermissionError("no_team_assigned")

        # Redis Cluster hash tag: {<user_id>} co-locates usage/config on same slot.
        user_key = f"budget:user:{{{user_id}}}:{period}"
        user_config_key = f"budget:config:user:{{{user_id}}}"
        team_key = f"budget:team:{{{team_id}}}:{period}"
        team_config_key = f"budget:config:team:{{{team_id}}}"

        # Redis 예산 설정 확인
        if redis is not None:
            # 어느 지점의 Redis 실패든 같은 계약 — DB fallback. 재수화
            # exists() 사전 호출까지 try 안이어야 헬스체커 마킹 전 미드요청
            # 단절이 500 이 아니라 fallback 으로 간다.
            try:
                # TEAM config cold-cache fallback: init SQL / alembic backfill 로 DB에
                # 삽입된 TEAM 예산이 Redis에 없으면 DB에서 조회해 캐시 (admin-api startup
                # warmup 의 안전망).
                if db is not None and not await redis.exists(team_config_key):
                    await self._hydrate_team_config_cache(redis, db, team_id)

                # P0-③: USER config cold-cache fallback. If admin's best-effort SET of
                # the user-config key (which carries app_clients) was lost, the per-app
                # budget gate below would silently skip → budget bypass. Rehydrate from
                # DB on miss (ensure_config_cached rebuilds app_clients from active
                # per-app BudgetConfig rows) so the gate stays enforced.
                user_config_present = await redis.exists(user_config_key)
                if db is not None and not user_config_present:
                    await self.ensure_config_cached(redis, db, user_id)
                    user_config_present = await redis.exists(user_config_key)

                # Redis 키 없으면 DB에서 복구 후 재캐싱 (LRU 삭제 / failover 대비)
                #
                # ⚠️ 이 경로는 **죽은 코드였다 — fail-OPEN 방향으로.** 쿼리에
                #    `client IS NULL` 이 없어서, 그 (user, period) 에 per-app 행이 **하나만**
                #    있어도 `scalar_one_or_none()` 이 MultipleResultsFound 로 터지고, 아래
                #    `except Exception` 이 그것을 삼켰다. 그러면 user_key 가 복원되지 않은
                #    채로 budget_check.lua 가 돌아 `used=0` 을 읽는다 — config 는 별도 키에서
                #    재수화되므로 `config_present` 는 true 다. 즉 **그 사용자의 월 사용액
                #    전체가 0 으로 리셋되고 전부 통과한다.**
                #
                #    per-app 행은 pub 에서 실제로 만들어진다(admin-api seed_spent 가
                #    client 를 받아 INSERT 한다). 그리고 이 파일의 DB 폴백 경로는 이미 같은
                #    교훈을 배워 `client.is_(None)` 을 걸고 있었다 — 복원 경로만 남겨졌다.
                # 카운터가 없거나(**음수**)면 복원 — 축출된 키 위로 in-flight
                # settle 의 INCRBYFLOAT 델타만 담긴 음수 키가 생기면, 그 키가
                # 존재한다는 이유로 복원이 스킵돼 한도가 그 달 내내 해제됐다.
                # 카운터를 생성한 스크립트가 세운 ``:lost`` 센티넬도 트리거 —
                # 양수 첫 쓰기(reserve/deduct)는 키를 존재·양수로 만들어
                # 존재/부호 검사만으로는 유실을 영구히 못 본다(R22 r1 M1).
                # rebuild.lua 는 플로어 방식이라(cur < db+live 일 때만 SET)
                # 정상 월 첫 쓰기의 센티넬 발화도 no-op 으로 수렴한다.
                _rebuild_needed = False
                if db is not None:
                    # 같은 {uid} 슬롯이라 MGET 1회로 카운터+센티넬을 묶는다 —
                    # 키별 GET/EXISTS 왕복을 요청마다 반복하지 않게(R22 r4 N3).
                    # 앱 카운터 단독 유실도 같이 본다 — 유저 총합 키가 멀쩡해도
                    # 현재 요청의 client 앱 키가 유실(부재·음수·센티넬)이면 같은
                    # 복원 블록을 태워야 per-app 하드캡이 해제되지 않는다
                    # (R22 r3 S2). 복원 블록은 진입하면 PER_APP 전체를 순회.
                    _probe = [user_key, f"{user_key}:lost"]
                    _app_key = (
                        f"budget:user:{{{user_id}}}:{client}:{period}"
                        if client in PER_APP_BUDGET_CLIENTS
                        else None
                    )
                    if _app_key:
                        _probe += [_app_key, f"{_app_key}:lost"]
                    _vals = await redis.mget(_probe)
                    _uk = _vals[0]
                    _rebuild_needed = (
                        _uk is None
                        or float(_uk) < 0
                        or _vals[1] is not None
                    )
                    if not _rebuild_needed and _app_key:
                        _cv = _vals[2]
                        _rebuild_needed = (
                            _cv is None
                            or float(_cv) < 0
                            or _vals[3] is not None
                        )
                if _rebuild_needed:
                    # 다른 파드가 rebuild(DB 읽기) 중이면 이번 요청은 쿼리 없이
                    # 진행 — 대량 유실 직후 동시 요청 전원의 DB 조회가 회복을
                    # 늦추는 것을 막는다. 정합 자체는 rebuild.lua 의 조건부
                    # 트리거가 보장하므로 이 플래그는 순수 부하 억제다 —
                    # 30s 락처럼 재축출을 막지 않게 짧게(5s) 잡고, 못 얻으면
                    # 센티넬이 남아 다음 요청이 재시도한다(R22 r2 S5).
                    if await redis.set(
                        f"budget:rebuild-lock:{{{user_id}}}", "1",
                        nx=True, ex=5,
                    ):
                        try:
                            from sqlalchemy import func, select, text

                            # 총합 행과 앱별 행을 **한 번에** 읽는다. 총합은 client IS NULL 이고,
                            # 앱별 행은 각자의 카운터 키로 복원해야 한다(아래).
                            result = await db.execute(
                                text(
                                    "SELECT client, used_usd FROM budget.budget_usages "
                                    "WHERE scope = 'USER' AND scope_id = :uid AND period = :period"
                                ),
                                {"uid": user_id, "period": period},
                            )
                            rows = result.all()
                            by_client = {r[0]: r[1] for r in rows}
                            total_row = by_client.get(None)

                            if total_row is None:
                                # budget_usages 에 총합 행이 없으면 usage_logs 에서 SUM.
                                from app.config import get_settings
                                from app.models.usage import UsageRecord

                                stmt2 = select(func.coalesce(func.sum(UsageRecord.cost_usd), 0)).where(
                                    UsageRecord.user_id == user_id,
                                    # period 는 리포팅 타임존 월 — to_char 를 UTC 타임스탬프에
                                    # 바로 걸면 월 경계 몇 시간이 다른 달로 새어 과다/과소 복원된다.
                                    func.to_char(
                                        func.timezone(
                                            get_settings().reporting_timezone,
                                            UsageRecord.requested_at,
                                        ),
                                        "YYYY-MM",
                                    )
                                    == period,
                                )
                                result2 = await db.execute(stmt2)
                                used_from_db = result2.scalar_one()
                            else:
                                used_from_db = total_row

                            # ⚠️ db=0 이라도 항상 rebuild 를 호출한다 — 센티넬을
                            #    해제해야 다음 요청이 DB 조회를 반복하지 않는다
                            #    (무가격 모델 유저가 45 일간 매 요청 SUM 을
                            #    도는 결함, R22 r2 S4). rebuild.lua 는 플로어라
                            #    db=0 이면 보통 no-op 이고 센티넬만 정리된다.
                            applied = await self._rebuild_counter(
                                redis,
                                usage_key=user_key,
                                resvsum_key=(
                                    f"budget:resvsum:user:{{{user_id}}}:{period}"
                                ),
                                idx_key=f"budget:pendingidx:user:{{{user_id}}}",
                                marker_prefix=(
                                    f"budget:pending:user:{{{user_id}}}:"
                                ),
                                field_prefix="",
                                db_base=Decimal(str(used_from_db)),
                            )
                            if applied:
                                logger.info(
                                    "budget_counter_restored",
                                    user_id=user_id,
                                    period=period,
                                    used=str(used_from_db),
                                )

                            # ── 앱별 카운터도 복원한다 ──
                            #
                            # ⚠️ 예전에는 총합 키만 복원했다. 그래서 Redis 데이터 유실/failover
                            #    후 **소진된 앱별 예산이 조용히 0 으로 되돌아갔다** — 그 앱의
                            #    한도가 그 달 내내 사실상 없어진다.
                            #    이미 존재하는 키는 건드리지 않는다(진행 중인 카운트를 덮어쓰면
                            #    그 사이의 사용량을 잃는다). DB 행이 없어도 센티넬이 선
                            #    앱 키는 복원해 센티넬을 해제한다 — PER_APP_BUDGET_CLIENTS
                            #    전수와 by_client 의 합집합을 본다(R22 r2 S4).
                            for client_name in sorted(
                                set(PER_APP_BUDGET_CLIENTS)
                                | {c for c in by_client if c}
                            ):
                                used = by_client.get(client_name) or 0
                                app_key = f"budget:user:{{{user_id}}}:{client_name}:{period}"
                                _ak = await redis.get(app_key)
                                if (
                                    _ak is not None
                                    and float(_ak) >= 0
                                    # :lost 센티넬 — 양수 첫 쓰기로 위장된
                                    # 유실도 복원한다(R22 r1 M1).
                                    and not await redis.exists(f"{app_key}:lost")
                                ):
                                    continue
                                applied = await self._rebuild_counter(
                                    redis,
                                    usage_key=app_key,
                                    resvsum_key=(
                                        f"budget:resvsum:user:{{{user_id}}}:"
                                        f"{client_name}:{period}"
                                    ),
                                    idx_key=(
                                        f"budget:pendingidx:client:{{{user_id}}}"
                                    ),
                                    marker_prefix=(
                                        f"budget:pending:client:{{{user_id}}}:"
                                    ),
                                    field_prefix=f"{client_name}:",
                                    db_base=Decimal(str(used)),
                                )
                                if applied:
                                    logger.info(
                                        "budget_app_counter_restored",
                                        user_id=user_id,
                                        client=client_name,
                                        period=period,
                                        used=str(used),
                                    )
                        except Exception:
                            logger.exception("budget_counter_restore_failed", user_id=user_id)
                            # 실패한 구문은 트랜잭션을 aborted 상태로 남긴다 — 같은
                            # 세션의 뒤 쿼리(팀 복원·eval 사전 조회)가 전부
                            # PendingRollbackError 로 터지지 않게 되돌린다.
                            await _safe_rollback(db)

                # TEAM 사용 카운터도 복원한다 — 위가 USER/앱만 복원하고 팀은 놔두면,
                # 카운터 유실(축출/failover) 뒤 budget_check.lua 가 used=0 을 읽어
                # 팀 하드캡이 그 달 내내 사실상 해제된다(E2E S2-1). 사용자와 같은
                # 규칙: 이미 있는 키는 건드리지 않고, in-flight 예약 마커 합을 얹는다.
                _tk, _tk_lost = await redis.mget([team_key, f"{team_key}:lost"])
                if db is not None and (
                    _tk is None
                    or float(_tk) < 0
                    or _tk_lost is not None
                ):
                    # user 경로와 같은 inflight 플래그 — 정합은 rebuild.lua 의
                    # 조건부 트리거가 보장, 여기선 DB 조회만 억제(5s).
                    if await redis.set(
                        f"budget:rebuild-lock:team:{{{team_id}}}", "1",
                        nx=True, ex=5,
                    ):
                        try:
                            from sqlalchemy import text as _t

                            result = await db.execute(
                                _t(
                                    "SELECT used_usd FROM budget.budget_usages "
                                    "WHERE scope = 'TEAM' AND scope_id = :tid "
                                    "AND period = :period AND client IS NULL"
                                ),
                                {"tid": team_id, "period": period},
                            )
                            team_used = result.scalar_one_or_none()
                            if team_used is None:
                                from sqlalchemy import func as _f, select as _s

                                from app.config import get_settings
                                from app.models.usage import UsageRecord

                                team_used = (
                                    await db.execute(
                                        _s(_f.coalesce(_f.sum(UsageRecord.cost_usd), 0)).where(
                                            UsageRecord.team_id == team_id,
                                            # user 경로와 같다 — 리포팅 타임존 월로 버킷.
                                            _f.to_char(
                                                _f.timezone(
                                                    get_settings().reporting_timezone,
                                                    UsageRecord.requested_at,
                                                ),
                                                "YYYY-MM",
                                            )
                                            == period,
                                        )
                                    )
                                ).scalar_one()
                            # db=0 여도 항상 rebuild — 센티넬 해제가 목적의 일부
                            # (S4: db=0 에서 영구 재트리거 방지).
                            applied = await self._rebuild_counter(
                                redis,
                                usage_key=team_key,
                                resvsum_key=(
                                    f"budget:resvsum:team:{{{team_id}}}:{period}"
                                ),
                                idx_key=f"budget:pendingidx:team:{{{team_id}}}",
                                marker_prefix=(
                                    f"budget:pending:team:{{{team_id}}}:"
                                ),
                                field_prefix="",
                                db_base=Decimal(str(team_used)),
                            )
                            if applied:
                                logger.info(
                                    "budget_team_counter_restored",
                                    team_id=team_id,
                                    period=period,
                                    used=str(team_used),
                                )
                        except Exception:
                            logger.exception("budget_team_counter_restore_failed", team_id=team_id)
                            await _safe_rollback(db)

                # Redis Cluster CROSSSLOT 대응: USER/TEAM 키는 hash tag 가 달라 slot 이
                # 서로 다르므로 단일 Lua 호출에 묶을 수 없다. scope 별로 2번 호출하고
                # Python 에서 결과를 합친다. Lua 내부 체크는 여전히 각 scope 원자적.
                script = LuaScriptLoader.get("budget_check")

                # §6-1 단계 1: TEAM 설정 존재 확인이 USER 평가보다 먼저다.
                # user EVAL 을 먼저 돌리면 D-유저 차단 + T-미설정 조합에서
                # user_budget_exceeded 가 team_budget_unset 을 이겨 DB 폴백과
                # 에러 코드가 갈린다. eval 순서만 바꾸고 block 판정은
                # §6-1 순서(1 unset → 2 user → 3 team)대로 적용한다.
                team_raw = await redis.eval(script, 2, team_key, team_config_key, "team", "")
                team_result = json.loads(team_raw)

                # TEAM config 미설정 → deny (C-1 정책)
                if not team_result.get("config_present"):
                    raise PermissionError("team_budget_unset")

                # D-3: 개인 config 가 없고 팀 기본 cap D 가 있으면, D 를 합성
                # config 로 user EVAL 에 넘긴다 (cap_u = A_u ?? D). 팀 키는 다른
                # slot 이라 Lua 안에서 읽지 못하므로 plain GET 으로 미리 읽어
                # ARGV 로 주입한다 — D 유저의 policy/thresholds 는 팀 것을 따른다.
                user_fallback = ""
                if not user_config_present:
                    team_cfg_raw = await redis.get(team_config_key)
                    if team_cfg_raw:
                        team_cfg = json.loads(
                            team_cfg_raw.decode()
                            if isinstance(team_cfg_raw, bytes)
                            else team_cfg_raw
                        )
                        d_cap = team_cfg.get("default_user_cap_usd")
                        if d_cap is not None:
                            # 캐시된 팀 config 에 thresholds 가 없는 구버전 엔트리 대비
                            # 폴백 — 재수화는 _row_thresholds 가 DB 행에서 쓴다.
                            d_thresholds = (
                                team_cfg.get("thresholds") or list(DEFAULT_THRESHOLDS)
                            )
                            user_fallback = json.dumps({
                                "limit_usd": str(d_cap),
                                "policy": team_cfg.get("policy", "hard_block"),
                                "soft_limit_pct": team_cfg.get("soft_limit_pct")
                                or DEFAULT_SOFT_LIMIT_PCT,
                                "throttle_rpm_pct": team_cfg.get("throttle_rpm_pct")
                                or DEFAULT_THROTTLE_RPM_PCT,
                                "thresholds": d_thresholds,
                            })

                # §6-1 단계 2: USER (A_u 또는 D)
                user_raw = await redis.eval(
                    script, 2, user_key, user_config_key, "user", user_fallback
                )
                user_result = json.loads(user_raw)

                # USER config 미설정(+D 미설정) → pass-through (Q 정책)
                if user_result.get("config_present") and not user_result["allowed"]:
                    raise PermissionError(user_result.get("reason", "user_budget_exceeded"))

                # §6-1 단계 3: TEAM 한도 — unset 은 위에서 이미 걸렀다.
                if not team_result["allowed"]:
                    raise PermissionError(team_result.get("reason", "team_budget_exceeded"))

                # 앱(client) 예산 확인 — 'claude-code' / 'cowork' 이고, USER config 에
                # 이 client 가 app_clients 로 등록된 경우에만 추가 eval (free gate).
                # USER check 결과가 이미 config.app_clients 를 echo 하므로 추가 RTT 없이
                # 게이팅 — 앱 예산 없는 유저는 3번째 eval 자체를 건너뛴다.
                # 불변식(P0-③ review): per-app 예산은 USER 총예산의 하위 서브-리밋이므로
                # 항상 부모 USER 예산이 존재한다(admin-api 가 부모 없는 per-app 생성 거부).
                # → app_clients 게이트는 부모 config 가 있다는 전제에서 신뢰 가능.
                #
                # ⚠️ `app_clients` 는 리스트 또는 **빈 dict** 로 올 수 있다. admin-api 는 이
                #    필드를 Lua 로 병합해 쓰는데(app_clients 클로버를 막기 위해),
                #    Redis 의 cjson 에는 빈 배열 개념이 없어 `[]` 가 `{}` 로 인코딩된다
                #    (실측: redis 7.4, `cjson.empty_array` 미지원). 둘 다 "활성 per-app
                #    예산이 없다" 는 같은 뜻이므로 같게 다뤄야 한다.
                #
                #    `isinstance(list)` 만 보면 빈 dict 가 "타입이 틀렸다" 로 떨어져,
                #    아래 per-app 분기 전체(콜드 캐시 재수화 안전망 포함)를 건너뛴다.
                #    지금은 결과가 같지만(둘 다 per-app 예산 0건), 그 동등성이 우연이라
                #    미래의 독자가 `{}` 를 "알 수 없음" 으로 오독할 수 있다. 여기서 한 번
                #    정규화해 그 여지를 없앤다.
                user_app_clients = _as_client_list(user_result.get("app_clients"))
                client_result = None
                # 개인 config 가 없는 유저(D 합성 포함)는 app_clients 게이트를
                # 신뢰할 수 없다 — 불변식(P0-③) 이전의 고아 per-app 행이 남아
                # 있으면 DB 폴백은 걸고 Redis 경로는 건너뛰어 판정이 갈린다.
                # 이 경우엔 per-app 키를 직접 eval 해 두 경로를 일치시킨다.
                if client in PER_APP_BUDGET_CLIENTS and (
                    client in user_app_clients or not user_config_present
                ):
                    client_key = f"budget:user:{{{user_id}}}:{client}:{period}"
                    client_config_key = f"budget:config:user:{{{user_id}}}:{client}"
                    # P0-③: per-app config cold-cache fallback. If admin's
                    # best-effort SET of this key was lost, the Lua would see
                    # config_present=false → pass-through (bypass). Rehydrate from
                    # DB on miss so the per-app limit stays enforced.
                    if db is not None and not await redis.exists(client_config_key):
                        await self._hydrate_client_config_cache(
                            redis, db, user_id, client
                        )
                    client_raw = await redis.eval(
                        script, 2, client_key, client_config_key, "client", ""
                    )
                    client_result = json.loads(client_raw)
                    if client_result.get("config_present") and not client_result["allowed"]:
                        raise PermissionError(
                            client_result.get("reason", "client_budget_exceeded")
                        )

                # §6-3/§6-5: 평가된 계층 전부를 조합 — remaining 은 "차단까지의
                # 잔여" 최솟값, 경고·throttle 은 계층별로 모은다.
                tiers: list[tuple[str, dict]] = []
                if user_result.get("config_present"):
                    tiers.append(("user", user_result))
                tiers.append(("team", team_result))
                if client_result is not None and client_result.get("config_present"):
                    tiers.append(("client", client_result))
                return _build_status(tiers, team_result)
            except PermissionError:
                raise
            except Exception:
                logger.exception("redis_budget_check_failed", user_id=user_id)
                # Redis 경로 안에서 실패한 DB 구문(hydrate/복원)이 세션을
                # aborted 상태로 남겼을 수 있다 — 같은 세션으로 가는
                # DB fallback 이 즉시 PendingRollbackError 를 맞지 않게 되돌린다.
                await _safe_rollback(db)
                # Redis 실패 시 DB fallback

        # DB fallback (REDIS_DEGRADED)
        try:
            return await self._check_budget_db(db, user_id, team_id, period, client=client)
        except PermissionError:
            raise
        except Exception as e:
            # 인프라 장애만 503 — 코드 버그(NULL 필드 TypeError 등)는 그대로
            # 전파해 500 으로 남겨 재시도 가능한 장애처럼 보이지 않게 한다.
            if not isinstance(e, (SQLAlchemyError, OSError, asyncio.TimeoutError)):
                raise
            logger.exception("budget_db_fallback_failed", user_id=user_id)
            raise BudgetBackendUnavailableError("budget check failed: DB fallback error")

    async def reserve_budget(
        self,
        redis,
        user_id: str,
        team_id: str | None,
        period: str,
        estimate: Decimal,
        request_id: str,
        client: str | None = None,
        marker_ttl: int = 14400,
    ) -> list[dict]:
        """Admission-time 최악비용 예약 (check-then-act 창 제거).

        budget_check 가 읽기 전용이라, 동시 요청들이 같은 used 를 읽고 전부 통과한 뒤
        각자 실비를 차감해 월 하드캡을 넘길 수 있었다. 이 메서드는 사용 카운터에
        추정 최악비용을 **원자적으로 선차감**하고 per-request 마커를 남긴다 —
        이후 요청의 check/reserve 는 in-flight 예약을 포함한 used 를 본다.
        완료 시 ``settle_budget`` 이 (실비 - 예약) 델타로 정산한다.

        반환: 커밋된 scope 디스크립터 목록 [{scope, usage_key, config_key,
        marker_key, fallback}] — settle/refund 에 그대로 넘긴다.
        거절 시 이미 커밋된 앞선 scope 를 되돌리고 PermissionError 를 던진다 —
        reason 은 middleware 의 429 코드와 같은 어휘다.

        ⚠️ config 해석은 ``check_budget`` 의 Redis 경로와 동일 규칙을 따른다
           (team 미설정 → deny, user 미설정+D 없음 → pass, D 합성 config 폴백).
           config 캐시는 직전 check_budget 이 수화해 뒀으므로 여기서는 재수화
           하지 않는다 — 키가 증발한 엣지는 fail-closed 방향으로만 어긋난다.
        """
        if not team_id:
            raise PermissionError("no_team_assigned")

        script = LuaScriptLoader.get("budget_reserve")
        est_str = str(estimate)

        user_usage_key = f"budget:user:{{{user_id}}}:{period}"
        user_config_key = f"budget:config:user:{{{user_id}}}"
        team_usage_key = f"budget:team:{{{team_id}}}:{period}"
        team_config_key = f"budget:config:team:{{{team_id}}}"

        # D-3 와 동일: 개인 config 없음 + 팀 기본 cap D → 합성 config 를 ARGV 로.
        user_fallback = ""
        if not await redis.exists(user_config_key):
            team_cfg_raw = await redis.get(team_config_key)
            if team_cfg_raw:
                team_cfg = json.loads(
                    team_cfg_raw.decode()
                    if isinstance(team_cfg_raw, bytes)
                    else team_cfg_raw
                )
                d_cap = team_cfg.get("default_user_cap_usd")
                if d_cap is not None:
                    # check_budget 의 D-3 합성과 같은 규칙 — 캐시된 팀 config 에
                    # thresholds 가 없는 구버전 엔트리 대비 폴백(변수 추출은
                    # 재수화 기본값 주입 가드 테스트의 AST 패턴과 맞추기 위함).
                    d_thresholds = (
                        team_cfg.get("thresholds") or list(DEFAULT_THRESHOLDS)
                    )
                    user_fallback = json.dumps({
                        "limit_usd": str(d_cap),
                        "policy": team_cfg.get("policy", "hard_block"),
                        "soft_limit_pct": team_cfg.get("soft_limit_pct")
                        or DEFAULT_SOFT_LIMIT_PCT,
                        "throttle_rpm_pct": team_cfg.get("throttle_rpm_pct")
                        or DEFAULT_THROTTLE_RPM_PCT,
                        "thresholds": d_thresholds,
                    })

        # 평가 순서는 §6-1 과 같다: 1) team 2) user 3) client.
        # resvsum 키는 usage 키와 같은 hash tag — 미정산 예약 합계를 따로 유지해
        # 표시 경로(/v1/usage/me)가 "확약액"이 아닌 실지출을 보여주게 한다.
        # pendingidx 인덱스는 마커와 같은 hash tag 를 쓴다 — Cluster 에서
        # 같은 슬롯이라 reserve/settle eval 안에서 원자적으로 갱신된다.
        # rebuild 의 pending 합산은 이 해시(HKEYS+필드별 GET)로 끝내 키스페이스
        # SCAN 을 돌지 않는다(R22 r1 S1).
        scope_specs: list[tuple[str, str, str, str, str, str, str]] = [
            ("team", team_usage_key, team_config_key,
             f"budget:pending:team:{{{team_id}}}:{request_id}",
             f"budget:resvsum:team:{{{team_id}}}:{period}",
             f"budget:pendingidx:team:{{{team_id}}}", request_id),
            ("user", user_usage_key, user_config_key,
             f"budget:pending:user:{{{user_id}}}:{request_id}",
             f"budget:resvsum:user:{{{user_id}}}:{period}",
             f"budget:pendingidx:user:{{{user_id}}}", request_id),
        ]
        if client in PER_APP_BUDGET_CLIENTS:
            scope_specs.append((
                "client",
                f"budget:user:{{{user_id}}}:{client}:{period}",
                f"budget:config:user:{{{user_id}}}:{client}",
                f"budget:pending:client:{{{user_id}}}:{client}:{request_id}",
                f"budget:resvsum:user:{{{user_id}}}:{client}:{period}",
                f"budget:pendingidx:client:{{{user_id}}}",
                f"{client}:{request_id}",
            ))

        committed: list[dict] = []
        for scope, usage_key, config_key, marker_key, resvsum_key, idx_key, idx_field in scope_specs:
            fallback = user_fallback if scope == "user" else ""
            try:
                raw = await redis.eval(
                    script, 5,
                    usage_key, config_key, marker_key, resvsum_key, idx_key,
                    scope, est_str, fallback, marker_ttl, idx_field,
                )
                result = json.loads(raw)
            except Exception:
                # Redis 일시 장애 — fail-open 방향으로 둔다. 예약 없이 진행하면
                # 기존(체크만 하는) 동작과 같아지므로 안전하다. 커밋분은 되돌린다.
                logger.exception("budget_reserve_eval_failed", scope=scope)
                # ⚠️ 클라이언트 타임아웃은 서버에서 eval 이 **실행 완료**됐을 수
                #    있고, eval 성공 뒤 json.loads 가 실패했을 수도 있다 — 그때 이
                #    scope 의 마커는 실존하는데 committed 에 없으면 영구 고아가
                #    된다. 방금 시도한 scope 도 환불 대상에 포함시킨다 —
                #    마커가 없으면 settle(0) 은 no-op 이라 안전하다.
                committed.append({
                    "scope": scope,
                    "usage_key": usage_key,
                    "config_key": config_key,
                    "marker_key": marker_key,
                    "resvsum_key": resvsum_key,
                    "idx_key": idx_key,
                    "idx_field": idx_field,
                    "fallback": fallback,
                    "reserved": est_str,
                    "marker_ttl": marker_ttl,
                })
                await self._refund_reservations(redis, committed)
                # rl_fail_mode=closed 정책을 존중한다 — 다른 한도 경로(RPM/TPM/
                # 비용)는 eval 실패를 차단으로 번역하는데 예약만 항상 fail-open
                # 하면 모드 설정이 예산 예약에만 무의미해진다.
                from app.config import get_settings as _gs

                if _gs().rl_fail_mode.strip().lower() == "closed":
                    raise PermissionError("budget_backend_unavailable")
                return []

            if result.get("config_present") is False:
                if scope == "team":
                    # C-1: 팀 예산 미설정은 deny (check_budget 과 동일).
                    await self._refund_reservations(redis, committed)
                    raise PermissionError("team_budget_unset")
                # user(Q)/client 미설정 → 예약 대상 자체가 없으므로 pass.
                continue
            if "config_present" not in result:
                # 필드 자체가 없는 응답 = 구버전/다른 스크립트의 응답 형태 —
                # 예약 계약을 알 수 없으니 이 scope 는 예약 없이 통과시킨다
                # (기존 체크 전용 동작으로 회귀, 차단 방향으로 어긋나지 않게).
                logger.warning("budget_reserve_malformed_response", scope=scope)
                continue

            if not result.get("allowed"):
                await self._refund_reservations(redis, committed)
                raise PermissionError(
                    result.get("reason") or f"{scope}_budget_exceeded"
                )

            if result.get("reserved"):
                # "reserved" — settle 의 마커 만료 복구 경로(ARGV[4])에 넘기는
                # 예약 당시 추정치. 마커가 살아있으면 마커 값이 우선이라 이 필드는
                # 무시된다.
                committed.append({
                    "scope": scope,
                    "usage_key": usage_key,
                    "config_key": config_key,
                    "marker_key": marker_key,
                    "resvsum_key": resvsum_key,
                    "idx_key": idx_key,
                    "idx_field": idx_field,
                    "fallback": fallback,
                    "reserved": est_str,
                    # settle 의 done 툼스톤 TTL(ARGV[5]) — 마커 수명과 같게 둬야
                    # 툼스톤 소멸 뒤 늦은 중복 settle 이 실비를 재적용하지 않는다
                    # (R23 r3 N-4).
                    "marker_ttl": marker_ttl,
                })

        return committed

    async def settle_budget(
        self,
        redis,
        scopes: list[dict],
        actual: Decimal,
    ) -> tuple[Decimal | None, str | None]:
        """예약된 월예산을 실비로 정산한다 (actual=0 이면 전액 환불).

        마커 기반 멱등 — 같은 요청에 여러 번 불려도 한 번만 적용된다.
        (threshold_triggered, scope) 를 돌려 호출자가 알림을 재사용하게 한다.
        """
        script = LuaScriptLoader.get("budget_settle")
        triggered = None
        triggered_scope = None
        for s in scopes:
            try:
                # 롤링 배포 중 구버전 파드가 만든 스펙에는 idx 키가 없다 —
                # settle_eval_args 가 4키 호출로 미뤄 settle 은 그대로 동작하고
                # 인덱스만 건너뛴다(인덱스 없는 마커는 rebuild SCAN 폴백이 잡는다).
                raw = await redis.eval(
                    script, *settle_eval_args(s),
                    str(actual), s.get("fallback") or "",
                    s.get("idx_field") or "",
                    s.get("reserved") or "",
                    str(s.get("marker_ttl") or ""),
                )
                result = json.loads(raw)
            except Exception:
                # 정산 실패 = 카운터에 예약치 잔류(과대 계상, 보수적 방향).
                # 마커도 남아 재시도 수단은 있으나, 현재는 경고만 남긴다.
                logger.exception("budget_settle_failed", scope=s.get("scope"))
                continue
            if result.get("settled") is False:
                # 마커 부재 + done 툼스톤 존재(이미 정산) = 정상 멱등 경로.
                # 툼스톤도 없고 예약치도 미상(구버전 스펙)이면 카운터에 예약치가
                # 남은 채 no-op — 진단 신호로 남긴다(R23 S5).
                logger.info(
                    "budget_settle_noop",
                    scope=s.get("scope"),
                    request_id=(s.get("marker_key") or "").rsplit(":", 1)[-1],
                )
            elif result.get("recovered"):
                logger.warning(
                    "budget_settle_recovered",
                    scope=s.get("scope"),
                    request_id=(s.get("marker_key") or "").rsplit(":", 1)[-1],
                )
            t = result.get("threshold_triggered")
            if triggered is None and t is not None:
                triggered = t
                triggered_scope = s.get("scope")
        return triggered, triggered_scope

    async def _refund_reservations(self, redis, scopes: list[dict]) -> None:
        """거절/에러 경로의 예약 롤백 — settle_budget(0) 과 동일 효과."""
        if not scopes:
            return
        try:
            await self.settle_budget(redis, scopes, Decimal("0"))
        except Exception:
            logger.warning("budget_reservation_refund_failed")

    async def _check_budget_db(
        self,
        db: AsyncSession,
        user_id: str,
        team_id: str,
        period: str,
        client: str | None = None,
    ) -> BudgetStatus:
        """DB SELECT 기반 예산 확인 (Redis fallback 경로).

        DB 컬럼: scope / scope_id / max_budget_usd (KI-09 수정 반영).
        soft_limit_pct, throttle_rpm_pct 는 현재 DB 스키마에 없으므로 Python 기본값 사용.
        thresholds 는 migration 0041 이후 DB 컬럼(``alert_thresholds``)이 원천이다.
        client 가 설정된 경우 앱별 BudgetConfig/BudgetUsage 도 확인한다.
        앱 예산 미설정(config=None) → pass-through.
        """
        if db is None:
            # DB_DEGRADED — Redis 경로가 실패해 이 fallback 까지 왔는데 DB 도 없다.
            # 'no_budget_assigned' 는 예산 미설정이라는 뜻이라 인프라 장애를 오보한다.
            raise BudgetBackendUnavailableError("budget check unavailable: DB degraded")

        # Q 정책: USER 예산 미설정 → pass-through (차단 안 함)
        # client IS NULL 로 한정 — per-app(client) row 가 생기면 (scope,scope_id) 당
        # 행이 2개 이상이 되어 scalar_one_or_none() 가 MultipleResultsFound 로 터진다.
        # (REDIS_DEGRADED 시 app 예산 유저가 전부 차단되는 잠복 장애 방지.)
        user_cfg_result = await db.execute(
            select(BudgetConfig)
            .where(BudgetConfig.scope == BudgetScope.USER)
            .where(BudgetConfig.scope_id == user_id)
            .where(BudgetConfig.client.is_(None))
            .where(BudgetConfig.is_active == True)  # noqa: E712
        )
        user_config = user_cfg_result.scalar_one_or_none()

        def _layer(
            used: Decimal,
            limit: Decimal,
            policy: BudgetPolicy,
            thresholds: list[int],
        ) -> tuple[str | None, dict]:
            """한 계층을 평가해 (block_reason, normalized dict) 를 돌려준다.

            dict 는 budget_check.lua 반환 JSON 과 같은 shape — _build_status 가
            Redis 경로와 이 경로를 같은 코드로 조합할 수 있게 한다. ``thresholds``
            는 호출자가 해당 BudgetConfig 행의 ``_row_thresholds`` 값을 넘긴다 —
            기본값을 여기서 구우면 degrade 동안만 알림 기준이 달라진다(0037).
            """
            block, sw, ta = _evaluate_layer(used, limit, policy)
            return block, {
                "limit_usd": limit,
                "used_usd": used,
                "policy": policy.value,
                "soft_limit_pct": DEFAULT_SOFT_LIMIT_PCT,
                "throttle_rpm_pct": DEFAULT_THROTTLE_RPM_PCT,
                "threshold_pct": int(used / limit * 100) if limit > 0 else 0,
                "thresholds": thresholds,
                "soft_warning": sw,
                "throttle_active": ta,
                "config_present": True,
            }

        async def _user_used() -> Decimal:
            r = await db.execute(
                select(BudgetUsage)
                .where(BudgetUsage.scope == BudgetScope.USER)
                .where(BudgetUsage.scope_id == user_id)
                .where(BudgetUsage.client.is_(None))
                .where(BudgetUsage.period == period)
            )
            u = r.scalar_one_or_none()
            return u.used_usd if u else Decimal("0")

        tiers: list[tuple[str, dict]] = []

        # C-1 정책 + §6-1 단계 1: TEAM 설정 존재 확인이 USER 평가보다 먼저다.
        # 개인 cap 평가를 먼저 하면 T-미설정 팀의 차단된 유저가 user_* 코드를
        # 받아 Redis 경로와 에러 코드가 갈린다.
        team_cfg_result = await db.execute(
            select(BudgetConfig)
            .where(BudgetConfig.scope == BudgetScope.TEAM)
            .where(BudgetConfig.scope_id == team_id)
            .where(BudgetConfig.client.is_(None))
            .where(BudgetConfig.is_active == True)  # noqa: E712
        )
        team_config = team_cfg_result.scalar_one_or_none()
        # T=NULL 'D-only' 행은 행 없음과 동일 — fail-closed(§3-1).
        if team_config is None or team_config.max_budget_usd is None:
            raise PermissionError("team_budget_unset")

        policy = _db_policy_to_domain(team_config.policy)

        # §6-1 단계 2: USER — 명시적 A_u, 없으면 팀 기본 cap D (cap_u = A_u ?? D)
        if user_config is not None:
            # 정책을 적용한다 — 무조건 차단은 Redis 경로와 어긋난다(_evaluate_layer 주석).
            user_block, d = _layer(
                await _user_used(),
                user_config.max_budget_usd,
                _db_policy_to_domain(user_config.policy),
                _row_thresholds(user_config),
            )
            if user_block:
                raise PermissionError(f"user_{user_block}")
            tiers.append(("user", d))
        else:
            cap_d = team_config.default_user_cap_usd
            if cap_d is not None:
                # D 유저의 thresholds 는 팀 것을 따른다(Redis 경로의 D 합성과 동일).
                user_block, d = _layer(
                    await _user_used(), cap_d, policy, _row_thresholds(team_config)
                )
                if user_block:
                    raise PermissionError(f"user_{user_block}")
                tiers.append(("user", d))

        team_usage_result = await db.execute(
            select(BudgetUsage)
            .where(BudgetUsage.scope == BudgetScope.TEAM)
            .where(BudgetUsage.scope_id == team_id)
            .where(BudgetUsage.period == period)
            .where(BudgetUsage.client.is_(None))
        )
        team_usage = team_usage_result.scalar_one_or_none()
        team_used = team_usage.used_usd if team_usage else Decimal("0")

        max_budget = team_config.max_budget_usd
        # Redis degrade 중에도 임계값은 DB 행의 값이어야 한다 — 기본값을 구우면
        # degrade 동안만 알림 기준이 달라진다(migration 0041: alert_thresholds).
        team_block, team_dict = _layer(
            team_used, max_budget, policy, _row_thresholds(team_config)
        )
        if team_block:
            raise PermissionError(f"team_{team_block}")
        tiers.append(("team", team_dict))

        # 앱(client) 예산 확인 (REDIS_DEGRADED 경로) — 미설정 시 pass-through.
        if client in PER_APP_BUDGET_CLIENTS:
            client_cfg_result = await db.execute(
                select(BudgetConfig)
                .where(BudgetConfig.scope == BudgetScope.USER)
                .where(BudgetConfig.scope_id == user_id)
                .where(BudgetConfig.client == client)
                .where(BudgetConfig.is_active == True)  # noqa: E712
            )
            client_config = client_cfg_result.scalar_one_or_none()
            if client_config is not None:
                client_usage_result = await db.execute(
                    select(BudgetUsage)
                    .where(BudgetUsage.scope == BudgetScope.USER)
                    .where(BudgetUsage.scope_id == user_id)
                    .where(BudgetUsage.client == client)
                    .where(BudgetUsage.period == period)
                )
                client_usage = client_usage_result.scalar_one_or_none()
                client_used = client_usage.used_usd if client_usage else Decimal("0")
                client_block, client_dict = _layer(
                    client_used,
                    client_config.max_budget_usd,
                    _db_policy_to_domain(client_config.policy),
                    _row_thresholds(client_config),
                )
                if client_block:
                    raise PermissionError(f"client_{client_block}")
                tiers.append(("client", client_dict))

        return _build_status(tiers, team_dict)

    async def ensure_config_cached(
        self,
        redis,
        db: AsyncSession,
        user_id: str,
    ) -> None:
        """예산 설정이 Redis에 없으면 DB에서 조회하여 캐시.

        Lua 스크립트는 lowercase policy 값('hard_block' 등)을 기대하므로
        DB UPPERCASE enum을 변환해서 저장. soft/throttle 파라미터는 DB 스키마에 없어
        Python 기본값을 쓰지만, thresholds 는 migration 0041 이후 DB 컬럼
        (``alert_thresholds``)이 원천이다 — 예전에 여기서 기본값을 쓴 것이 운영자 설정을
        캐시 TTL(300초)마다 되돌린 원인이었다.
        """
        config_key = f"budget:config:user:{{{user_id}}}"
        if redis is None:
            return
        cached = await redis.get(config_key)
        if cached:
            return

        # client IS NULL = USER total config (per-app rows는 별도 키로 캐시됨).
        # 필터 없으면 app 예산 유저에서 MultipleResultsFound.
        result = await db.execute(
            select(BudgetConfig)
            .where(BudgetConfig.scope == BudgetScope.USER)
            .where(BudgetConfig.scope_id == user_id)
            .where(BudgetConfig.client.is_(None))
            .where(BudgetConfig.is_active == True)  # noqa: E712
        )
        config = result.scalar_one_or_none()
        if config:
            # app_clients 는 DB의 활성 per-app budget row에서 재구성 — 이 캐시 재생성이
            # admin-api가 user-config JSON에 써둔 app_clients 를 덮어쓰지 않도록(free gate 보존).
            app_rows = await db.execute(
                select(BudgetConfig.client)
                .where(BudgetConfig.scope == BudgetScope.USER)
                .where(BudgetConfig.scope_id == user_id)
                .where(BudgetConfig.client.isnot(None))
                .where(BudgetConfig.is_active == True)  # noqa: E712
            )
            app_clients = [c for c in app_rows.scalars().all() if c]
            config_data = {
                "limit_usd": str(config.max_budget_usd),
                "policy": _policy_to_lua_value(_db_policy_to_domain(config.policy)),
                "soft_limit_pct": DEFAULT_SOFT_LIMIT_PCT,
                "throttle_rpm_pct": DEFAULT_THROTTLE_RPM_PCT,
                # ⚠️ 여기가 운영자 설정이 되돌아간 지점이다. 이 재수화는 Redis 설정 키가
                #    만료(ex=300)될 때마다 돌고, 예전에는 DB 에 저장된 값이 없어서
                #    DEFAULT_THRESHOLDS 를 써 넣었다 — admin-api 가 방금 써 둔 운영자
                #    임계값을 5분마다 조용히 덮었다. migration 0041 이후 DB 가 원천이다.
                "thresholds": _row_thresholds(config),
                "app_clients": app_clients,
            }
            # TTL 300s to match team/client hydrate + admin warmers. Without it a
            # lost invalidation could leave this parent config stale-forever
            # (P0-③ review fix). admin DEL is the durable path; TTL is the backstop.
            await redis.set(config_key, json.dumps(config_data), ex=300)

    async def _hydrate_team_config_cache(
        self,
        redis,
        db: AsyncSession,
        team_id: str,
    ) -> None:
        """TEAM budget config cache miss 시 DB에서 조회해 Redis에 SET (best-effort).

        admin-api startup warmup 의 안전망:
        - init SQL seed 예산 또는 post-startup 신규 팀 등 admin-api warmup 이후에
          생성된 TEAM 예산이 아직 Redis에 없는 경우를 처리.
        - 키가 없으면 do-nothing → Lua 가 team_budget_unset 을 정상 반환.

        ⚠️ 예외는 삼키지 않고 전파한다 — '조회 실패' 와 '행 없음' 을 구분해야 한다.
           DB 오류를 삼키면 키 부재로 보여 Lua 가 team_budget_unset(429) 를 내고,
           인프라 장애가 예산 미설정으로 오보된다. 전파되면 check_budget 의
           바깥 try 가 DB fallback 으로 보낸다.
        """
        result = await db.execute(
            select(BudgetConfig)
            .where(BudgetConfig.scope == BudgetScope.TEAM)
            .where(BudgetConfig.scope_id == team_id)
            .where(BudgetConfig.is_active == True)  # noqa: E712
            .limit(1)
        )
        config = result.scalar_one_or_none()
        if config is None or config.max_budget_usd is None:
            # T=NULL 'D-only' 행 — 키 부재로 두어 Lua 가 team_budget_unset
            # 으로 fail-closed 판정하게 한다(§3-1: 행 없음과 동일 취급).
            return

        config_data = {
            "limit_usd": str(config.max_budget_usd),
            "policy": _policy_to_lua_value(_db_policy_to_domain(config.policy)),
            "thresholds": _row_thresholds(config),
            # 팀 기본 유저 cap D — admin DEL-only 경로의 재수화가 D 를 잃지
            # 않도록 DB 행에서 함께 싣는다(§6-6). 현행 Lua 는 미지 필드를 무시.
            "default_user_cap_usd": (
                str(config.default_user_cap_usd)
                if config.default_user_cap_usd is not None
                else None
            ),
        }
        config_key = f"budget:config:team:{{{team_id}}}"
        # TTL은 admin-api BUDGET_CONFIG_CACHE_TTL 과 일치 (5분)
        await redis.set(config_key, json.dumps(config_data), ex=300)
        logger.info(
            "team_budget_config_hydrated",
            team_id=team_id,
            limit_usd=str(config.max_budget_usd),
        )

    async def _hydrate_client_config_cache(
        self,
        redis,
        db: AsyncSession,
        user_id: str,
        client: str,
    ) -> None:
        """Per-app (user, client) budget config cache miss 시 DB에서 조회해 SET.

        P0-③ 안전망: admin 의 best-effort SET 이 유실되어 per-app config 키가
        없을 때, gateway 가 DB 의 활성 per-app BudgetConfig 행으로 키를 재생성해
        per-app 한도가 우회되지 않도록 한다. 키 shape 은 admin
        `_sync_redis_app_config` 와 동일(limit_usd, lowercase policy, thresholds).
        키가 없으면(활성 행 없음) do-nothing → Lua 가 config_present=false 로
        pass-through (정상: 해당 client 에 per-app 예산이 없는 경우).

        ⚠️ 예외는 삼키지 않고 전파한다 — 조회 실패를 '행 없음' 으로 읽으면
           per-app 예산이 조용히 우회된다(P0-③ 가 막으려던 바로 그 구멍).
           전파되면 check_budget 의 바깥 try 가 DB fallback 으로 보낸다.
        """
        result = await db.execute(
            select(BudgetConfig)
            .where(BudgetConfig.scope == BudgetScope.USER)
            .where(BudgetConfig.scope_id == user_id)
            .where(BudgetConfig.client == client)
            .where(BudgetConfig.is_active == True)  # noqa: E712
            .limit(1)
        )
        config = result.scalar_one_or_none()
        if config is None:
            return

        config_data = {
            "limit_usd": str(config.max_budget_usd),
            "policy": _policy_to_lua_value(_db_policy_to_domain(config.policy)),
            "thresholds": _row_thresholds(config),
        }
        config_key = f"budget:config:user:{{{user_id}}}:{client}"
        await redis.set(config_key, json.dumps(config_data), ex=300)
        logger.info(
            "client_budget_config_hydrated",
            user_id=user_id,
            client=client,
            limit_usd=str(config.max_budget_usd),
        )
