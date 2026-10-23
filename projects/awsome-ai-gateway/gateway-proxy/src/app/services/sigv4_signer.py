# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""SigV4 request signer for the Bedrock **runtime** OpenAI wires (httpx, not boto3).

Why this exists at all: ``bedrock-runtime`` serves the OpenAI dialect at
``/openai/v1/responses`` and ``/openai/v1/chat/completions`` as plain HTTPS — those paths
are NOT botocore operations, so boto3 cannot call them and the request must be signed by
hand (verified live 2026-09-03: SigV4 service name ``bedrock``, both paths return 200 and
a real SSE stream). Everything else on the Bedrock runtime plane (InvokeModel/Converse)
keeps using ``providers/bedrock_adapter.py`` + boto3.

Two credential paths, mirroring :class:`~app.services.mantle_credentials.MantleCredentialBroker`:

* in-account (``role_arn`` is None) — the pod's own IRSA credentials.
* cross-account (``role_arn`` set) — STS AssumeRole, creds cached ~1h.

It vends SIGNATURES where the Mantle broker vends BEARER TOKENS; the duplication of the
assume+cache block is deliberate. Folding both into one broker would mean editing the
live Cowork/Codex bearer path to add a runtime-plane feature, and a regression there takes
down two shipped clients. The two caches are also keyed differently: a bearer is bound to
(creds, region) and reusable for 30 min, while a signature is per-request (it covers the
body hash) so only the CREDENTIALS are worth caching here.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional

import structlog

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from app.services.credential_backoff import (
    failure_backoff_s,
    reraise_fresh,
)

logger = structlog.get_logger(__name__)

_ASSUME_DURATION = 3600      # 1h STS session (same as the Mantle broker)
_CRED_REFRESH_SKEW = 300     # refresh this many seconds before hard expiry


@dataclass
class _CachedCreds:
    creds: Credentials
    expires_at: float  # epoch seconds (from STS Expiration)


class SigV4Signer:
    """Sign bedrock-runtime HTTPS requests, in-account or cross-account.

    ``sts_client`` may be None when only the in-account path is used (then a
    cross-account call raises rather than silently signing with the pod's own identity,
    which would bill the wrong account and hide a misconfiguration).
    """

    def __init__(
        self,
        sts_client=None,
        service_name: str = "bedrock",
        now: Callable[[], float] = time.time,
    ) -> None:
        self._sts = sts_client
        self._service = service_name
        self._now = now
        # 키 = (role_arn, external_id) — 같은 롤을 다른 ExternalId 조건으로
        # 참조하는 두 모델/프로필이 생기면(모델 레벨 라우팅 0049) 한쪽의 assume
        # 실패 백오프가 다른 쪽 호출을 막고, 한쪽 ExternalId 로 딴 세션이 다른
        # 쪽 요청에 재사용된다. 자격증명 자체는 리전 무관이라 키에 region 은 없다.
        self._creds: dict[tuple[str, str], _CachedCreds] = {}
        self._lock = asyncio.Lock()
        # 키별 단일비행 + 실패 백오프 — 전역 락 안에서 AssumeRole 하던 구조는
        # trust/ExternalId 장애 시 큐잉된 요청 전체를 직렬 재시도로 묶었다
        # (R22 r1 M3). 락은 캐시 판정·태스크 등록에만 쓴다.
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}
        self._failures: dict[tuple[str, str], tuple[Exception, float]] = {}
        # One long-lived botocore session for the in-account path. It owns the
        # refreshable credential object, so building it ONCE is what makes the IRSA
        # web-identity session reusable; a per-request boto3.Session() would re-run the
        # whole credential chain (token-file read + AssumeRoleWithWebIdentity) on every
        # single request.
        self._session = None

    async def sign(
        self,
        *,
        method: str,
        url: str,
        body: bytes,
        region: str,
        headers: Optional[dict] = None,
        role_arn: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> dict:
        """Return the headers to send, including Authorization/X-Amz-Date/security token.

        The signature covers the exact ``body`` bytes and the host implied by ``url``, so
        the caller MUST send both verbatim (no re-serialising the JSON, no redirect
        following) or the service answers 403 SignatureDoesNotMatch.
        """
        creds = await self._resolve_creds(role_arn, external_id)
        base = {"content-type": "application/json", **(headers or {})}
        request = AWSRequest(method=method, url=url, data=body, headers=base)
        # SigV4Auth is pure CPU (hmac over the canonical request) — no I/O, safe inline.
        SigV4Auth(creds, self._service, region).add_auth(request)
        return dict(request.headers)

    async def _resolve_creds(
        self, role_arn: Optional[str], external_id: Optional[str]
    ) -> Credentials:
        if not role_arn:
            return await asyncio.get_running_loop().run_in_executor(
                None, self._in_account_creds
            )
        key = (role_arn, external_id or "")
        now = self._now()
        cached = self._creds.get(key)
        if cached and cached.expires_at - _CRED_REFRESH_SKEW > now:
            return cached.creds
        failure = self._failures.get(key)
        if failure and failure[1] > now:
            reraise_fresh(failure[0])

        async with self._lock:
            now = self._now()
            cached = self._creds.get(key)
            if cached and cached.expires_at - _CRED_REFRESH_SKEW > now:
                return cached.creds
            failure = self._failures.get(key)
            if failure and failure[1] > now:
                reraise_fresh(failure[0])
            if self._sts is None:
                raise RuntimeError(
                    "SigV4Signer has no STS client; cannot assume "
                    f"{role_arn} for the cross-account runtime path"
                )
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.get_running_loop().create_task(
                    self._refresh(role_arn, external_id)
                )
                self._inflight[key] = task
                task.add_done_callback(
                    lambda _t, k=key: self._inflight.pop(k, None)
                )
        try:
            # 취소 전파 차단 — 대기자 취소가 공유 refresh task 를 취소하면
            # 모든 대기 요청이 CancelledError 로 쓰러진다(R22 r2 S1).
            return await asyncio.shield(task)
        except Exception as exc:
            self._failures[key] = (exc, self._now() + failure_backoff_s(exc))
            raise

    async def _refresh(
        self, role_arn: str, external_id: Optional[str]
    ) -> Credentials:
        creds, expires_at = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._assume(role_arn, external_id)
        )
        self._creds[(role_arn, external_id or "")] = _CachedCreds(
            creds=creds, expires_at=expires_at
        )
        logger.info("sigv4_xacct_creds_assumed", role_arn=role_arn)
        return creds

    def _assume(self, role_arn: str, external_id: Optional[str]) -> tuple[Credentials, float]:
        kwargs: dict = {
            "RoleArn": role_arn,
            "RoleSessionName": "gw-bedrock-openai",
            "DurationSeconds": _ASSUME_DURATION,
        }
        if external_id:
            kwargs["ExternalId"] = external_id
        resp = self._sts.assume_role(**kwargs)
        c = resp["Credentials"]
        return (
            Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"]),
            c["Expiration"].timestamp(),
        )

    def _in_account_creds(self) -> Credentials:
        """The pod's own IRSA credentials.

        ``get_frozen_credentials()`` is what refreshes an expiring IRSA web-identity
        session, and it can block on a token-file read plus an STS call — hence the
        executor hop in :meth:`_resolve_creds`. The frozen copy is deliberately NOT
        cached: botocore's session-owned credential object already caches and refreshes,
        and holding the frozen copy would pin an expired session.
        """
        import boto3

        if self._session is None:
            self._session = boto3.Session()
        raw = self._session.get_credentials()
        if raw is None:
            raise RuntimeError(
                "No AWS credentials available for the in-account Bedrock runtime path. "
                "Ensure IRSA is configured on the pod."
            )
        return raw.get_frozen_credentials()
