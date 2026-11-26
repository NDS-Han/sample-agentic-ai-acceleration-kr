# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Rate Limit Config 로더 (FR-4.1 Step 5).

`model.rate_limit_configs` 테이블을 Redis-first + DB fallback 으로 조회해
스코프별 RPM/TPM 한도를 반환. Admin 변경 시 Redis 키 DEL로 즉시 반영.

조회 우선순위 (스코프 × 2 세분성):
    1. (scope, scope_id, model_alias=X)  — 모델별 한도
    2. (scope, scope_id, model_alias=NULL) — 전체 모델 합산 한도

1이 존재하면 그 값을 사용, 없으면 2 fallback. GLOBAL 스코프는 항상 1만 사용.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model import RateLimitConfig

logger = structlog.get_logger(__name__)

# Redis 캐시 TTL — Admin이 변경 시 DEL로 즉시 무효화하지만 fallback으로 짧은 TTL도 유지
_CONFIG_CACHE_TTL_SEC = 300


@dataclass
class ScopeLimits:
    """한 스코프(USER/TEAM/GLOBAL)의 RPM/TPM/CPM/CPH 한도.

    ``None`` = 한도 미설정(unlimited). 해당 metric 체크 스킵.
    CPM/CPH는 USER/TEAM 스코프만 유효 (GLOBAL은 월 예산 엔진이 담당).

    ``model_agnostic``: 한도 행이 ``model_alias=NULL``(전체 모델 합산)에서
    왔으면 True — 카운터 키를 요청 모델이 아니라 ``*`` 로 만들어야 한다.
    그렇지 않으면 NULL 한도가 "모델별 한도"로 오적용돼, 사용자가 N개 모델에
    걸치면 N배의 실효 한도를 얻는다(rate_limit_scope.build_rl_key 의 `*` 계약).
    """

    rpm: int | None = None
    tpm: int | None = None
    cpm: Decimal | None = None
    cph: Decimal | None = None
    model_agnostic: bool = False


@dataclass
class AllScopeLimits:
    """한 요청에 적용되는 3-스코프 한도 번들."""

    user: ScopeLimits = field(default_factory=ScopeLimits)
    team: ScopeLimits = field(default_factory=ScopeLimits)
    global_: ScopeLimits = field(default_factory=ScopeLimits)


def _cache_key(scope: str, scope_id: str | None, model_alias: str) -> str:
    sid = scope_id if scope_id is not None else "NULL"
    return f"rl:config:{scope}:{sid}:{model_alias}"


def _parse_cached(raw, scope: str, scope_id: str | None) -> ScopeLimits | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        # rpm/tpm 은 bool 이 아닌 int 여야 한다 — 'abc' 는 소비부
        # `'abc' > 0` TypeError → 매 요청 500(TTL 300s), true 는
        # Lua tonumber('True')→nil 로 한도가 조용히 스킵돼 무제한
        # fail-open(17차 리뷰 F7). 오염 항목은 캐시미스 → DB 폴백.
        rpm, tpm = data.get("rpm"), data.get("tpm")
        for v in (rpm, tpm):
            if v is not None and (
                isinstance(v, bool) or not isinstance(v, int) or v < 0
            ):
                logger.warning(
                    "rl_config_cache_type_corrupt", scope=scope, scope_id=scope_id
                )
                return None
        cpm_raw = data.get("cpm")
        cph_raw = data.get("cph")
        return ScopeLimits(
            rpm=rpm,
            tpm=tpm,
            cpm=Decimal(str(cpm_raw)) if cpm_raw is not None else None,
            cph=Decimal(str(cph_raw)) if cph_raw is not None else None,
            model_agnostic=bool(data.get("agg")),
        )
    except Exception:
        logger.warning("rl_config_cache_read_failed", scope=scope, scope_id=scope_id)
        return None


def _guard_enabled() -> bool:
    from app.config import get_settings

    return bool(get_settings().rl_breaker_enabled)


async def _load_scopes_from_cache(
    redis, specs: list[tuple[str, str | None, str]], model_alias: str
) -> list[ScopeLimits | None]:
    """스코프별 캐시 GET 을 **동시에** 보낸다 — 순차 3 왕복 → 1 왕복(p1-hotpath-redis).

    스코프 키는 hash tag 가 없어 cluster 에서 슬롯이 갈리므로 MGET 대신 GET 을 gather 한다.
    공용 Redis 브레이커가 열려 있으면 읽지 않고(전부 미스 → DB), 타임아웃/연결 오류는 브레이커를
    연다. 실패한 스코프는 미스로 처리(예전과 같다).
    """
    misses: list[ScopeLimits | None] = [None] * len(specs)
    if redis is None:
        return misses
    from app.services.hot_path import REDIS_BREAKER, is_redis_infra_error, run_redis

    async def _all():
        return await asyncio.gather(
            *(redis.get(_cache_key(sc, sid, model_alias)) for sc, sid, _attr in specs),
            return_exceptions=True,
        )

    try:
        if _guard_enabled():
            from app.config import get_settings

            raws = await run_redis(
                _all, timeout=get_settings().hot_redis_timeout_sec, what="rl_config"
            )
        else:
            raws = await _all()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("rl_config_cache_read_failed", error=type(e).__name__)
        return misses
    out: list[ScopeLimits | None] = []
    for (sc, sid, _attr), raw in zip(specs, raws):
        if isinstance(raw, BaseException):
            if isinstance(raw, asyncio.CancelledError):
                raise raw
            if _guard_enabled() and is_redis_infra_error(raw):
                REDIS_BREAKER.trip(f"rl_config:{type(raw).__name__}")
            logger.warning("rl_config_cache_read_failed", scope=sc, scope_id=sid)
            out.append(None)
            continue
        out.append(_parse_cached(raw, sc, sid))
    return out


# ── 마지막으로 알던 한도 (프로세스 로컬, p1-hotpath-redis) ─────────────────────────
# Redis 가 답하지 못할 때 middleware/rate_limit.py 의 메모리 fallback 이 스코프의 실제 한도를
# 쓰게 한다 — 예산 열화 모드(wave 1c)가 마지막으로 알던 정책을 쓰는 것과 같은 규칙.
# budget_stale_max_seconds 까지 유효. 요청 경로의 판정은 여전히 Redis/DB 값이 먼저다.
_LAST_KNOWN = None


def _last_known_cache():
    global _LAST_KNOWN
    if _LAST_KNOWN is None:
        from app.config import get_settings
        from app.services.hot_path import LocalCache

        s = get_settings()
        _LAST_KNOWN = LocalCache(
            ttl=s.budget_stale_max_seconds,
            max_stale=s.budget_stale_max_seconds,
            max_entries=s.budget_local_cache_max_entries,
        )
    return _LAST_KNOWN


def remember_limits(scope: str, scope_id: str | None, model_alias: str,
                    limits: ScopeLimits) -> None:
    _last_known_cache().put((scope, scope_id, model_alias), limits)


def last_known_limits(scope: str, scope_id: str | None, model_alias: str) -> ScopeLimits | None:
    """마지막으로 Redis/DB 에서 읽은 이 스코프의 한도(없으면 None = 모름)."""
    return _last_known_cache().get_stale((scope, scope_id, model_alias))


def reset_last_known_for_test() -> None:
    global _LAST_KNOWN
    _LAST_KNOWN = None


async def _save_scope_to_cache(
    redis, scope: str, scope_id: str | None, model_alias: str, limits: ScopeLimits
) -> None:
    if redis is None:
        return
    payload = json.dumps(
        {
            "rpm": limits.rpm,
            "tpm": limits.tpm,
            "cpm": str(limits.cpm) if limits.cpm is not None else None,
            "cph": str(limits.cph) if limits.cph is not None else None,
            "agg": limits.model_agnostic,
        }
    )

    def _set():
        return redis.set(
            _cache_key(scope, scope_id, model_alias), payload, ex=_CONFIG_CACHE_TTL_SEC
        )

    try:
        if _guard_enabled():
            from app.config import get_settings
            from app.services.hot_path import run_redis

            # 브레이커가 열려 있으면 쓰지 않는다(HotRedisUnavailable) — 다음 요청이 다시 채운다.
            await run_redis(_set, timeout=get_settings().hot_redis_timeout_sec,
                            what="rl_config_write")
        else:
            await _set()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("rl_config_cache_write_failed", scope=scope, scope_id=scope_id)


async def _query_scope_from_db(
    db: AsyncSession,
    scope: str,
    scope_id: str | None,
    model_alias: str,
) -> ScopeLimits:
    """DB에서 스코프 한도 조회 (모델별 우선, NULL fallback).

    GLOBAL 스코프는 ``scope_id IS NULL`` + ``model_alias=X`` 만 유효 — Bedrock 쿼터.
    """
    conditions = [RateLimitConfig.scope == scope, RateLimitConfig.is_active.is_(True)]

    if scope == "GLOBAL":
        conditions.append(RateLimitConfig.scope_id.is_(None))
        conditions.append(RateLimitConfig.model_alias == model_alias)
    else:
        conditions.append(RateLimitConfig.scope_id == scope_id)
        # 모델별 (우선) OR 전체 (fallback)
        conditions.append(
            or_(
                RateLimitConfig.model_alias == model_alias,
                RateLimitConfig.model_alias.is_(None),
            )
        )

    result = await db.execute(select(RateLimitConfig).where(and_(*conditions)))
    rows = result.scalars().all()

    if not rows:
        return ScopeLimits()

    # 모델별 먼저, 없으면 NULL 사용
    model_specific = next((r for r in rows if r.model_alias == model_alias), None)
    chosen = model_specific or rows[0]
    return ScopeLimits(
        rpm=chosen.rpm_limit,
        tpm=chosen.tpm_limit,
        cpm=chosen.cpm_limit_usd if scope != "GLOBAL" else None,
        cph=chosen.cph_limit_usd if scope != "GLOBAL" else None,
        # NULL 행 = "전체 모델 합산 한도" — 카운터 키를 `*` 로 만들어야
        # 모델들이 하나의 버킷을 공유한다.
        model_agnostic=chosen.model_alias is None,
    )


async def load_all_scope_limits(
    *,
    redis,
    db: AsyncSession | None,
    user_id: str,
    team_id: str | None,
    model_alias: str,
) -> AllScopeLimits:
    """3-스코프 한도 한꺼번에 로드 (Redis → DB fallback).

    Redis miss 시 DB 조회 → Redis 캐시 주입 (TTL 5분).
    DB 없음/실패 시 해당 스코프는 unlimited (None) 처리.
    """
    result = AllScopeLimits()

    scope_specs: list[tuple[str, str | None, str]] = [
        ("USER", user_id, "user"),
    ]
    if team_id is not None:
        scope_specs.append(("TEAM", team_id, "team"))
    scope_specs.append(("GLOBAL", None, "global_"))

    cached_all = await _load_scopes_from_cache(redis, scope_specs, model_alias)
    for (scope_name, scope_id, attr), cached in zip(scope_specs, cached_all):
        if cached is not None:
            setattr(result, attr, cached)
            remember_limits(scope_name, scope_id, model_alias, cached)
            continue

        if db is None:
            # DB fallback 불가 → unlimited
            continue

        try:
            limits = await _query_scope_from_db(db, scope_name, scope_id, model_alias)
        except Exception:
            logger.exception(
                "rl_config_db_query_failed",
                scope=scope_name,
                scope_id=scope_id,
                model_alias=model_alias,
            )
            # 공유 세션은 실패 후 aborted — rollback 해야 다음 스코프 쿼리가 산다.
            # 안 하면 한 스코프의 일시 장애가 TEAM/GLOBAL 까지 전부 실패로 번진다.
            try:
                await db.rollback()
            except Exception:
                pass
            # 실패한 스코프는 캐시하지 않는다 — unlimited 를 300s 캐시하면 DB 일시
            # 장애 한 번이 (GLOBAL 포함) 모든 한도를 캐시 만료까지 꺼버린다.
            # 이번 요청만 unlimited 로 통과시키고 다음 요청에 DB 를 다시 본다.
            continue

        setattr(result, attr, limits)
        remember_limits(scope_name, scope_id, model_alias, limits)
        await _save_scope_to_cache(redis, scope_name, scope_id, model_alias, limits)

    return result


async def invalidate_scope_cache(
    redis,
    scope: str,
    scope_id: str | None,
    model_alias: str,
) -> None:
    """Admin이 한도 변경 시 호출 — 해당 스코프 캐시 즉시 무효화."""
    if redis is None:
        return
    try:
        await redis.delete(_cache_key(scope, scope_id, model_alias))
    except Exception:
        logger.warning("rl_config_cache_invalidate_failed", scope=scope, scope_id=scope_id)
