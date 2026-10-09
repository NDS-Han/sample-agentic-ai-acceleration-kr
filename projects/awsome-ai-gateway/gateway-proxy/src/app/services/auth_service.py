# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import hashlib
import json
from typing import Protocol

import jwt
import structlog
from sqlalchemy import select
from sqlalchemy import text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import JwtPublicKey, User
from app.models.model import TeamAllowedModel, UserAllowedModel
from app.schemas.domain import AuthContext, AuthType, Role

logger = structlog.get_logger(__name__)

VK_CACHE_TTL = 300  # 5분
JWT_KEY_CACHE_TTL = 3600  # 1시간

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
                # VK 폐기 시 admin-api 가 key:vk:{hash} 와 key:cache:vk:{hash} 를
                # 함께 DEL 한다. 캐시 DEL 이 일시 Redis 장애로 실패하면 AuthContext
                # 가 남아 폐기 키가 TTL(≤300s) 동안 통과했다 — 그래서 캐시 히트도
                # 매핑 키 존재를 함께 확인한다. revoke 가 두 키를 같이 지우므로
                # 매핑 부재 = 폐기됨.
                vk_mapping = await redis.get(f"key:vk:{key_hash}")
                if vk_mapping is None:
                    await redis.delete(f"key:cache:vk:{key_hash}")
                    raise PermissionError("Invalid or inactive virtual key")
                data = json.loads(cached)
                # user.is_active 재확인 — 캐시 TTL(300s) 안에 계정 비활성화된 경우 즉시 차단
                if db is not None:
                    from sqlalchemy import select as sa_select
                    result = await db.execute(
                        sa_select(User.is_active).where(User.id == data["user_id"])
                    )
                    is_active = result.scalar_one_or_none()
                    if is_active is False:
                        await redis.delete(f"key:cache:vk:{key_hash}", f"key:vk:{key_hash}")
                        raise PermissionError("User account is deactivated")
                return AuthContext(**data)

        # 2) VK→user_id 매핑 조회 (Admin API가 발급 시 저장)
        user_id = None
        if redis is not None:
            raw = await redis.get(f"key:vk:{key_hash}")
            if raw:
                user_id = raw if isinstance(raw, str) else raw.decode()

        if user_id is None:
            raise PermissionError("Invalid or inactive virtual key")

        # 3) 사용자 정보 조회
        if db is None:
            raise PermissionError("DB unavailable, cache miss")

        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise PermissionError("User not found or inactive")

        # allowed_models 스냅샷 — 우선순위 user > team > none (260626_comm_customer 항목2).
        #   user_allowed_models 행 존재 → 그 화이트리스트만 (팀 무시).
        #   user 행 0개 → team_allowed_models 로 폴백. 둘 다 없음 → None(전체 허용).
        # ★ fail-closed: 국가핵심기술 제한이므로, user override 조회가 DB 오류로 실패하면
        #   제한을 우회시키지 않고 인증을 막는다(allowed_clients 의 fail-open 과 다름).
        try:
            uam_result = await db.execute(
                select(UserAllowedModel.model_alias).where(
                    UserAllowedModel.user_id == user.id
                )
            )
            user_aliases = list(uam_result.scalars().all())
        except Exception:
            logger.warning(
                "user_allowed_models_lookup_failed_fail_closed",
                user_id=str(user.id),
                exc_info=True,
            )
            raise PermissionError(
                "model access policy unavailable (fail-closed)"
            )

        allowed_models: list[str] | None
        if user_aliases:
            allowed_models = user_aliases
        elif user.team_id:
            tam_result = await db.execute(
                select(TeamAllowedModel.model_alias).where(
                    TeamAllowedModel.team_id == user.team_id
                )
            )
            team_aliases = list(tam_result.scalars().all())
            allowed_models = team_aliases if team_aliases else None
        else:
            allowed_models = None

        # allowed_clients — 우선순위 user > team > org > none (alembic 0038,
        # allowed_models 의 user > team > none 과 같은 구조에 org 기본값 한 단계 추가).
        #   각 스코프에서 행 0개 = 정책 없음 = 하위 스코프로 폴백. 전면 거부는 표현 불가.
        #   모두 없음 → None(전체 허용).
        # text 는 모듈 상단에서 import — except 가 ImportError 까지 삼켜 enforcement 를
        # 조용히 끄지 않도록(실제 DB I/O 실패만 fail-open) 범위를 좁힌다.
        allowed_clients: list[str] | None = None
        try:
            rows = (await db.execute(
                _sql_text("SELECT client FROM auth.user_allowed_clients WHERE user_id = :uid"),
                {"uid": str(user.id)},
            )).scalars().all()
            if rows:
                allowed_clients = list(rows)
            elif user.team_id:
                rows = (await db.execute(
                    _sql_text(
                        "SELECT client FROM auth.team_allowed_clients WHERE team_id = :tid"
                    ),
                    {"tid": str(user.team_id)},
                )).scalars().all()
                if rows:
                    allowed_clients = list(rows)
                else:
                    rows = (await db.execute(
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
                        {"tid": str(user.team_id)},
                    )).scalars().all()
                    allowed_clients = list(rows) if rows else None
        except Exception:
            allowed_clients = None  # DB 조회 실패 시 막지 않음(allow-all) — fail-open(soft gating)

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

        # 4) AuthContext 캐시 저장
        if redis is not None:
            await redis.setex(
                f"key:cache:vk:{key_hash}",
                VK_CACHE_TTL,
                auth_context.model_dump_json(),
            )

        return auth_context


class JWTAuthStrategy:
    """JWT 인증 (OpenAI 경로 /v1/*)."""

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
            if db is None:
                raise PermissionError("DB unavailable, JWT key cache miss")
            try:
                result = await db.execute(
                    select(JwtPublicKey).where(JwtPublicKey.is_active.is_(True))
                )
                jwt_keys = list(result.scalars().all())
            except Exception as e:  # 내부 장애도 401 로 — 500 유발 경로를 남기지 않는다
                logger.warning("jwt_key_lookup_failed", error=str(e))
                raise PermissionError(f"JWT key lookup failed: {e}")
            if not jwt_keys:
                raise PermissionError(f"Unknown JWT kid: {kid}")

            matched_key: JwtPublicKey | None = None
            last_err: Exception | None = None
            for jwt_key in jwt_keys:
                try:
                    claims = _decode_jwt(
                        token,
                        jwt_key.public_key_pem,
                        jwt_key.algorithm,
                        issuer=jwt_key.issuer,
                        audience=jwt_key.audience,
                    )
                    matched_key = jwt_key
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
            if redis is not None:
                await redis.setex(
                    kid_cache_key,
                    JWT_KEY_CACHE_TTL,
                    json.dumps(
                        {
                            "pem": matched_key.public_key_pem,
                            "algorithm": matched_key.algorithm,
                            "issuer": matched_key.issuer,
                            "audience": matched_key.audience,
                        }
                    ),
                )

        # 알 수 없는 role 라벨은 통째로 401 을 만드는 대신 건너뛰고 경고를 남긴다.
        # (Role(r) 가 ValueError → middleware 가 이유 없는 401 로 바꿔버렸다.)
        roles: list[Role] = []
        for r in claims.get("roles", []):
            try:
                roles.append(Role(r))
            except ValueError:
                logger.warning("unknown_role_in_jwt", role=str(r))

        return AuthContext(
            user_id=claims["user_id"],
            team_id=claims["team_id"],
            dept_id=claims["dept_id"],
            roles=roles,
            auth_type=AuthType.JWT,
            key_id=None,
            allowed_models=None,  # JWT는 Key Scope 없음
            # allowed_clients 미설정(None=both) — JWT 경로는 client 정책을 강제하지 않음.
            # 의도적: Claude Code/Cowork 추론 트래픽은 VK(api-key-helper/inferenceGatewayApiKey)
            # 로 인증하므로 VK 경로의 allowed_clients 로 커버됨. JWT 는 admin/console 경로.
            # JWT 사용자까지 강제하려면 claims["user_id"] 로 동일 DB 조회를 추가하면 됨(향후).
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
    if path.startswith("/model/"):
        return _VK_STRATEGY
    if path.startswith("/v1/"):
        return _JWT_STRATEGY
    return None
