# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""R26 L2 — /v1/chat/completions, /v1/completions 의 model ref 가드 회귀.

_handle_openai 는 프로파일 폴백이 없어 /v1/responses 와 달리 unusable ref 가
resolver 의 LookupError 밖 예외(NUL → asyncpg CharacterNotInRepertoireError,
서로게이트 → redis-py UnicodeEncodeError, 비문자열 → DBAPIError)로 500 이 났다.
모듈 레벨 _is_usable_model_ref 가드가 400 으로 닫는다.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.routers.openai_compat import _handle_openai


class _FakeRequest:
    def __init__(self, body: dict, state: dict) -> None:
        self._body = json.dumps(body).encode()
        self.scope = {"state": state}
        self.app = MagicMock()
        self.app.state = MagicMock()

    async def body(self) -> bytes:
        return self._body

    async def is_disconnected(self) -> bool:
        return False


def _request(model_value) -> _FakeRequest:
    return _FakeRequest(
        {"model": model_value, "messages": [{"role": "user", "content": "hi"}]},
        {"auth_context": None, "_redis": None, "_session_factory": None},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_model",
    ["codex\x00gpt", "\x00", "a\ud800b", "", None, 123, ["a"], {"x": 1}, True],
    ids=["nul-mid", "nul-only", "surrogate", "empty", "null", "int", "list", "dict", "bool"],
)
async def test_unusable_model_ref_returns_400_not_500(monkeypatch, bad_model):
    reached: list = []

    async def _resolve(redis, db, model_ref):
        reached.append(model_ref)
        raise LookupError(model_ref)

    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_openai_model", _resolve
    )

    resp = await _handle_openai(_request(bad_model), "/v1/chat/completions")

    assert resp.status_code == 400
    assert reached == [], "unusable ref must never reach the resolver"


@pytest.mark.asyncio
async def test_valid_model_ref_still_reaches_resolver(monkeypatch):
    reached: list = []

    async def _resolve(redis, db, model_ref):
        reached.append(model_ref)
        raise LookupError(f"Model alias '{model_ref}' not found")

    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_openai_model", _resolve
    )

    resp = await _handle_openai(_request("gpt-4o"), "/v1/chat/completions")

    assert reached == ["gpt-4o"]
    assert resp.status_code == 404  # 미등록 alias 는 기존과 동일하게 404
