# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt
from jose.exceptions import JWKError

from app.core.db import AsyncSessionLocal
from app.core.oidc_verifier import OIDCConfigError, OIDCVerifier, OIDCVerifyError
from app.models.auth import UserRole
from app.services.service_token_service import (
    SERVICE_TOKEN_PREFIX,
    SERVICE_TOKEN_RANDOM_BYTES,
    ServiceTokenService,
    hash_token,
)

logger = structlog.get_logger()


@dataclass
class CurrentUser:
    user_id: uuid.UUID
    email: str
    role: UserRole
    team_id: uuid.UUID | None
    is_service_token: bool = False
    service_token_id: uuid.UUID | None = None


# 공개키 테이블(admin_jwt_configs)에는 비대칭 알고리즘만 등록돼야 한다 —
# 대칭 HMAC(HS256 등)이 들어가면 공개 PEM 텍스트가 곧 서명 비밀이 돼
# alg-confusion 위조가 성립한다. gateway-proxy(auth_service)도 같은 allowlist.
_ALLOWED_JWT_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)


class JWTVerifier:
    """Loads Admin JWT public keys at startup and verifies RS256 tokens."""

    def __init__(self) -> None:
        self._public_keys: dict[str, dict] = {}  # kid -> {pem, issuer, audience, algorithm}

    def load_configs(self, configs: list[dict]) -> None:
        for cfg in configs:
            if cfg["algorithm"] not in _ALLOWED_JWT_ALGORITHMS:
                # 오설정/침해로 대칭 alg 가 등록된 키는 검증에 쓰지 않는다.
                logger.warning(
                    "jwt_key_disallowed_algorithm",
                    kid=str(cfg["id"]),
                    algorithm=cfg["algorithm"],
                )
                continue
            kid = str(cfg["id"])
            self._public_keys[kid] = {
                "pem": cfg["public_key_pem"],
                "issuer": cfg["issuer"],
                "audience": cfg["audience"],
                "algorithm": cfg["algorithm"],
            }
        logger.info("jwt_verifier.loaded", key_count=len(self._public_keys))

    def verify(self, token: str) -> dict:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        # If kid present, look up specific key; otherwise try all active keys
        candidates = [self._public_keys[kid]] if kid and kid in self._public_keys else list(self._public_keys.values())

        if not candidates:
            raise JWTError("No matching public key found")

        last_error: JWTError | None = None
        for key_cfg in candidates:
            try:
                payload = jwt.decode(
                    token,
                    key_cfg["pem"],
                    algorithms=[key_cfg["algorithm"]],
                    issuer=key_cfg["issuer"],
                    audience=key_cfg["audience"],
                    # IdP id_token 의 at_hash 는 access_token 과의 binding 검증용이다.
                    # 쿠키에는 id_token 만 실리므로 비교할 access_token 이 없다 —
                    # core.oidc_verifier 와 같은 이유로 끈다.
                    options={"verify_at_hash": False},
                )
                return payload
            except JWTError as e:
                last_error = e
                continue
            except JWKError as e:
                # 이 설정의 키 자체를 만들 수 없다(예: 시드 자리표시 PEM
                # ``REPLACE_WITH_ACTUAL_RS256_PUBLIC_KEY``). 그 키로는 아무것도 검증할 수
                # 없으니 다음 키로 넘어간다 — 잡지 않으면 인증 실패가 500 으로 보인다.
                last_error = JWTError(f"unusable public key: {e}")
                continue

        raise last_error  # type: ignore[misc]


def _extract_token(request: Request) -> str:
    """Extract JWT from Authorization header or admin_jwt cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]

    # Fallback: read from cookie (admin-ui sends cookies via server-side fetch)
    cookie_header = request.headers.get("Cookie", "")
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("admin_jwt="):
            return part[len("admin_jwt="):]

    raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")


def _parse_dev_token(token: str) -> dict | None:
    """Parse dev-mode JWT (format: dev.<base64url-payload>.sig). Returns None if not a dev token."""
    import base64
    import json
    import os

    if not token.startswith("dev.") or os.getenv("DEV_LOGIN_ENABLED") != "true":
        return None

    # 이중 가드: configmap 실수로 DEV_LOGIN_ENABLED=true 가 prod 에 새어도
    # dev 토큰은 절대 수용하지 않는다(백서명 없는 형식이므로 위조가 자유다).
    from app.core.config import get_settings

    if get_settings().is_production:
        return None

    parts = token.split(".")
    if len(parts) != 3:
        return None

    try:
        payload_b64 = parts[1]
        # Add padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception:
        return None


async def get_current_user(request: Request) -> CurrentUser:
    token = _extract_token(request)

    # Dev token shortcut (DEV_LOGIN_ENABLED=true only)
    dev_payload = _parse_dev_token(token)
    if dev_payload is not None:
        return CurrentUser(
            user_id=uuid.UUID("00000000-0000-4000-a000-000000000010"),
            email=dev_payload.get("email", "admin@dev.local"),
            role=UserRole(dev_payload.get("role", "ADMIN")),
            team_id=None,
        )

    # Service token shortcut: external systems call with `Bearer svc-...`.
    # Resolved against auth.service_tokens (sha256 hash). Synthesizes an ADMIN
    # identity. Non-`svc-` tokens fall through to the JWT path below (unchanged).
    if token.startswith(SERVICE_TOKEN_PREFIX):
        # ⚠️ 형식이 아예 틀린 토큰은 DB 왕복 전에 끊는다 — 무효 svc- 토큰 홍수가
        #    요청마다 새 세션+해시조회를 열지 못하게(VK 경로가 같은 이유로
        #    invalid 키에 세션을 안 여는 것과 대칭).
        body = token[len(SERVICE_TOKEN_PREFIX):]
        if len(body) != SERVICE_TOKEN_RANDOM_BYTES * 2 or not all(
            c in "0123456789abcdef" for c in body
        ):
            raise HTTPException(status_code=401, detail="Invalid or expired service token")
        svc_service = ServiceTokenService()
        async with AsyncSessionLocal() as session:
            svc_tok = await svc_service.verify(session, hash_token(token))
        if svc_tok is None:
            raise HTTPException(status_code=401, detail="Invalid or expired service token")
        return CurrentUser(
            user_id=uuid.UUID("00000000-0000-4000-a000-000000000011"),
            email="service-token@admin.local",
            role=UserRole.ADMIN,
            team_id=None,
            is_service_token=True,
            service_token_id=svc_tok.id,
        )

    # ── IdP 가 발급한 토큰은 IdP 의 JWKS 로 검증한다 ──
    #
    # admin-ui 의 OIDC 로그인은 IdP id_token 을 그대로 admin_jwt 쿠키에 굽는다. 그 토큰을
    # 아래 정적 키(auth.admin_jwt_configs — 우리가 발급하는 내부 admin JWT 용)로 검증하면
    # 설치 직후엔 시드 자리표시 키뿐이라 모든 API 호출이 실패했다(2026-09-25 US dev 실측:
    # JWKError → 500). VK 발급(/v1/auth/exchange)이 이미 쓰는 OIDCVerifier 는 JWKS 를
    # 자동으로 받고 kid 로 골라 키 교체에도 안전하다. iss 로 경로만 고른다 — 서명·iss 는
    # verify_async 가 다시 엄격히 검증하므로 iss 를 위조해도 401 로 끝난다.
    oidc_verifier = _idp_verifier_for(request, token)
    if oidc_verifier is not None:
        try:
            claims = await oidc_verifier.verify_async(token, request.app.state.oidc_http)
        except OIDCVerifyError:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        except OIDCConfigError as e:
            logger.error("auth.idp_jwks_unavailable", error=str(e))
            raise HTTPException(status_code=503, detail="Identity provider unavailable")
        return await _resolve_idp_identity(request, claims)

    verifier: JWTVerifier = request.app.state.jwt_verifier

    try:
        payload = verifier.verify(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # ── 내부 admin JWT: `sub` 가 곧 auth.users.id 이고 `role` 클레임이 있다 ──
    #
    # ⚠️ IdP 가 직접 발급한 id_token 은 이 형상이 **아니다.** admin-ui 는 OIDC 콜백에서
    #    받은 id_token 을 그대로 `admin_jwt` 쿠키에 굽고(app/api/auth/callback/route.ts),
    #    그 토큰의 `sub` 는 **IdP 의 subject** 이지 우리 DB PK 가 아니다. 그리고 `role`
    #    클레임도 없다(IdP 는 groups 를 준다). 예전엔 그 토큰이 아래 한 줄을 그대로 타서
    #    두 가지로 깨졌다:
    #
    #      1. `uuid.UUID(payload["sub"])` — Cognito sub 는 UUID 라 파싱은 되지만 그 값은
    #         DB 에 없는 id 다. created_by FK 가 붙는 모든 쓰기(모델 생성·단가 설정)가
    #         **FK 위반 500** 으로 죽었다.
    #      2. Okta/Keycloak/Entra 의 sub 는 UUID 가 아니다("00u1a2b3…"). 그러면
    #         `uuid.UUID()` 가 **ValueError** 를 던지는데 이 함수는 그걸 잡지 않으므로
    #         401 도 403 도 아닌 **500** 이 나간다 — 인증 실패가 서버 오류로 보인다.
    #
    #    그래서 IdP 토큰은 DB 신원으로 해석한다. 판정 기준은 **발급자(iss)** 다 —
    #    IdP 에 custom mapper 로 `role` 클레임을 싣는 설정이면, 그 클레임을 그대로
    #    신뢰하는 것이 권한 상승 경로가 된다. 내부 admin JWT 경로는 한 글자도 바뀌지
    #    않는다(issuer 가 IdP 가 아니고 role 이 있으면 아래로 온다).
    from app.core.config import get_settings

    oidc_issuer = get_settings().OIDC_ISSUER_URL
    if (oidc_issuer and payload.get("iss") == oidc_issuer) or "role" not in payload:
        return await _resolve_idp_identity(request, payload)

    team_id_raw = payload.get("team_id")
    try:
        return CurrentUser(
            user_id=uuid.UUID(payload["sub"]),
            email=payload.get("email", ""),
            role=UserRole(payload["role"]),
            team_id=uuid.UUID(team_id_raw) if team_id_raw else None,
        )
    except (ValueError, TypeError, KeyError):
        # sub 가 UUID 가 아니거나 role 이 enum 에 없는 값 → 우리 토큰이 아니다.
        # 500 이 아니라 401.
        raise HTTPException(status_code=401, detail="Invalid token claims")


def _idp_verifier_for(request: Request, token: str) -> OIDCVerifier | None:
    """토큰의 (검증 전) ``iss`` 가 설정된 OIDC issuer 면 그 검증기를, 아니면 None."""
    verifier = getattr(request.app.state, "oidc_verifier", None)
    if not isinstance(verifier, OIDCVerifier):
        return None
    try:
        iss = jwt.get_unverified_claims(token).get("iss")
    except JWTError:
        return None
    if not isinstance(iss, str) or iss.rstrip("/") != verifier.issuer_url:
        return None
    return verifier


# sub -> (unix 만료시각, email 또는 "") — Redis 다운 시 인프로세스 폴백 캐시.
# 두 번째 계층이 없으면 Redis 장애 중 email-less 토큰 사용자 전원이 매 요청마다
# Cognito AdminGetUser 를 호출해 계정 전체 읽기 쿼터를 소진한다.
_EMAIL_ENRICH_CACHE: dict[str, tuple[float, str]] = {}
_EMAIL_ENRICH_TTL = 300
_EMAIL_ENRICH_MISS_TTL = 30  # 미해결("!")은 짧게 — Cognito 일시 오류가 5분 강등이 되지 않도록


async def _verified_email_for_subject(request: Request, sub: str) -> str:
    """토큰에 검증된 이메일이 없을 때 Cognito 서버측 속성으로 ADMIN_EMAILS 평가 근거를 보강.

    access token(Cognito 는 email claim 없음)이나 email 클레임을 안 주는 배치에서
    ADMIN_EMAILS 만 설정된 관리자는 매칭할 이메일이 없어 DEVELOPER 로 강등된다 —
    sub 가 신원을 증명하므로 풀 속성의 검증된 이메일을 대신 쓴다. 캐시는 인프로세스
    → Redis(`oidc:emailver:{sub}`) 순으로, Cognito 왕복은 sub 당 TTL 에 한 번.
    ADMIN_EMAILS 가 비어 있으면 평가할 것 자체가 없어 바로 "".
    """
    from app.core.config import get_settings

    settings = get_settings()
    if not sub or not settings.ADMIN_EMAILS:
        return ""
    # Cognito 보강은 issuer 가 그 풀일 때만 유효하다 — IdP 가 Okta/Entra 인데
    # COGNITO_USER_POOL_ID 만 설정된(동기화용) 배치에선 admin_get_user 가
    # 외부 sub 로 항상 미스 → "!" 캐시만 쌓는 무의미 호출이 된다.
    pool_id = settings.COGNITO_USER_POOL_ID
    issuer = (settings.OIDC_ISSUER_URL or "").rstrip("/")
    if not (pool_id and issuer.endswith("/" + pool_id)):
        return ""
    import time

    now = time.time()
    hit = _EMAIL_ENRICH_CACHE.get(sub)
    if hit and hit[0] > now:
        return hit[1]
    redis = getattr(request.app.state, "redis", None)
    cache_key = f"oidc:emailver:{sub}"
    if redis is not None:
        try:
            cached = await redis.get(cache_key)
        except Exception:
            cached = None
        if cached is not None:
            email = "" if cached == "!" else cached
            _EMAIL_ENRICH_CACHE[sub] = (
                now + (_EMAIL_ENRICH_MISS_TTL if cached == "!" else _EMAIL_ENRICH_TTL),
                email,
            )
            return email
    email = ""
    oidc_service = getattr(request.app.state, "oidc_service", None)
    if oidc_service is not None:
        try:
            pool_email, pool_verified = await oidc_service._email_attrs_from_cognito(
                str(sub)
            )
        except Exception:
            pool_email, pool_verified = None, False
        if pool_email and pool_verified:
            email = pool_email
    # 미해결은 짧은 TTL — Cognito 일시 장애(스로틀링 등)가 관리자를 5분간
    # 강등시키는 것보다 30초마다 재시도가 낫다.
    ttl = _EMAIL_ENRICH_TTL if email else _EMAIL_ENRICH_MISS_TTL
    _EMAIL_ENRICH_CACHE[sub] = (now + ttl, email)
    if len(_EMAIL_ENRICH_CACHE) > 10000:
        _EMAIL_ENRICH_CACHE.clear()  # 무한 증가 방지 — 만료항목 재구축
    if redis is not None:
        try:
            await redis.set(cache_key, email or "!", ex=ttl)
        except Exception:
            pass
    return email


async def _resolve_idp_identity(request: Request, claims: dict) -> CurrentUser:
    """IdP id_token 클레임 → DB 신원.

    ``auth.users`` 를 ``sso_subject`` 로 조회한다. 여기서 프로비저닝은 **하지 않는다** —
    이 경로는 admin-ui 세션 검증이고, 프로비저닝은 ``POST /v1/auth/exchange``
    (``services/oidc_service.py``)의 책임이다. 두 곳에서 만들면 같은 사용자가 서로 다른
    팀/역할로 두 번 생길 수 있다.

    role 은 DB 값이 아니라 **현재 토큰의 그룹**으로 판정한다. IdP 에서 관리자 그룹을
    떼어낸 사용자가 DB 에 ADMIN 으로 남아 있는 동안 계속 ADMIN 으로 도는 것을 막아야
    한다(권한 회수가 즉시 반영되어야 하는 방향). 단, ADMIN 매핑(그룹/이메일)을
    아예 설정하지 않은 운영 형태에서는 ``derive_role`` 이 ADMIN 을 낼 수 없으므로
    DB role 을 그대로 신뢰한다 — 안 그러면 수동 부여한 관리자가 전원 잠긴다.
    """
    from app.core.config import get_settings
    from app.core.oidc_identity import (
        admin_mapping_configured,
        claims_truthy,
        derive_role,
        extract_claims,
        required_group_missing,
    )
    from app.repositories.user_repository import UserRepository

    parsed = extract_claims(claims)
    if not parsed.subject:
        raise HTTPException(status_code=401, detail="Token is missing a subject claim")

    missing = required_group_missing(parsed.groups)
    if missing is not None:
        logger.warning("auth.idp_required_group_missing", subject=parsed.subject, required=missing)
        raise HTTPException(status_code=403, detail="required_group_missing")

    async with AsyncSessionLocal() as session:
        user = await UserRepository(session).get_by_sso_subject(parsed.subject)

    if user is None:
        # ⚠️ 401 이 아니라 403 이다. 토큰은 유효하다 — 이 사람이 아직 프로비저닝되지
        #    않은 것이다. 401 을 주면 admin-ui middleware 가 쿠키를 지우고 로그인으로
        #    되돌리는데, IdP 세션이 살아 있으니 즉시 같은 토큰으로 돌아와 무한 루프가 된다.
        logger.warning(
            "auth.idp_user_not_provisioned", subject=parsed.subject, email=parsed.email
        )
        raise HTTPException(status_code=403, detail="user_not_provisioned")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="user_deactivated")

    # ADMIN_EMAILS 매칭은 **이 토큰의 검증된 이메일 클레임**에만 허용한다 —
    # 미검증 클레임 이메일은 사용자가 쓸 수 있는 값이라 주장만으로 ADMIN 승격이
    # 된다. DB 이메일은 폴백으로 쓰지 않는다: 이 게이트 이전에 미검증으로 저장된
    # 행이 남아 있을 수 있고, 클레임 이메일이 없는 토큰(access token)이면
    # "DB 에 신뢰된 주소가 있다"는 가정 하나로 승격되던 경로가 열린다.
    # 그룹 클레임은 IdP 가 관리하므로 이 게이트와 무관하다.
    # email_verified 클레임은 "email" **속성**에 대한 검증 — 매핑된 이메일
    # 클레임이 email 이 아니면(예: preferred_username, upn) 그 값을 증명하지
    # 않으므로 미검증 취급한다.
    claim_email_verified = (
        claims_truthy(claims.get("email_verified"))
        if get_settings().OIDC_EMAIL_CLAIM == "email"
        else False
    )
    email_for_role = parsed.email or ""
    if not (email_for_role and claim_email_verified):
        enriched = await _verified_email_for_subject(request, parsed.subject)
        if enriched:
            email_for_role, claim_email_verified = enriched, True
    role_from_claims = derive_role(
        email_for_role,
        parsed.groups,
        email_verified=claim_email_verified,
    )
    if role_from_claims == UserRole.ADMIN:
        role = UserRole.ADMIN
    elif user.role == UserRole.ADMIN and admin_mapping_configured():
        # ADMIN 출처가 IdP 로 설정된 환경에서 토큰이 더 이상 ADMIN 을 내지 않는다
        # — 그룹 회수(또는 ADMIN_EMAILS 제외)다. DB role 은 마지막 로그인 시점의
        # 스냅샷일 뿐이라 그대로 쓰면 회수가 영구 지연된다.
        role = UserRole.DEVELOPER
    else:
        role = user.role

    return CurrentUser(
        user_id=user.id,
        email=user.email or parsed.email,
        role=role,
        team_id=user.team_id,
    )


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


async def require_admin_or_team_leader(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role not in (UserRole.ADMIN, UserRole.TEAM_LEADER):
        raise HTTPException(status_code=403, detail="Admin or Team Leader role required")
    return user


def require_team_leader_of(team_id: uuid.UUID):
    """Returns a dependency that verifies the user is ADMIN or TEAM_LEADER of the specified team."""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role == UserRole.ADMIN:
            return user
        if user.role == UserRole.TEAM_LEADER and user.team_id == team_id:
            return user
        raise HTTPException(status_code=403, detail="Not authorized for this team")

    return _check
