# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""productivity 라우터 인증 게이트 회귀 (R4-A1-2).

배경: `routers/productivity.py` 는 `routers/internal.py` 와 **별도** APIRouter 라
R3-1 의 `_require_internal_token` 게이트가 적용되지 않았다 — `/internal/productivity`
·`/webhooks/git` 이 공개 ALB 에 무인증으로 열려 임의의 productivity/git 이벤트를
DB 에 위조 적재할 수 있었다(TestClient 로 무인증 200 실측).

이 파일이 고정하는 것:
  1. `POST /internal/productivity` 에 `_require_internal_token` 의존이 붙어 있다.
  2. `POST /webhooks/git` 에 GitHub `X-Hub-Signature-256` HMAC 검증이 붙어 있다.
  3. `GITHUB_WEBHOOK_SECRET` 미설정 → 403 (fail-closed).
  4. 잘못된 서명 → 403, 정확한 서명 → 통과.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.core.config import get_settings
from app.routers import productivity
from app.routers.internal import _require_internal_token


def _route(path: str) -> APIRoute:
    for r in productivity.router.routes:
        if isinstance(r, APIRoute) and r.path == path:
            return r
    raise AssertionError(f"route {path} not found")


def _deps(route: APIRoute):
    return [d.call for d in route.dependant.dependencies]


class TestRouteWiring:
    def test_internal_productivity_requires_internal_token(self):
        assert _require_internal_token in _deps(_route("/internal/productivity"))

    def test_git_webhook_requires_signature(self):
        assert productivity._verify_github_signature in _deps(_route("/webhooks/git"))


class TestGithubSignature:
    def _req(self, body: bytes, signature: str | None) -> MagicMock:
        req = MagicMock()
        req.body = AsyncMock(return_value=body)
        req.headers = {} if signature is None else {"x-hub-signature-256": signature}
        return req

    @pytest.mark.asyncio
    async def test_no_secret_fails_closed(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "GITHUB_WEBHOOK_SECRET", "")
        with pytest.raises(HTTPException) as exc:
            await productivity._verify_github_signature(self._req(b"{}", "sha256=x"))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_bad_signature_403(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "GITHUB_WEBHOOK_SECRET", "whsec")
        body = b'{"repo":"x"}'
        for bad in (None, "", "sha256=deadbeef", "deadbeef"):
            with pytest.raises(HTTPException) as exc:
                await productivity._verify_github_signature(self._req(body, bad))
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_valid_signature_passes(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "GITHUB_WEBHOOK_SECRET", "whsec")
        body = b'{"repo":"x"}'
        sig = "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
        await productivity._verify_github_signature(self._req(body, sig))  # no raise
