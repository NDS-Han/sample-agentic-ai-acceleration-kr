# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import uuid
from typing import ClassVar

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.budget_cache import defer_redis_write_until_commit
from app.core.cache_invalidation import (
    CacheInvalidationManager,
    invalidate_after_commit,
)
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.models.auth import Team, User
from app.models.model import ModelAlias, RateLimitConfig, RateLimitScope
from app.repositories.model_repository import RateLimitConfigRepository
from app.repositories.user_repository import UserRepository
from app.schemas.rate_limits import (
    RateLimitConfigItem,
    RateLimitResponse,
    RateLimitScopeStatus,
    RateLimitSetRequest,
    RateLimitTreeNode,
)

logger = structlog.get_logger()


def _team_display_name(team: Team) -> str:
    """부서가 default가 아니면 '부서명_팀명' 형태로 반환한다.
    예: Claude_NDS_Developers 그룹 → Team.name='Developers', department='NDS' → 'NDS_Developers'"""
    settings = get_settings()
    default_dept_id = uuid.UUID(settings.DEFAULT_DEPT_ID)
    dept = getattr(team, "department", None)
    if dept is not None and team.dept_id != default_dept_id:
        return f"{dept.name}_{team.name}"
    return team.name


def _to_config_item(cfg: RateLimitConfig, scope: str) -> RateLimitConfigItem:
    """RateLimitConfig ORM → API 항목. 트리와 단건 GET 이 같은 변환을 써야
    배지/표시가 어긋나지 않는다."""
    return RateLimitConfigItem(
        target_id=str(cfg.scope_id) if cfg.scope_id else "",
        scope=scope,
        rpm=cfg.rpm_limit,
        tpm=cfg.tpm_limit,
        cpm=cfg.cpm_limit_usd,
        cph=cfg.cph_limit_usd,
    )


class RateLimitService:
    def __init__(self, cache_mgr: CacheInvalidationManager) -> None:
        self._cache_mgr = cache_mgr

    async def set_user_rate_limit(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        data: RateLimitSetRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> RateLimitResponse:
        return await self._set_rate_limit(
            session,
            scope=RateLimitScope.USER,
            scope_id=user_id,
            data=data,
            actor=actor,
            cache_key=f"ratelimit:config:user:{user_id}",
            ip_address=ip_address,
            request_id=request_id,
        )

    async def set_team_rate_limit(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        data: RateLimitSetRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> RateLimitResponse:
        # BR-RL-01: CPM/CPH allowed for USER and TEAM scopes (GLOBAL 제외).
        # 단기 비용 폭주 방어는 개별/팀 레벨에서. GLOBAL은 월 예산 엔진이 커버.

        return await self._set_rate_limit(
            session,
            scope=RateLimitScope.TEAM,
            scope_id=team_id,
            data=data,
            actor=actor,
            cache_key=f"ratelimit:config:team:{team_id}",
            ip_address=ip_address,
            request_id=request_id,
        )

    async def set_global_rate_limit(
        self,
        session: AsyncSession,
        *,
        model_alias: str,
        data: RateLimitSetRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> RateLimitResponse:
        # BR-RL-01: CPM/CPH not allowed for GLOBAL scope
        # (단기 비용 폭주 방어는 USER/TEAM 레벨에서. GLOBAL은 월 예산 엔진이 커버.)
        if data.cpm is not None or data.cph is not None:
            raise ValidationError("CPM/CPH limits are only allowed for USER/TEAM scopes")

        alias_exists = await session.scalar(
            select(ModelAlias.alias).where(ModelAlias.alias == model_alias)
        )
        if alias_exists is None:
            raise NotFoundError("ModelAlias", model_alias)

        return await self._set_rate_limit(
            session,
            scope=RateLimitScope.GLOBAL,
            scope_id=None,
            data=data,
            actor=actor,
            model_alias=model_alias,
            cache_key=f"ratelimit:config:global:{model_alias}",
            ip_address=ip_address,
            request_id=request_id,
        )

    async def get_rate_limit_tree(self, session: AsyncSession) -> list[RateLimitTreeNode]:
        user_repo = UserRepository(session)
        rl_repo = RateLimitConfigRepository(session)

        teams = await user_repo.list_all_teams()
        team_configs = await rl_repo.list_active_by_scope(RateLimitScope.TEAM)
        user_configs = await rl_repo.list_active_by_scope(RateLimitScope.USER)

        team_config_map: dict[str, RateLimitConfig] = {
            str(c.scope_id): c for c in team_configs if c.scope_id
        }
        user_config_map: dict[str, RateLimitConfig] = {
            str(c.scope_id): c for c in user_configs if c.scope_id
        }

        def _make_config(cfg: RateLimitConfig, scope: str) -> RateLimitConfigItem:
            return _to_config_item(cfg, scope)

        nodes: list[RateLimitTreeNode] = []
        for team in teams:
            team_id = str(team.id)
            team_cfg = team_config_map.get(team_id)

            member_nodes: list[RateLimitTreeNode] = []
            for member in team.members:
                member_id = str(member.id)
                user_cfg = user_config_map.get(member_id)
                inherited_from: str | None = None
                effective_cfg: RateLimitConfigItem | None = None
                if user_cfg:
                    effective_cfg = _make_config(user_cfg, "USER")
                elif team_cfg:
                    effective_cfg = _make_config(team_cfg, "TEAM")
                    inherited_from = team_id
                member_nodes.append(
                    RateLimitTreeNode(
                        id=member_id,
                        label=member.display_name,
                        scope="USER",
                        is_active=member.is_active,
                        config=effective_cfg,
                        children=[],
                        inherited_from=inherited_from,
                    )
                )

            has_active_members = any(m.is_active for m in team.members)
            nodes.append(
                RateLimitTreeNode(
                    id=team_id,
                    label=_team_display_name(team),
                    scope="TEAM",
                    is_active=has_active_members,
                    config=_make_config(team_cfg, "TEAM") if team_cfg else None,
                    children=member_nodes,
                    inherited_from=None,
                )
            )

        return nodes

    async def get_rate_limit_status(
        self,
        session: AsyncSession,
        *,
        scope: RateLimitScope,
        scope_id: uuid.UUID,
    ) -> RateLimitScopeStatus:
        """단건 조회 — /users 패널용. USER 는 own 이 없으면 팀 설정을
        inherited 로 내려준다(get_rate_limit_tree 의 USER 분기와 동일 규칙).
        TEAM 은 상위 상속이 없다."""
        repo = RateLimitConfigRepository(session)
        own = await repo.get_active(scope, scope_id)
        inherited = None
        inherited_scope = None
        if scope is RateLimitScope.USER and own is None:
            user = await session.get(User, scope_id)
            if user is None:
                raise NotFoundError("User", str(scope_id))
            if user.team_id:
                team_cfg = await repo.get_active(RateLimitScope.TEAM, user.team_id)
                if team_cfg is not None:
                    inherited = _to_config_item(team_cfg, "TEAM")
                    inherited_scope = "TEAM"
        return RateLimitScopeStatus(
            own=_to_config_item(own, scope.value) if own else None,
            inherited=inherited,
            inherited_scope=inherited_scope,
        )

    async def delete_rate_limit(
        self,
        session: AsyncSession,
        *,
        scope: RateLimitScope,
        scope_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        """own 설정을 비활성화해 상위 상속(USER→TEAM) 또는 무제한(TEAM)으로
        되돌린다. 설정이 없으면 멱등 성공 — /users 의 "상위 정책 따라가기"가
        이미 상속 상태에서 호출돼도 에러가 아니어야 한다."""
        repo = RateLimitConfigRepository(session)
        removed = await repo.deactivate_configs(scope, scope_id)
        # 프로세스 로컬 계획 캐시도 커밋 후에 비운다 — commit 전 클리어 창에 폴링이
        # 미커밋 구 행으로 재적재할 수 있다.
        await defer_redis_write_until_commit(session, self._clear_live_plans_deferred)

        # proxy 가 읽는 건 rl:config:* 뿐이지만, _set_rate_limit 이 쓰는
        # ratelimit:config:* 도 대칭으로 지워 stale 상태를 남기지 않는다.
        sid = str(scope_id)
        rl_scope = scope.value.lower()
        await invalidate_after_commit(
            session,
            self._cache_mgr,
            pattern=f"rl:config:{scope.value}:{sid}:*",
            extra=[
                lambda: self._cache_mgr._redis.delete(
                    f"ratelimit:config:{rl_scope}:{sid}"
                )
            ],
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            service_token_id=getattr(actor, "service_token_id", None),
            action="DELETE_RATE_LIMIT",
            resource_type="RateLimitConfig",
            resource_id=sid,
            changes={"after": {"scope": scope.value, "deactivated": removed}},
            ip_address=ip_address,
            request_id=request_id,
        )

    #: 실시간 RPM 키 계획(한도 행 → 카운터 키) 프로세스 로컬 캐시 — 카드가 10초마다 폴링한다.
    #: 같은 프로세스의 한도 변경은 즉시 비우고(_invalidate_live_plans), 다른 파드의 변경은
    #: 최대 이 시간 늦게 반영된다(표시 전용 — 집행은 gateway 가 한다).
    _LIVE_PLAN_TTL_S = 15.0

    #: 프로세스 로컬 계획 캐시 — app.state 의 싱글턴 외에 user_team_service(팀 이관의
    #: USER RL 비활성화)처럼 서비스를 거치지 않는 경로도 비울 수 있게 클래스 속성으로 둔다.
    _live_plans: ClassVar[dict] = {}

    @classmethod
    def clear_live_plans(cls) -> None:
        cls._live_plans.clear()

    def _invalidate_live_plans(self) -> None:
        self.clear_live_plans()

    async def _clear_live_plans_deferred(self) -> None:
        """``defer_redis_write_until_commit`` 용 awaitable — 커밋 후 계획 캐시를 비운다."""
        self.clear_live_plans()

    async def _live_plan(
        self, session: AsyncSession | None, sc: str, scope_id: str
    ) -> tuple[bool, list[str]]:
        """(tracked, 카운터 키 alias 목록). gateway 키 규약과 같은 규칙:

        * USER/TEAM — 요청 모델 X 에 대해 X 전용 행이 있으면 그 행, 없으면 NULL 행이 고른다
          (rate_limit_config_loader._query_scope_from_db). NULL 행은 `*` 키
          (rate_limit_enforcement._key_alias), 모델 행은 alias 키. rpm>0 인 행만 카운터를
          적재한다(check_multi_scope_rpm). 그래서 키 = {rpm>0 인 모델 행 alias} ∪ {`*` | NULL
          행 rpm>0}.
        * GLOBAL — scope_id IS NULL + 모델 행만 쓴다(NULL alias 행은 로더가 보지 않는다).
        """
        import time

        plans: dict = self._live_plans
        ck = (sc, scope_id)
        hit = plans.get(ck)
        now = time.monotonic()
        if hit is not None and hit[0] > now:
            return hit[1], hit[2]

        cfg_sid = uuid.UUID(scope_id) if sc != "GLOBAL" else None

        async def _read(s: AsyncSession):
            return await RateLimitConfigRepository(s).list_active_rpm_aliases(
                RateLimitScope(sc), cfg_sid
            )

        if session is None:
            # 짧은 세션 — 설정 두어 행만 읽고 바로 반납한다. 요청 범위 세션을 Redis 조회 동안
            # 붙잡지 않는다(카드 폴링이 기본 풀 연결을 상주시키지 않게).
            from app.core.db import AsyncSessionLocal

            async with AsyncSessionLocal() as s:
                try:
                    rows = await _read(s)
                finally:
                    await s.rollback()
        else:
            rows = await _read(session)

        aliases: set[str] = set()
        for alias, rpm in rows:
            if not rpm or rpm <= 0:
                continue
            if alias is None:
                if sc != "GLOBAL":
                    aliases.add("*")
            else:
                aliases.add(alias)
        plan = (bool(aliases), sorted(aliases))
        if len(plans) > 4096:
            plans.clear()
        plans[ck] = (now + self._LIVE_PLAN_TTL_S, *plan)
        return plan

    async def get_live_usage(
        self,
        session: AsyncSession | None,
        scope: str,
        scope_id: str,
        *,
        window_ms: int = 60_000,
    ) -> dict:
        """gateway-proxy 가 적재하는 **실시간 RPM 카운터**(Redis ZSET)를 읽어 현재
        사용량을 반환(§60.9).

        proxy 키 규약(gateway-proxy/rate_limit_scope.py:build_rl_key):
          `{{SCOPE:scope_id:model_alias}}:rpm`  (ZSET, member=request_id, score=now_ms)
        현재 사용량 = window(60s) 안의 ZSET 항목 수 = ZCOUNT(key, now-window, +inf).

        ⚠️ 예전엔 `SCAN MATCH {SCOPE:id:*}:rpm` 으로 키를 찾고 키마다 ZCOUNT 를 따로 보냈다 —
        SCAN 은 키스페이스 전체를 훑는다(gateway 의 수십만 키 · 카드 10초 폴링 × 관리자 수).
        이제 키는 한도 행에서 계산하고(_live_plan) ZCOUNT 는 파이프라인 한 번이다. 한도
        미설정이면 Redis 를 치지 않는다.

        ``session`` 이 None 이면 설정 조회만 짧은 세션으로 하고 Redis 조회 전에 반납한다.

        tracked: RPM 한도가 설정된 scope 에만 proxy 가 카운터를 적재한다 — 한도 미설정
        scope 의 0 은 "요청 없음"이 아니라 "계량 안 함"이다. 설정 조회가 실패하면 키를 알 수
        없으므로 available=false(0 을 지어내지 않는다).
        fail-soft: Redis 오류 → available=false (config 화면은 그대로).
        """
        import time

        sc = (scope or "").upper()
        if sc not in ("USER", "TEAM", "GLOBAL"):
            return {"available": False, "reason": "invalid scope"}
        try:
            tracked, aliases = await self._live_plan(session, sc, scope_id)
        except Exception as exc:  # noqa: BLE001 — 설정 조회 실패가 화면을 막지 않게
            logger.warning("rl_live_usage_config_failed", error=str(exc), scope=sc, scope_id=scope_id)
            return {"available": False, "reason": "config unavailable"}

        base = {
            "available": True,
            "scope": sc,
            "scope_id": scope_id,
            "window_sec": window_ms // 1000,
            "tracked": tracked,
            "rpm_used_total": 0,
            "by_model": [],
        }
        if not aliases:
            return base

        # ⚠️ GLOBAL 의 scope_id 는 proxy 가 `*` 로 적는다(rate_limit_scope.GLOBAL_WILDCARD).
        sid = scope_id if sc != "GLOBAL" else "*"
        keys = [f"{{{sc}:{sid}:{alias}}}:rpm" for alias in aliases]
        now_ms = int(time.time() * 1000)
        window_start = now_ms - window_ms
        try:
            redis = self._cache_mgr._redis
            pipe = redis.pipeline(transaction=False)
            for k in keys:
                # 윈도우 내 항목 수(만료분 제외) — proxy 의 ZCARD-after-cleanup 과 동치.
                pipe.zcount(k, window_start, "+inf")
            counts = await pipe.execute()
        except Exception as exc:  # noqa: BLE001 — 실시간 조회 실패가 화면을 막지 않게
            logger.warning("rl_live_usage_failed", error=str(exc), scope=sc, scope_id=scope_id)
            return {"available": False, "reason": f"{type(exc).__name__}"}

        per_model = [
            {"model_alias": alias, "rpm_used": int(cnt or 0)}
            for alias, cnt in zip(aliases, counts)
            if int(cnt or 0) > 0
        ]
        base["rpm_used_total"] = sum(m["rpm_used"] for m in per_model)
        base["by_model"] = sorted(per_model, key=lambda x: -x["rpm_used"])
        return base

    # 트렌드 조회의 창/버킷은 고정 조합만 허용 — free-form bucket 을 받으면 임의
    # 비용의 집계가 가능해져 admin-api 풀(pool_size=5+overflow10)을 압박한다.
    _TREND_WINDOWS = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}
    _TREND_BUCKETS = {"1h": 60, "6h": 300, "24h": 900, "7d": 3600}
    _TREND_CACHE_TTL_SEC = 60

    async def get_usage_trend(
        self,
        session: AsyncSession,
        scope: str,
        scope_id: str,
        window: str = "24h",
    ) -> dict:
        """USER/TEAM 의 과거 사용량 트렌드 — usage_logs 를 시간 버킷으로 집계.

        한도 설정의 근거 데이터: 버킷당 요청 수(RPM 근거)·토큰 수(TPM 근거,
        cache_read 제외 = compute_tpm_incr 과 동일 기준)·비용(CPH 근거)을 반환.
        idx_usage_logs_{user,team}_time 인덱스로 bounded window 만 스캔하고,
        결과는 60초 Redis 캐시 — 노드 재선택/윈도우 토글 반복이 매번 집계를
        치지 않게 한다. fail-soft: DB/Redis 오류 시 available=false.
        """
        from datetime import datetime, timezone

        from sqlalchemy import text

        sc = (scope or "").upper()
        # GLOBAL 은 usage_logs 의 단일 소유 컬럼에 대응하지 않아 트렌드 대상 아님.
        if sc not in ("USER", "TEAM"):
            return {"available": False, "reason": "invalid scope"}
        win_sec = self._TREND_WINDOWS.get(window)
        if win_sec is None:
            return {"available": False, "reason": "invalid window"}
        try:
            sid = uuid.UUID(scope_id)
        except (ValueError, AttributeError):
            return {"available": False, "reason": "invalid scope_id"}
        bucket_sec = self._TREND_BUCKETS[window]
        col = "user_id" if sc == "USER" else "team_id"
        cache_key = f"trend:rl:{sc}:{scope_id}:{window}"

        try:
            cached = await self._cache_mgr._redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:  # noqa: BLE001 — 캐시 실패는 무시하고 DB 조회로 진행
            pass

        now_ts = int(datetime.now(timezone.utc).timestamp())
        # 조회 하한을 버킷 경계로 내린다 — now-win 이 버킷 중간에 걸리면
        # 첫 버킷이 반쪽만 집계돼("언더카운트") 차트 시작점이 낮게 찍힌다.
        # 창이 최대 bucket_sec 만큼 길어지는 대가는 있다(bounded).
        start_ts = now_ts - win_sec
        start_ts -= start_ts % bucket_sec

        try:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT to_timestamp(
                                   floor(extract(epoch from requested_at) / :bucket) * :bucket
                               ) AS b,
                               count(*) AS req,
                               coalesce(sum(input_tokens + output_tokens
                                            + cache_creation_tokens), 0) AS tok,
                               coalesce(sum(cost_usd), 0) AS cost
                        FROM usage.usage_logs
                        WHERE {col} = :sid
                          AND requested_at >= to_timestamp(:start)
                        GROUP BY b
                        ORDER BY b
                        """
                    ),
                    {"bucket": bucket_sec, "sid": sid, "start": start_ts},
                )
            ).all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("rl_usage_trend_failed", error=str(exc), scope=sc, scope_id=scope_id)
            return {"available": False, "reason": f"{type(exc).__name__}"}

        by_t = {int(r.b.timestamp()): r for r in rows}
        live_bucket_ts = now_ts - now_ts % bucket_sec
        points = []
        t = start_ts
        while t <= now_ts:
            r = by_t.get(t)
            points.append(
                {
                    "t": t,
                    "requests": int(r.req) if r else 0,
                    "tokens": int(r.tok) if r else 0,
                    "cost_usd": float(r.cost) if r else 0.0,
                    # 진행 중인 버킷은 아직 다 차지 않았다 — UI 가 툴팁 등으로
                    # 구분할 수 있게 표시한다(나머지 버킷은 완전 집계).
                    "partial": t == live_bucket_ts,
                }
            )
            t += bucket_sec

        result = {
            "available": True,
            "scope": sc,
            "scope_id": scope_id,
            "window_sec": win_sec,
            "bucket_sec": bucket_sec,
            "points": points,
        }
        try:
            await self._cache_mgr._redis.setex(
                cache_key, self._TREND_CACHE_TTL_SEC, json.dumps(result)
            )
        except Exception:  # noqa: BLE001 — 캐시 쓰기 실패는 결과에 영향 없음
            pass
        return result

    async def _set_rate_limit(
        self,
        session: AsyncSession,
        *,
        scope: RateLimitScope,
        scope_id: uuid.UUID | None,
        data: RateLimitSetRequest,
        actor: CurrentUser,
        cache_key: str,
        model_alias: str | None = None,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> RateLimitResponse:
        repo = RateLimitConfigRepository(session)
        await defer_redis_write_until_commit(session, self._clear_live_plans_deferred)

        config = RateLimitConfig(
            id=uuid.uuid4(),
            scope=scope,
            scope_id=scope_id,
            model_alias=model_alias,
            rpm_limit=data.rpm,
            tpm_limit=data.tpm,
            cpm_limit_usd=data.cpm,
            cph_limit_usd=data.cph,
            is_active=True,
            created_by=actor.user_id,
        )
        await repo.upsert(config)

        # Write config to Redis for Gateway Proxy — commit 후 지연(§6-6:
        # commit 전 SET 은 rollback 시 미커밋 정책을 TTL 동안 광고한다).
        config_json = json.dumps({
            "rpm": data.rpm,
            "tpm": data.tpm,
            "cpm": str(data.cpm) if data.cpm else None,
            "cph": str(data.cph) if data.cph else None,
        })
        await defer_redis_write_until_commit(
            session,
            lambda: self._cache_mgr._redis.set(cache_key, config_json),
        )

        # Invalidate gateway-proxy's rate-limit policy cache so the new policy
        # takes effect on the next request instead of waiting for the 5-min TTL.
        # Gateway uses keys: rl:config:{SCOPE}:{scope_id_or_NULL}:{model_alias}
        # USER/TEAM with model_alias=None covers all models → wildcard delete.
        sid = str(scope_id) if scope_id is not None else "NULL"
        if model_alias is not None:
            await invalidate_after_commit(
                session,
                self._cache_mgr,
                [f"rl:config:{scope.value}:{sid}:{model_alias}"],
            )
        else:
            await invalidate_after_commit(
                session,
                self._cache_mgr,
                pattern=f"rl:config:{scope.value}:{sid}:*",
            )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            service_token_id=getattr(actor, "service_token_id", None),
            action="SET_RATE_LIMIT",
            resource_type="RateLimitConfig",
            resource_id=str(config.id),
            changes={"after": {"scope": scope.value, "rpm": data.rpm, "tpm": data.tpm}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return RateLimitResponse(
            scope=scope.value,
            scope_id=str(scope_id) if scope_id else None,
            model_alias=model_alias,
            rpm_limit=config.rpm_limit,
            tpm_limit=config.tpm_limit,
            cpm_limit_usd=config.cpm_limit_usd,
            cph_limit_usd=config.cph_limit_usd,
            is_active=config.is_active,
        )
