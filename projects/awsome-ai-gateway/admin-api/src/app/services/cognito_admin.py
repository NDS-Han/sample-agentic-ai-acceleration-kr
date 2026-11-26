# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""/aicm/v1 이 쓰는 Cognito 호출 래퍼 — 타임아웃, 쿼터 가드, 명확한 오류 매핑.

- boto3(이미 쓰는 의존성)의 ``cognito-idp`` 클라이언트를 ``botocore.config.Config`` 로 만든다:
  connect/read 타임아웃(``AICM_COGNITO_*_TIMEOUT_S``), standard 재시도(최대 ``AICM_COGNITO_MAX_ATTEMPTS``).
  동기 boto3 호출은 ``asyncio.to_thread`` 로 이벤트 루프 밖에서 돈다.
- **쿼터 가드**: Cognito 쿼터는 계정·리전 단위로 로그인 보강·sync·고객의 다른 앱과 공유된다
  (UserUpdate 25 RPS, UserList 30 RPS 는 조정 불가). 통합이 일으키는 호출은 카테고리별 초당 몫
  (``AICM_COGNITO_RPS_*``, Redis 공유 버킷)을 넘지 못한다. 넘으면 1초까지 기다렸다가 다시 보고,
  그래도 넘으면 503 ``cognito_busy`` + ``Retry-After: 1``.
- **오류 매핑** (``IntegrationError``):
  스로틀링 → 503 ``cognito_throttled`` · IAM 거부 → 503 ``cognito_permission_missing`` ·
  풀 없음 → 503 ``cognito_misconfigured`` · 타임아웃/연결 실패 → 504 ``cognito_timeout`` ·
  파라미터 오류 → 400 ``cognito_invalid_parameter`` · 그 외 → 502 ``cognito_error``.
  호출부가 직접 처리할 코드(예: ``UsernameExistsException``)는 ``passthrough`` 로 넘겨
  ``CognitoClientError`` 로 받는다.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from app.core.config import get_settings
from app.core.exceptions import IntegrationError

logger = structlog.get_logger()

USER_UPDATE = "user_update"
USER_LIST = "user_list"
USER_CREATION = "user_creation"
USER_READ = "user_read"

#: 오퍼레이션 → Cognito 쿼터 카테고리 (docs: Amazon Cognito quotas, "API request rate").
OP_CATEGORY: dict[str, str] = {
    "list_users": USER_LIST,
    "admin_get_user": USER_READ,
    "admin_list_groups_for_user": USER_READ,
    "admin_create_user": USER_CREATION,
    "admin_link_provider_for_user": USER_UPDATE,
    "admin_update_user_attributes": USER_UPDATE,
    "admin_disable_user": USER_UPDATE,
    "admin_enable_user": USER_UPDATE,
    "admin_user_global_sign_out": USER_UPDATE,
}

#: 오퍼레이션 → IAM 액션 (cognito_permission_missing 메시지·HANDOFF 목록).
OP_IAM_ACTION: dict[str, str] = {
    "list_users": "cognito-idp:ListUsers",
    "admin_get_user": "cognito-idp:AdminGetUser",
    "admin_list_groups_for_user": "cognito-idp:AdminListGroupsForUser",
    "admin_create_user": "cognito-idp:AdminCreateUser",
    "admin_link_provider_for_user": "cognito-idp:AdminLinkProviderForUser",
    "admin_update_user_attributes": "cognito-idp:AdminUpdateUserAttributes",
    "admin_disable_user": "cognito-idp:AdminDisableUser",
    "admin_enable_user": "cognito-idp:AdminEnableUser",
    "admin_user_global_sign_out": "cognito-idp:AdminUserGlobalSignOut",
}

_THROTTLE = frozenset({
    "TooManyRequestsException", "LimitExceededException", "ThrottlingException",
    "RequestLimitExceeded",
})
_DENIED = frozenset({"AccessDeniedException", "AccessDenied", "UnrecognizedClientException"})


class CognitoClientError(Exception):
    """호출부가 ``passthrough`` 로 직접 처리하겠다고 한 Cognito 오류 코드."""

    def __init__(self, code: str, message: str, op: str) -> None:
        super().__init__(f"{op}: {code}: {message}")
        self.code = code
        self.message = message
        self.op = op


def build_client():
    """타임아웃·재시도 상한이 걸린 cognito-idp 클라이언트."""
    import boto3
    from botocore.config import Config

    s = get_settings()
    cfg = Config(
        connect_timeout=s.AICM_COGNITO_CONNECT_TIMEOUT_S,
        read_timeout=s.AICM_COGNITO_READ_TIMEOUT_S,
        # total_max_attempts = 최초 시도 포함(botocore 의 max_attempts 는 재시도 횟수라 +1 이 된다).
        retries={"mode": "standard", "total_max_attempts": s.AICM_COGNITO_MAX_ATTEMPTS},
    )
    return boto3.client("cognito-idp", region_name=s.COGNITO_REGION, config=cfg)


def map_error(exc: BaseException, op: str) -> IntegrationError:
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        ConnectTimeoutError,
        EndpointConnectionError,
        NoCredentialsError,
        ReadTimeoutError,
    )

    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        msg = exc.response.get("Error", {}).get("Message", "")
        details = {"cognito_operation": op, "cognito_code": code}
        if code in _THROTTLE:
            return IntegrationError(
                f"Cognito throttled {op}; retry later", code="cognito_throttled",
                status_code=503, retry_after=1, details=details,
            )
        if code in _DENIED:
            action = OP_IAM_ACTION.get(op, op)
            return IntegrationError(
                f"admin-api role is not allowed to call {action}", code="cognito_permission_missing",
                status_code=503, details={**details, "iam_action": action},
            )
        if code == "ResourceNotFoundException":
            return IntegrationError(
                "Cognito user pool not found (check COGNITO_USER_POOL_ID / COGNITO_REGION)",
                code="cognito_misconfigured", status_code=503, details=details,
            )
        if code == "InvalidParameterException":
            return IntegrationError(
                f"Cognito rejected {op}: {msg}", code="cognito_invalid_parameter",
                status_code=400, details=details,
            )
        if code == "UserNotFoundException":
            return IntegrationError(
                "user not found in Cognito", code="cognito_user_not_found", status_code=404,
                details=details,
            )
        return IntegrationError(
            f"Cognito {op} failed: {code}", code="cognito_error", status_code=502, details=details,
        )
    if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError)):
        return IntegrationError(
            f"Cognito {op} timed out or was unreachable", code="cognito_timeout", status_code=504,
            details={"cognito_operation": op},
        )
    if isinstance(exc, NoCredentialsError):
        return IntegrationError(
            "admin-api has no AWS credentials for Cognito", code="cognito_permission_missing",
            status_code=503, details={"cognito_operation": op},
        )
    if isinstance(exc, BotoCoreError):
        return IntegrationError(
            f"Cognito {op} failed: {type(exc).__name__}", code="cognito_unavailable",
            status_code=503, details={"cognito_operation": op},
        )
    return IntegrationError(
        f"Cognito {op} failed", code="cognito_error", status_code=502, details={"cognito_operation": op},
    )


def _cap(category: str) -> int:
    s = get_settings()
    return {
        USER_UPDATE: s.AICM_COGNITO_RPS_USER_UPDATE,
        USER_LIST: s.AICM_COGNITO_RPS_USER_LIST,
        USER_CREATION: s.AICM_COGNITO_RPS_USER_CREATION,
        USER_READ: s.AICM_COGNITO_RPS_USER_READ,
    }[category]


# Redis 장애 시 로컬 버킷 — {category: (second, count)}
_LOCAL: dict[str, tuple[int, int]] = {}


class CognitoAdmin:
    def __init__(self, client, *, redis=None, user_pool_id: str | None = None) -> None:
        self.client = client
        self._redis = redis
        self.pool_id = user_pool_id if user_pool_id is not None else get_settings().COGNITO_USER_POOL_ID

    async def _try_take(self, category: str, n: int) -> bool:
        cap = _cap(category)
        sec = int(time.time())
        if self._redis is not None:
            key = f"itg:cog:{category}:{sec}"
            try:
                used = await self._redis.incrby(key, n)
                if used == n:
                    await self._redis.expire(key, 3)
                return used <= cap
            except Exception:
                logger.warning("aicm.cognito_guard_redis_unavailable", category=category)
        local_cap = max(1, cap // max(1, get_settings().AICM_COGNITO_FALLBACK_DIVISOR))
        s, c = _LOCAL.get(category, (sec, 0))
        if s != sec:
            s, c = sec, 0
        c += n
        _LOCAL[category] = (s, c)
        return c <= local_cap

    async def acquire(self, category: str, n: int = 1) -> None:
        if await self._try_take(category, n):
            return
        await asyncio.sleep(max(0.0, 1.0 - (time.time() % 1.0)) + 0.01)
        if await self._try_take(category, n):
            return
        raise IntegrationError(
            f"integration Cognito budget for {category} is exhausted; retry shortly",
            code="cognito_busy", status_code=503, retry_after=1,
        )

    async def call(self, op: str, *, passthrough: frozenset[str] | set[str] = frozenset(), **kwargs) -> dict[str, Any]:
        if not self.pool_id:
            raise IntegrationError(
                "COGNITO_USER_POOL_ID is not configured", code="cognito_not_configured", status_code=503,
            )
        await self.acquire(OP_CATEGORY[op])
        fn = getattr(self.client, op)
        try:
            return await asyncio.to_thread(fn, UserPoolId=self.pool_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 — 아래에서 분류
            from botocore.exceptions import ClientError

            if isinstance(exc, ClientError):
                code = exc.response.get("Error", {}).get("Code", "")
                if code in passthrough:
                    raise CognitoClientError(code, exc.response.get("Error", {}).get("Message", ""), op) from exc
            mapped = map_error(exc, op)
            logger.warning(
                "aicm.cognito_call_failed", op=op, code=mapped.code,
                cognito_code=(mapped.details or {}).get("cognito_code"),
            )
            raise mapped from exc

    # ── 자주 쓰는 조회 ──

    async def find_by_email(self, email: str) -> list[dict]:
        resp = await self.call("list_users", Filter=f'email = "{email}"', Limit=10)
        return list(resp.get("Users", []))

    async def find_by_sub(self, sub: str) -> dict | None:
        resp = await self.call("list_users", Filter=f'sub = "{sub}"', Limit=1)
        users = resp.get("Users", [])
        return users[0] if users else None


def attr(user: dict, name: str) -> str | None:
    """ListUsers(Attributes) / AdminGetUser·AdminCreateUser(UserAttributes|Attributes) 공용."""
    for a in user.get("Attributes", user.get("UserAttributes", [])) or []:
        if a.get("Name") == name:
            return a.get("Value")
    return None
