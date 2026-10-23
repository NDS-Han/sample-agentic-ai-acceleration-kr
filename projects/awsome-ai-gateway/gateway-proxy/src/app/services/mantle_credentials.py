# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import structlog

from botocore.credentials import Credentials

from app.services.credential_backoff import (
    failure_backoff_s,
    reraise_fresh,
)

from app.schemas.routing import RoutingProfileSchema

logger = structlog.get_logger(__name__)

# Lazy import so the module loads even if the dep is missing in some envs;
# bearer_token() raises clearly if it is truly absent.
try:
    from aws_bedrock_token_generator import BedrockTokenGenerator
except Exception:  # pragma: no cover - exercised only when dep absent
    BedrockTokenGenerator = None  # type: ignore[assignment]

# Refresh assumed creds this many seconds BEFORE their hard expiry.
_CRED_REFRESH_SKEW = 300
# Bearer tokens are minted with a 12h SigV4 expiry by the generator; we re-mint
# more often to stay well within and to follow rotated creds.
_BEARER_TTL = 1800  # 30 min
# A bearer must never outlive the assumed creds it was minted from. Cap its
# expiry at (creds_expiry - this skew) so we never serve a bearer against
# expired creds (would 401).
_BEARER_CRED_SKEW = 60  # 1 min grace before creds hard-expire
_ASSUME_DURATION = 3600  # 1h
# 키별 단일비행 + 실패 백오프 — 프로세스 전역 락이 AssumeRole/IRSA 체인을
# 직렬화해, 한 키의 갱신이 무관한 키의 캐시 적중까지 막았다(R22 r1 M3/S7).


@dataclass
class _CachedCreds:
    creds: Credentials
    expires_at: float  # epoch seconds (from STS Expiration)


@dataclass
class _CachedBearer:
    token: str = field(repr=False)  # never expose the bearer in repr/logs
    expires_at: float = 0.0  # epoch seconds from now()


class MantleCredentialBroker:
    """Mint short-lived Mantle bearer tokens, supporting two credential paths.

    - Cross-account (Cowork / 905): ``account_role_arn`` is set → AssumeRole via
      STS to obtain temporary creds, then mint bearer.
    - In-account (Claude Code / 374): ``account_role_arn`` is None → use the
      pod's own IRSA credentials directly (no AssumeRole needed).

    Holds NO long-lived account keys. Caches assumed creds (~1h) and bearer
    tokens (30 min) to avoid per-request STS calls and token minting.
    """

    def __init__(self, sts_client, now: Callable[[], float] = time.time) -> None:
        self._sts = sts_client
        self._now = now
        # cred 키 = (role_arn, external_id) — 같은 롤을 다른 ExternalId 조건으로
        # 참조하는 두 모델/프로필이 생기면(모델 레벨 라우팅 0049) 한쪽 ExternalId
        # 로 딴 세션이 다른 쪽 요청에 재사용되고 실패 백오프도 서로 오염된다.
        self._creds: dict[tuple[str, str], _CachedCreds] = {}
        # Bearer cache is keyed by (cred_key, region): a bearer is region-bound
        # (SigV4 binds to the regional endpoint), so two profiles sharing a role
        # but differing region must not cross-serve tokens. cred_key 자체는
        # (role_arn, external_id) 쌍 — 위와 같은 이유로 ExternalId 도 키다.
        self._bearers: dict[tuple[tuple[str, str], str], _CachedBearer] = {}
        self._lock = asyncio.Lock()
        self._inflight: dict[tuple[tuple[str, str], str], asyncio.Task] = {}
        self._failures: dict[tuple[tuple[str, str], str], tuple[Exception, float]] = {}

    def _in_account_creds(self) -> Credentials:
        """Pod's own IRSA credentials (no AssumeRole) for in-account Mantle (333)."""
        import boto3
        raw = boto3.Session().get_credentials()
        if raw is None:
            raise RuntimeError(
                "No AWS credentials available for in-account Mantle path. "
                "Ensure IRSA is configured on the pod."
            )
        frozen = raw.get_frozen_credentials()
        return Credentials(frozen.access_key, frozen.secret_key, frozen.token)

    async def bearer_token(self, profile: RoutingProfileSchema) -> str:
        if BedrockTokenGenerator is None:
            raise RuntimeError("aws-bedrock-token-generator not installed")

        cred_key = (profile.account_role_arn or "in-account", profile.external_id or "")
        bearer_key = (cred_key, profile.region)
        # 락 없는 빠른 경로 — 캐시 적중/실패 백오프는 락/STS 없이 결정한다.
        now = self._now()
        cached_bearer = self._bearers.get(bearer_key)
        if cached_bearer and cached_bearer.expires_at > now:
            return cached_bearer.token
        failure = self._failures.get(bearer_key)
        if failure and failure[1] > now:
            reraise_fresh(failure[0])

        async with self._lock:
            now = self._now()
            cached_bearer = self._bearers.get(bearer_key)
            if cached_bearer and cached_bearer.expires_at > now:
                return cached_bearer.token
            failure = self._failures.get(bearer_key)
            if failure and failure[1] > now:
                reraise_fresh(failure[0])
            task = self._inflight.get(bearer_key)
            if task is None:
                task = asyncio.get_running_loop().create_task(
                    self._refresh_bearer(bearer_key, profile)
                )
                self._inflight[bearer_key] = task
                task.add_done_callback(
                    lambda _t, k=bearer_key: self._inflight.pop(k, None)
                )
        try:
            # 락 밖에서 공유 await — 무관한 키의 요청을 직렬화하지 않는다.
            # 취소 전파 차단 — 대기자 취소가 공유 refresh task 를 취소하면
            # 모든 대기 요청이 CancelledError 로 쓰러진다(R22 r2 S1).
            return await asyncio.shield(task)
        except Exception as exc:
            self._failures[bearer_key] = (exc, self._now() + failure_backoff_s(exc))
            raise

    async def _refresh_bearer(
        self, bearer_key: tuple[str, str], profile: RoutingProfileSchema
    ) -> str:
        now = self._now()
        if profile.account_role_arn:
            creds = await self._get_creds(profile.account_role_arn, profile.external_id, now)
            cred_expiry = self._creds[
                (profile.account_role_arn, profile.external_id or "")
            ].expires_at
        else:
            creds = await asyncio.get_running_loop().run_in_executor(None, self._in_account_creds)
            cred_expiry = now + _ASSUME_DURATION

        token = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: BedrockTokenGenerator().get_token(creds, profile.region),
        )
        # Never let the bearer outlive the underlying creds it was minted from.
        bearer_expiry = min(now + _BEARER_TTL, cred_expiry - _BEARER_CRED_SKEW)
        self._bearers[bearer_key] = _CachedBearer(token=token, expires_at=bearer_expiry)
        return token

    async def _get_creds(
        self, role_arn: str, external_id: Optional[str], now: float
    ) -> Credentials:
        cred_key = (role_arn, external_id or "")
        cached = self._creds.get(cred_key)
        if cached and cached.expires_at - _CRED_REFRESH_SKEW > now:
            return cached.creds

        kwargs = {
            "RoleArn": role_arn,
            "RoleSessionName": "gw-mantle",
            "DurationSeconds": _ASSUME_DURATION,
        }
        if external_id:
            kwargs["ExternalId"] = external_id

        resp = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._sts.assume_role(**kwargs)
        )
        c = resp["Credentials"]
        creds = Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])
        expires_at = c["Expiration"].timestamp()
        self._creds[cred_key] = _CachedCreds(creds=creds, expires_at=expires_at)
        # invalidate any stale bearer (all regions) minted from older creds
        self._bearers = {k: v for k, v in self._bearers.items() if k[0] != cred_key}
        return creds
