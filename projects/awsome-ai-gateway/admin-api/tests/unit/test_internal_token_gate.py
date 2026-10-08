# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""`/internal/*` 공유 시크릿 게이트 회귀 (R3-1).

배경: admin-api 는 ALB 로 전 경로가 공개된다. `_require_internal_token` 이 없던 시절
`/internal/test/issue-key` 가 dev 공개 도메인에서 무인증 VK 발급기로 동작했다 —
라이브로 실제 VK 발급이 확인됐다. 또 `/internal/cache/retry` 는 무인증으로 누구나
캐시 무효화 재시도를 발동할 수 있었다.

이 파일이 고정하는 것:
  1. 토큰 미설정(기본값 "") → 무조건 403 (fail-closed).
  2. 토큰 설정 + 헤더 불일치/누락 → 403.
  3. 토큰 설정 + 헤더 일치 → 통과.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.core.config import get_settings
from app.routers.internal import _require_internal_token


def _req(token_header: str | None) -> MagicMock:
    req = MagicMock()
    req.headers = {} if token_header is None else {"x-internal-token": token_header}
    return req


@pytest.mark.asyncio
async def test_no_token_configured_always_403(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", "")
    with pytest.raises(HTTPException) as exc:
        await _require_internal_token(_req("anything"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_wrong_or_missing_token_403(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", "s3cret")
    for bad in (None, "", "wrong", "s3cret "):
        with pytest.raises(HTTPException) as exc:
            await _require_internal_token(_req(bad))
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_matching_token_passes(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", "s3cret")
    await _require_internal_token(_req("s3cret"))  # no raise
