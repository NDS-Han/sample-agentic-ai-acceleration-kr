# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Protocol

import jwt
import structlog
from sqlalchemy import select
from sqlalchemy import text as _sql_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import JwtPublicKey, User
from app.models.model import TeamAllowedModel, UserAllowedModel
from app.schemas.domain import AuthContext, AuthType, Role

logger = structlog.get_logger(__name__)

VK_CACHE_TTL = 300  # 5분
# per-kid 캐시는 keyset 캐시(60s)를 우회하므로 그보다 길면 비활성화된 키가 최대
# TTL 만큼 계속 유효하다(R16-V5). 재캐시는 keyset GET+검증이라 DB 를 치지 않는다.
# ⚠️ 최악의 폐기 전파 지연은 ~2×TTL(≈120s): 폐기 직전 keyset 에서 캐시된 per-kid
#    엔트리가 TTL 만큼 살고, 그 사이 keyset 자체도 최대 TTL 만큼 stale 하다.
JWT_KEY_CACHE_TTL = 60


class AuthBackendUnavailableError(PermissionError):
    """인증 백엔드(DB/Redis) 장애로 인증 자체가 불가 — 자격증명 거부와 구분한다.

    PermissionError 를 상속해 기존 except 경로는 유지되지만, 미들웨어는 이
    서브클래스를 먼저 잡아 503 을 내고 security 이벤트로 기록하지 않는다 —
    인프라 장애를 'Invalid or inactive virtual key' 401 로 오보하면 클라이언트가
    정상 키를 재발급받으려 시도하고, auth-failure 카운터가 장애 트래픽으로 오염된다.
    """

# 공개키 테이블(admin_jwt_configs)은 비대칭 알고리즘만 허용한다 — 대칭 HMAC
# (HS256 등)이 등록되면 공개 PEM 텍스트가 곧 서명 비밀이 돼 alg-confusion 위조가
# 성립한다. DB 오설정/침해 방어선. HS* 를 공개키 테이블에 둘 합법적 이유가 없다.
_ALLOWED_JWT_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)


def _decode_jwt(
    token: str,
    pem: str,
    algorithm: str,
    *,
    issuer: str | None,
    audience: str | None,
) -> dict:
    """admin_jwt_configs 의 키 레코드 계약으로 JWT 를 검증한다.

    admin-api 의 JWTVerifier(core/auth.py:65-74)와 동일하게 issuer/audience 를
    강제한다 — 예전엔 둘 다 넘기지 않아 (a) 같은 키로 서명된 다른 용도의 토큰이
    수용되고(confused-deputy), (b) PyJWT 기본 verify_aud=True 때문에 `aud`
    클레임을 실은 정상 토큰이 InvalidAudienceError 로 전부 401 이 났다.
    audience 가 비어 있으면 aud 검증을 끄고, issuer 도 비어 있을 때만 생략한다.
    """
    if algorithm not in _ALLOWED_JWT_ALGORITHMS:
        # 대칭/알 수 없는 알고리즘 레코드는 검증에 쓰지 않는다.
        raise jwt.InvalidAlgorithmError(f"disallowed jwt algorithm: {algorithm}")
    options: dict = {"require": ["user_id", "team_id", "dept_id", "roles", "exp"]}
    kwargs: dict = {}
    if audience:
        kwargs["audience"] = audience
    else:
        options["verify_aud"] = False
    if issuer:
        kwargs["issuer"] = issuer
    return jwt.decode(token, pem, algorithms=[algorithm], options=options, **kwargs)


def _extract_bearer_token(authorization: str) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise ValueError("Missing or invalid Authorization header")
    return authorization[7:]


async def _resolve_entitlements(
    db: AsyncSession, user_id: str, team_id: str | None
) -> tuple[list[str] | None, list[str] | None]:
    """(allowed_models, allowed_clients) 해소 — VK/JWT 공용.

    allowed_models — 우선순위 user > team > none (260626_comm_customer 항목2).
      user_allowed_models 행 존재 → 그 화이트리스트만 (팀 무시).
      user 행 0개 → team_allowed_models 로 폴백. 둘 다 없음 → None(전체 허용).
    ★ fail-closed: 국가핵심기술 제한이므로, user override 조회가 DB 오류로 실패하면
      제한을 우회시키지 않고 인증을 막는다(allowed_clients 의 fail-open 과 다름).

    allowed_clients — 우선순위 user > team > org > none (alembic 0038).
      각 스코프에서 행 0개 = 정책 없음 = 하위 스코프로 폴백. 전면 거부는 표현 불가.
      모두 없음 → None(전체 허용). DB 조회 실패 시 막지 않음(allow-all) —
      fail-open(soft gating) — 다만 무음으로 끄지 않고 경고 로그를 남긴다.
    """
    # allowed_models
    try:
        uam_result = await db.execute(
            select(UserAllowedModel.model_alias).where(
                UserAllowedModel.user_id == user_id
            )
        )
        user_aliases = list(uam_result.scalars().all())
    except Exception:
        logger.warning(
            "user_allowed_models_lookup_failed_fail_closed",
            user_id=str(user_id),
            exc_info=True,
        )
        # fail-closed 는 유지하되 상태코드는 정직하게: entitlement 해소 자체가
        # 불가한 인프라 장애이므로 401(자격 거부)이 아니라 503 다 — 401 이면
        # 클라이언트가 정상 키를 재발급받으려 하고 auth-failure 카운터가 오염된다.
        raise AuthBackendUnavailableError(
            "model access policy unavailable (fail-closed)"
        )

    allowed_models: list[str] | None
    if user_aliases:
        allowed_models = user_aliases
    elif team_id:
        tam_result = await db.execute(
            select(TeamAllowedModel.model_alias).where(
                TeamAllowedModel.team_id == team_id
            )
        )
        team_aliases = list(tam_result.scalars().all())
        allowed_models = team_aliases if team_aliases else None
    else:
        allowed_models = None

    # allowed_clients — 실패 시 fail-open 이지만 관측은 남긴다.
    allowed_clients: list[str] | None = None
    try:
        rows = (
            await db.execute(
                _sql_text(
                    "SELECT client FROM auth.user_allowed_clients WHERE user_id = :uid"
                ),
                {"uid": str(user_id)},
            )
        ).scalars().all()
        if rows:
            allowed_clients = list(rows)
        elif team_id:
            rows = (
                await db.execute(
                    _sql_text(
                        "SELECT client FROM auth.team_allowed_clients WHERE team_id = :tid"
                    ),
                    {"tid": str(team_id)},
                )
            ).scalars().all()
            if rows:
                allowed_clients = list(rows)
            else:
                rows = (
                    await db.execute(
                        _sql_text(
                            """
                            SELECT oac.client FROM auth.org_allowed_clients oac
                             WHERE oac.org_id = (
                                 SELECT d.org_id FROM auth.teams t
                                  JOIN auth.departments d ON d.id = t.dept_id
                                 WHERE t.id = :tid
                             )
                            """
                        ),
                        {"tid": str(team_id)},
                    )
                ).scalars().all()
                allowed_clients = list(rows) if rows else None
    except Exception:
        logger.warning(
            "allowed_clients_lookup_failed_fail_open",
            user_id=str(user_id),
            exc_info=True,
        )
        allowed_clients = None

    return allowed_models, allowed_clients


def _is_jwt_token(token: str) -> bool:
    """JWT 형태 판별: '.' 구분 3-part."""
    parts = token.split(".")
    return len(parts) == 3


class AuthStrategy(Protocol):
    async def authenticate(
        self, authorization: str, redis, db: AsyncSession | None
    ) -> AuthContext: ...


class VKAuthStrategy:
    """Virtual Key 인증 (Bedrock 경로 /model/*)."""

    async def authenticate(
        self, authorization: str, redis, db: AsyncSession | None
    ) -> AuthContext:
        token = _extract_bearer_token(authorization)
        key_hash = hashlib.sha256(token.encode()).hexdigest()

        # 1) AuthContext 캐시 조회
        if redis is not None:
            cached = await redis.get(f"key:cache:vk:{key_hash}")
            if cached:
                # 깨진 캐시 엔트리(JSON 손상, AuthContext 스키마 드리프트로 인한
                # ValidationError)는 미스로 처리해 DB 해소로 떨어진다 — 401 이면
                # 배포 직후 캐시 TTL(≤300s) 동안 전 VK 사용자가 잠긴다.
                try:
                    cached_ctx = AuthContext(**json.loads(cached))
                except Exception:
                    logger.warning("vk_auth_cache_corrupt", key_hash=key_hash[:16])
                    cached_ctx = None
                if cached_ctx is not None:
                    # VK 폐기 시 admin-api 가 key:vk:{hash} 와 key:cache:vk:{hash} 를
                    # 함께 DEL 한다. 캐시 DEL 이 일시 Redis 장애로 실패하면 AuthContext
                    # 가 남아 폐기 키가 TTL(≤300s) 동안 통과했다 — 그래서 캐시 히트도
                    # 매핑 키 존재를 함께 확인한다. revoke 가 두 키를 같이 지우므로
                    # 매핑 부재 = 폐기됨.
                    vk_mapping = await redis.get(f"key:vk:{key_hash}")
                    if vk_mapping is None:
                        try:
                            await redis.delete(f"key:cache:vk:{key_hash}")
                        except Exception:
                            logger.warning("vk_cache_cleanup_failed")
                        raise PermissionError("Invalid or inactive virtual key")
                    # user.is_active 재확인 — 캐시 TTL(300s) 안에 계정 비활성화된 경우 즉시 차단
                    if db is not None:
                        from sqlalchemy import select as sa_select
                        result = await db.execute(
                            sa_select(User.is_active).where(User.id == cached_ctx.user_id)
                        )
                        is_active = result.scalar_one_or_none()
                        # `is False` 는 행 없음(None — hard-deleted 유저)을 통과
                        # 시킨다 — 삭제된 유저가 VK 캐시 TTL(300s) 동안 인증된다.
                        # None 도 차단한다(R22 r1 N1).
                        if not is_active:
                            try:
                                await redis.delete(f"key:cache:vk:{key_hash}", f"key:vk:{key_hash}")
                            except Exception:
                                logger.warning("vk_cache_cleanup_failed")
                            raise PermissionError("User account is deactivated")
                    return cached_ctx

        # 2) VK→user_id 매핑 조회 (Admin API가 발급 시 저장). Redis 가 유일한
        #    소스다 — DB 의 auth.virtual_keys 는 암호화된 원문이라 해시로 조회가
        #    안 되고, 무효키 폭주가 DB 를 때리는 것을 막는 의도된 설계(§9).
        user_id = None
        if redis is not None:
            raw = await redis.get(f"key:vk:{key_hash}")
            if raw:
                user_id = raw if isinstance(raw, str) else raw.decode()

        if user_id is None:
            if redis is None:
                raise AuthBackendUnavailableError("redis unavailable, VK lookup impossible")
            raise PermissionError("Invalid or inactive virtual key")

        # 3) 사용자 정보 조회
        if db is None:
            raise AuthBackendUnavailableError("DB unavailable, cache miss")

        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise PermissionError("User not found or inactive")

        allowed_models, allowed_clients = await _resolve_entitlements(
            db, str(user.id), str(user.team_id) if user.team_id else None
        )

        auth_context = AuthContext(
            user_id=str(user.id),
            team_id=str(user.team_id) if user.team_id else "",
            dept_id="",
            roles=[Role.USER],
            auth_type=AuthType.VIRTUAL_KEY,
            key_id=None,
            allowed_models=allowed_models,
            allowed_clients=allowed_clients,
            sso_subject=user.sso_subject,
        )

        # 4) AuthContext 캐시 저장 — best-effort: 쓰기 실패는 다음 요청의 캐시
        #    미스일 뿐이라, 인증에 성공한 요청까지 RedisError→503 으로 만들면 안 된다.
        if redis is not None:
            try:
                await redis.setex(
                    f"key:cache:vk:{key_hash}",
                    VK_CACHE_TTL,
                    auth_context.model_dump_json(),
                )
            except Exception:
                logger.warning("vk_auth_cache_write_failed")

        return auth_context


class JWTAuthStrategy:
    """JWT 인증 (OpenAI 경로 /v1/*)."""

    #: 활성 키 "세트" Redis 캐시 — TTL 자체가 클러스터 전체의 refresh 스로틀이다.
    #: 캐시 미스(per-kid) 토큰은 이 세트로만 검증해 위조 kid 가 DB 를 전혀 치지
    #: 않게 하고(무인증 DoS 증폭 차단), 세트 미스 시에만 DB 를 읽는다 — 워커 수와
    #: 무관하게 DB 스캔은 TTL 당 최대 1회. 키 로테이션/비활성화가 반영되는 데
    #: 최대 이 시간이 걸리는 것이 트레이드오프(기존 per-kid 1h 캐시보다 짧다).
    _JWT_KEYSET_CACHE_KEY = "key:cache:jwt:keyset"
    _JWT_KEYSET_CACHE_TTL = 60

    def __init__(self) -> None:
        # Redis 장애 시 쓰는 프로세스 로컬 keyset 사본 — (monotonic 시각, 레코드).
        # "데이터" 캐시라 전역 스로틀과 달리 lockout 을 만들지 않는다: 위조 kid 는
        # TTL 동안 DB 를 치지 않고, 정상 신키도 TTL 만료 후 재조회돼 통과한다.
        self._local_keyset: tuple[float, list[dict]] | None = None

    async def _get_active_keyset(
        self, redis, db: AsyncSession | None
    ) -> list[dict] | None:
        """활성 JWT 키 세트 — Redis keyset 캐시 → 로컬 사본(TTL 내) → DB 순.

        반환 None 은 "조회 수단 전무"(db=None 이고 캐시도 없거나, DB 장애 + 캐시
        전무). DB 장애 시엔 **만료된** 로컬 사본이라도 반환한다 — 서명 검증은 캐시
        데이터로 계속 가능하므로 DB 다운이 곧 전원 503 이 되지 않는다.
        """
        if redis is not None:
            try:
                raw_set = await redis.get(self._JWT_KEYSET_CACHE_KEY)
                if raw_set:
                    parsed = json.loads(
                        raw_set if isinstance(raw_set, str) else raw_set.decode()
                    )
                    if isinstance(parsed, list):
                        return parsed
            except Exception:
                pass
        local = self._local_keyset
        if local is not None and (
            time.monotonic() - local[0] < self._JWT_KEYSET_CACHE_TTL
        ):
            return local[1]
        if db is None:
            # 조회 수단이 DB 뿐인데 없다 — 오래된 로컬 사본이라도 쓴다.
            return local[1] if local is not None else None
        try:
            result = await db.execute(
                select(JwtPublicKey).where(JwtPublicKey.is_active.is_(True))
            )
            jwt_keys = list(result.scalars().all())
        except Exception as e:
            # 인프라 장애만 캐시 폴백/503 — 코드 버그(AttributeError 등)는 그대로
            # 전파해 500 으로 남긴다. 싸버리면 디버깅 단서가 사라진다.
            if not isinstance(e, (SQLAlchemyError, OSError, asyncio.TimeoutError)):
                raise
            logger.warning("jwt_key_lookup_failed", error=str(e))
            return local[1] if local is not None else None
        key_records = [
            {
                "pem": k.public_key_pem,
                "algorithm": k.algorithm,
                "issuer": k.issuer,
                "audience": k.audience,
            }
            for k in jwt_keys
        ]
        self._local_keyset = (time.monotonic(), key_records)
        # 빈 세트(활성 키 0개)도 짧게 캐시해 stampede 를 막는다.
        if redis is not None:
            try:
                await redis.setex(
                    self._JWT_KEYSET_CACHE_KEY,
                    self._JWT_KEYSET_CACHE_TTL,
                    json.dumps(key_records),
                )
            except Exception:
                logger.warning("jwt_keyset_cache_write_failed")
        return key_records

    async def authenticate(
        self, authorization: str, redis, db: AsyncSession | None
    ) -> AuthContext:
        token = _extract_bearer_token(authorization)

        # kid 추출 (헤더 디코딩, 검증 없이)
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
        except Exception as e:
            raise PermissionError(f"Invalid JWT header: {e}")

        if not kid:
            raise PermissionError("JWT missing kid claim")

        # 캐시 키는 kid 원문이 아니라 sha256 digest(A3-1) — kid 는 공격자가 고르는
        # 서명 헤더라, 원문을 그대로 쓰면 무제한 길이/임의 문자열의 Redis 키를
        # 무한히 만들 수 있다. digest 는 64자 고정. 충돌 시 캐시된 PEM 이 이 토큰과
        # 안 맞아 verify 실패 → DB 전체 키 순회로 떨어져 올바른 키로 덮어쓴다.
        kid_cache_key = (
            f"key:cache:jwt:{hashlib.sha256(kid.encode()).hexdigest()}"
        )

        # public key 조회 — 캐시는 {"pem","algorithm","issuer","audience"} JSON.
        # 레거시 형태(순수 PEM 문자열, iss/aud 없는 JSON)는 iss/aud 를 모르므로
        # 캐시 검증 없이 DB 재조회로 새 형태 레코드로 갱신한다.
        cached_rec: tuple[str, str, str | None, str | None] | None = None
        if redis is not None:
            cached_key = await redis.get(kid_cache_key)
            if cached_key:
                raw = (
                    cached_key if isinstance(cached_key, str) else cached_key.decode()
                )
                try:
                    data = json.loads(raw)
                    if data.get("pem") and "issuer" in data and "audience" in data:
                        cached_rec = (
                            data["pem"],
                            data.get("algorithm", "RS256"),
                            data["issuer"],
                            data["audience"],
                        )
                except Exception:
                    pass

        # ⚠️ auth.admin_jwt_configs 에는 `kid` / `status` 컬럼이 없다. 실제 플래그는
        #    `is_active` 다 (db/init/02_create_tables.sql, app/models/auth.py:50-59,
        #    admin-api 의 같은 테이블 미러도 동일). 예전 코드는 존재하지 않는 두 컬럼을
        #    참조해 AttributeError 를 냈고, middleware/auth.py 가 (PermissionError,
        #    ValueError) 만 잡으므로 3-part 토큰(만료 JWT·Cognito id_token·`a.b.c`)이면
        #    무인증 엔드포인트에서 HTTP 500 이 났다. kid 는 캐시 키로만 쓴다.
        #    per-kid 선택이 필요해지면 kid 컬럼을 마이그레이션으로 추가한 뒤 필터를
        #    되살릴 것 — 없는 컬럼을 참조하는 쿼리로 되돌리지 말 것.
        #
        # ⚠️ 검증은 **활성 키 전체 순회**다 — 예전엔 `.first()` 로 임의의 키 하나만
        #    시도해, 로테이션 겹침(구키+신키 동시 active) 동안 다른 키로 서명된 정상
        #    토큰이 401 을 맞고, 그 잘못된 PEM 이 `key:cache:jwt:{kid}` 로 1시간
        #    캐시돼 같은 kid 의 후속 요청까지 계속 실패했다. admin-api 의
        #    JWTVerifier 와 같은 전략이다.
        claims: dict | None = None
        if cached_rec is not None:
            pem, algorithm, issuer, audience = cached_rec
            try:
                claims = _decode_jwt(
                    token, pem, algorithm, issuer=issuer, audience=audience
                )
            except jwt.ExpiredSignatureError:
                raise PermissionError("JWT expired")
            except jwt.InvalidTokenError:
                # 캐시된 키가 이 토큰과 안 맞을 수 있다(로테이션/재발급) —
                # DB 의 전체 활성 키로 재시도한다.
                claims = None

        if claims is None:
            # 활성 키 세트 — keyset 캐시 TTL 이 DB 스캔 빈도를 제한하므로
            # 프로세스 내 스로틀은 필요 없다(전역 타임스탬프 스로틀은 위조 kid
            # 하나가 정상 JWT 사용자 전원을 lockout 시키는 역 DoS 벡터였다).
            # db=None(DB_DEGRADED) 여도 캐시된 세트로 서명 검증은 계속한다 —
            # 세트가 없을 때만 백엔드 장애로 503.
            key_records = await self._get_active_keyset(redis, db)
            if key_records is None:
                raise AuthBackendUnavailableError(
                    "JWT key lookup unavailable (no cache, DB down)"
                )
            if not key_records:
                raise PermissionError(f"Unknown JWT kid: {kid}")

            matched_key: dict | None = None
            last_err: Exception | None = None
            for rec in key_records:
                pem = rec.get("pem") if isinstance(rec, dict) else None
                if not pem:
                    # 캐시된 keyset 의 변형 레코드 — 건너뛰지 않으면 KeyError 가
                    # 500 으로 새어나간다(401 로 떨어지는 게 정직한 판정).
                    continue
                try:
                    claims = _decode_jwt(
                        token,
                        pem,
                        rec.get("algorithm", "RS256"),
                        issuer=rec.get("issuer"),
                        audience=rec.get("audience"),
                    )
                    matched_key = rec
                    break
                except jwt.ExpiredSignatureError:
                    # 서명이 맞았는데 만료됐다 — 다른 키를 볼 이유가 없다.
                    raise PermissionError("JWT expired")
                except jwt.InvalidTokenError as e:
                    last_err = e
                    continue
            if matched_key is None:
                raise PermissionError(f"JWT invalid: {last_err}")

            # 검증에 성공한 **그 키**를 kid 아래에 캐시한다 — 잘못된 키가
            # 캐시돼 후속 요청을 계속 실패시키는 일이 없게.
            # best-effort: 쓰기 실패는 다음 요청의 캐시 미스일 뿐이다.
            if redis is not None:
                try:
                    await redis.setex(
                        kid_cache_key,
                        JWT_KEY_CACHE_TTL,
                        json.dumps(matched_key),
                    )
                except Exception:
                    logger.warning("jwt_key_cache_write_failed")

        # 알 수 없는 role 라벨은 통째로 401 을 만드는 대신 건너뛰고 경고를 남긴다.
        # (Role(r) 가 ValueError → middleware 가 이유 없는 401 로 바꿔버렸다.)
        roles: list[Role] = []
        for r in claims.get("roles", []):
            try:
                roles.append(Role(r))
            except ValueError:
                logger.warning("unknown_role_in_jwt", role=str(r))

        # ★ 계정 상태 + entitlement 재검증 — 서명만 맞으면 통과하던 시절에는
        #    비활성화된 사용자의 생존 JWT 가 만료 때까지 유효했고, allowed_models/
        #    allowed_clients 도 전부 None 으로 들어가 VK 의 Key Scope 를 우회할 수
        #    있었다. db 가 있으면 VK 와 동일 정책을 적용한다. Role.ADMIN 은
        #    콘솔 관리자라 모델/클라이언트 ACL 대상에서 제외(기존 계약 유지).
        #    db is None(DB_DEGRADED) 이면 해소 자체가 불가 — 캐시된 kid 로 서명은
        #    검증되지만 ACL 은 미검증(None)으로 통과시키는 디그레이드 계약 유지.
        allowed_models: list[str] | None = None
        allowed_clients: list[str] | None = None
        if db is not None:
            user_row = (
                await db.execute(
                    select(User.is_active, User.team_id).where(
                        User.id == claims["user_id"]
                    )
                )
            ).first()
            if user_row is None or not user_row.is_active:
                raise PermissionError("User not found or inactive")
            if Role.ADMIN not in roles:
                allowed_models, allowed_clients = await _resolve_entitlements(
                    db,
                    str(claims["user_id"]),
                    str(user_row.team_id) if user_row.team_id else None,
                )

        return AuthContext(
            user_id=claims["user_id"],
            # 팀 이관 후 JWT 클레임은 만료까지 옛 팀을 가리킨다 — 예산/레이트리밋
            # 스코프가 옛 팀으로 계속 청구된다. DB 에서 읽은 team_id 를 우선하고
            # db=None(DB_DEGRADED)일 때만 클레임으로 폴백한다(R22 r1 N6).
            team_id=(
                str(user_row.team_id)
                if db is not None and user_row is not None and user_row.team_id
                else claims["team_id"]
            ),
            dept_id=claims["dept_id"],
            roles=roles,
            auth_type=AuthType.JWT,
            key_id=None,
            allowed_models=allowed_models,
            allowed_clients=allowed_clients,
        )


class DualAuthStrategy:
    """/v1/usage/me 전용 — VK 또는 JWT 자동 판별."""

    def __init__(self) -> None:
        self._vk = VKAuthStrategy()
        self._jwt = JWTAuthStrategy()

    async def authenticate(
        self, authorization: str, redis, db: AsyncSession | None
    ) -> AuthContext:
        token = _extract_bearer_token(authorization)
        if _is_jwt_token(token):
            return await self._jwt.authenticate(authorization, redis, db)
        return await self._vk.authenticate(authorization, redis, db)


# 전역 전략 인스턴스
_VK_STRATEGY = VKAuthStrategy()
_JWT_STRATEGY = JWTAuthStrategy()
_DUAL_STRATEGY = DualAuthStrategy()


def resolve_auth_strategy(path: str) -> AuthStrategy | None:
    """경로 기반 인증 전략 반환.

    NOTE (FR-1.4 A안, 2026-04-17): /v1/chat/completions, /v1/completions는
    본래 JWT 전용(FR-2.1b)이나 Week 3까지 사내 JWT 인프라가 준비되지 않아
    VK로 mock-vllm E2E 검증 가능하도록 임시로 DUAL에 편입. Week 3 JWT 붙을 때
    JWT 전용 또는 JWT 우선 DUAL로 전환할 것. 자세한 인계는
    `requirements-document/milestone.md` Week 3 섹션 참조.
    """
    if path in (
        "/v1/messages",
        "/v1/messages/count_tokens",
        "/v1/usage/me",
        "/v1/models",
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/responses",  # Codex (OpenAI Responses API) — same VK/DUAL inference path
    ):
        return _DUAL_STRATEGY
    # /v1/models/{id} 같은 단일 모델 상세 엔드포인트 — Claude Code 가 세션 시작 시 호출.
    # 정확 매칭에 없으면 아래 `/v1/` fallthrough 로 JWT 로 라우팅돼 VK 인증 실패.
    if path.startswith("/v1/models/"):
        return _DUAL_STRATEGY
    # /v1/usage/team/{id} — 핸들러가 "TEAM_LEADER(본인 팀) 또는 ADMIN" 계약을
    # 집행하는데, TEAM_LEADER 는 VK 만 갖는다. `/v1/` fallthrough(JWT 전용)에 두면
    # VK 호출이 401 로 죽어 계약상 도달 불가 — DUAL 여야 한다.
    if path.startswith("/v1/usage/team/"):
        return _DUAL_STRATEGY
    if path.startswith("/model/"):
        return _VK_STRATEGY
    if path.startswith("/v1/"):
        return _JWT_STRATEGY
    return None
