# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OIDC unknown-kid JWKS 강제 리페치 스로틀 회귀 (R3-6).

배경: `/v1/auth/exchange` 는 무인증이고 `kid` 는 JWT 헤더라 공격자가 고른다.
예전엔 unknown kid 마다 `_ensure_jwks_async(force=True)` 로 IdP 에 JWKS GET 이
나가 — 요청 1건 = IdP GET 1건의 증폭 DoS 경로였다.

고정하는 것: 강제 리페치는 ``_force_refresh_min_interval``(30s)당 최대 1회.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.core.oidc_verifier import OIDCVerifier, OIDCVerifyError

ISSUER = "https://example-idp.test/realm"


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def _token(kid: str) -> str:
    return f'{_b64({"alg": "RS256", "kid": kid, "typ": "JWT"})}.{_b64({"iss": ISSUER, "sub": "u1"})}.sig'


class _FastIdP:
    """JWKS 를 즉시 돌려주고 요청 수를 센다."""

    def __init__(self) -> None:
        self.jwks_hits = 0

    async def __call__(self, scope, receive, send):
        path = scope["path"]
        if path.endswith("/.well-known/openid-configuration"):
            body = (b'{"issuer": "' + ISSUER.encode() + b'",'
                    b' "jwks_uri": "' + ISSUER.encode() + b'/keys"}')
        elif path.endswith("/keys"):
            self.jwks_hits += 1
            body = (b'{"keys": [{"kid": "k1", "use": "sig", "kty": "RSA", "alg": "RS256",'
                    b' "n": "AQAB", "e": "AQAB"}]}')
        else:
            body = b"{}"
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


@pytest.mark.asyncio
async def test_unknown_kids_do_not_amplify_jwks_fetches():
    """임의 kid 5개는 JWKS 를 최대 1+1회(최초 로드 + 최초 unknown)만 가져온다."""
    idp = _FastIdP()
    verifier = OIDCVerifier(issuer_url=ISSUER, audience=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=idp)) as client:
        for i in range(5):
            with pytest.raises(OIDCVerifyError):
                await verifier.verify_async(_token(f"attacker-{i}"), client)

    # 5개의 서로 다른 unknown kid 가 JWKS 를 최대 2회(초기 + 1회 강제)만 가져온다.
    assert idp.jwks_hits <= 2, f"JWKS fetch 증폭: {idp.jwks_hits} hits"


class _DownIdP:
    """JWKS 엔드포인트가 항상 500 — IdP 장애 시나리오."""

    def __init__(self) -> None:
        self.jwks_hits = 0

    async def __call__(self, scope, receive, send):
        path = scope["path"]
        if path.endswith("/.well-known/openid-configuration"):
            body = (b'{"issuer": "' + ISSUER.encode() + b'",'
                    b' "jwks_uri": "' + ISSUER.encode() + b'/keys"}')
            status = 200
        elif path.endswith("/keys"):
            self.jwks_hits += 1
            body, status = b'{"keys": []}', 500
        else:
            body, status = b"{}", 404
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


@pytest.mark.asyncio
async def test_unknown_kid_throttled_even_when_idp_down():
    """R4-A2-4: IdP 장애 시에도 리페치는 최소 간격당 1회(negative cache).

    이전엔 `_jwks_fetched_at` 이 **성공 시각**만 기록해서, 장애 + 마지막 성공이
    30s 경과 상태면 매 unknown-kid 요청이 다운 IdP 로 GET 을 날렸다.
    """
    import time

    idp = _DownIdP()
    verifier = OIDCVerifier(issuer_url=ISSUER, audience=None)
    # JWKS 가 이전에 정상 로드된 상태 — step-2 의 TTL 캐시가 아직 유효하도록.
    verifier._jwks_keys = {"k1": {"kid": "k1", "use": "sig", "kty": "RSA"}}
    verifier._jwks_fetched_at = time.monotonic()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=idp)) as client:
        for i in range(6):
            with pytest.raises(OIDCVerifyError):
                await verifier.verify_async(_token(f"attacker-{i}"), client)

    # 실패해도 시도 시각이 기록되므로 리페치는 최대 1회 — 증폭 없음.
    assert idp.jwks_hits <= 1, (
        f"IdP 장애 중 JWKS fetch 증폭: {idp.jwks_hits} hits"
    )
