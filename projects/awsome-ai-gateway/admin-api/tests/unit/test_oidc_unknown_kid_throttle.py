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
