# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""/aicm/v1 mutation 의 Idempotency-Key 처리 (migration 0065).

규약
----
- 모든 mutation 은 ``Idempotency-Key`` 헤더(1–128자, 출력 가능한 ASCII)가 필수다 → 없으면 400.
- 키 범위는 ``(integration, key)``. 요청 해시 = method + path + 정렬 query + 정규화 JSON 본문.
- 선점: 요청 트랜잭션 안에서 ``INSERT … ON CONFLICT DO NOTHING``(진행 중 = status_code NULL).
  같은 키의 동시 요청은 PK 행 잠금에서 기다렸다가 결과를 본다.
- 같은 해시로 재전송 → 저장된 응답을 그대로(새 감사 행 없음, ``Idempotent-Replayed: true``).
  다른 해시 → 422 ``idempotency_key_reused``. 아직 진행 중 → 409 ``idempotency_in_progress``.
- 2xx 응답만 저장한다. 실패하면 선점 행을 지워 같은 키로 다시 시도할 수 있다.
- 비밀값(rotate 의 새 토큰 원문)은 저장하지 않는다 — 재전송은 409 ``idempotency_secret_not_replayable``.
- 보존: ``AICM_IDEMPOTENCY_RETENTION_DAYS``. 새 키를 기록할 때 같은 통합의 만료 행을 최대 100개 지운다.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import IntegrationError

logger = structlog.get_logger()

_MAX_KEY_LEN = 128
SECRET_WITHHELD = "_secret_withheld"

_INSERT = text(
    """
    INSERT INTO auth.integration_idempotency_keys
        (integration, idem_key, method, path, request_sha256)
    VALUES (:i, :k, :m, :p, :h)
    ON CONFLICT (integration, idem_key) DO NOTHING
    RETURNING 1
    """
)
_SELECT = text(
    """
    SELECT request_sha256, status_code, response,
           created_at < now() - make_interval(mins => :stale) AS stale
      FROM auth.integration_idempotency_keys
     WHERE integration = :i AND idem_key = :k
    """
)
#: 진행 중 행이 이보다 오래되면 버려진 것으로 본다(요청은 그 전에 끝나거나 타임아웃된다).
_STALE_MINUTES = 5
_COMPLETE = text(
    """
    UPDATE auth.integration_idempotency_keys
       SET status_code = :s, response = CAST(:r AS jsonb), completed_at = now()
     WHERE integration = :i AND idem_key = :k
    """
)
_RELEASE = text(
    """
    DELETE FROM auth.integration_idempotency_keys
     WHERE integration = :i AND idem_key = :k AND status_code IS NULL
    """
)
_PURGE = text(
    """
    DELETE FROM auth.integration_idempotency_keys
     WHERE ctid IN (
        SELECT ctid FROM auth.integration_idempotency_keys
         WHERE integration = :i AND created_at < now() - make_interval(days => :d)
         LIMIT 100)
    """
)


def idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if key is None or not key.strip():
        raise IntegrationError(
            "Idempotency-Key header is required for this operation",
            code="idempotency_key_required", status_code=400,
        )
    key = key.strip()
    if len(key) > _MAX_KEY_LEN or not all(32 < ord(c) < 127 for c in key):
        raise IntegrationError(
            f"Idempotency-Key must be 1-{_MAX_KEY_LEN} printable ASCII characters without spaces",
            code="idempotency_key_invalid", status_code=400,
        )
    return key


async def request_fingerprint(request: Request) -> str:
    raw = await request.body()
    body: Any
    try:
        body = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")) if raw else ""
    except ValueError:
        body = raw.decode("utf-8", "replace")
    query = "&".join(f"{k}={v}" for k, v in sorted(request.query_params.multi_items()))
    material = "\n".join([request.method, request.url.path, query, body])
    return hashlib.sha256(material.encode()).hexdigest()


#: handler() → (status_code, body) 또는 (status_code, body, after_commit).
#: after_commit 이 있으면 결과 기록과 업무 변경을 **한 번에** 커밋한 뒤 호출한다
#: (예: VK 폐기의 Redis DEL·pub/sub — 커밋 전 실행은 재캐시 경합을 만든다).
Handler = Callable[[], Awaitable[tuple]]


async def run_idempotent(
    request: Request,
    session: AsyncSession,
    *,
    integration: str,
    handler: Handler,
    secret_fields: tuple[str, ...] = (),
) -> Response:
    """handler() → (status_code, body). body 는 JSON 직렬화 가능한 값 또는 None(204)."""
    key = idempotency_key(request)
    fp = await request_fingerprint(request)
    params = {"i": integration, "k": key}

    inserted = (
        await session.execute(
            _INSERT, {**params, "m": request.method, "p": request.url.path[:512], "h": fp}
        )
    ).first()
    if inserted is None:
        row = (await session.execute(_SELECT, {**params, "stale": _STALE_MINUTES})).first()
        if row is None:  # 선점 행이 방금 해제됨(앞선 시도 실패) — 재시도하게 한다.
            raise IntegrationError(
                "a previous request with this Idempotency-Key just failed; retry",
                code="idempotency_in_progress", status_code=409, retry_after=1,
            )
        stored_fp, status_code, stored, stale = row
        if stored_fp != fp:
            raise IntegrationError(
                "Idempotency-Key was already used with a different request",
                code="idempotency_key_reused", status_code=422,
            )
        if status_code is None:
            if stale:
                # 프로세스 중단 등으로 남은 선점 행 — 지우고 재시도하게 한다.
                await session.execute(_RELEASE, params)
                await session.commit()
            raise IntegrationError(
                "a request with this Idempotency-Key is still in progress",
                code="idempotency_in_progress", status_code=409, retry_after=1,
            )
        if isinstance(stored, dict) and stored.get(SECRET_WITHHELD):
            raise IntegrationError(
                "the original response contained a secret that is not stored; "
                "the operation already succeeded",
                code="idempotency_secret_not_replayable", status_code=409,
                details={k: v for k, v in stored.items() if k != SECRET_WITHHELD},
            )
        return _response(status_code, stored, replayed=True)

    await session.execute(
        _PURGE, {"i": integration, "d": int(get_settings().AICM_IDEMPOTENCY_RETENTION_DAYS)}
    )

    try:
        out = await handler()
    except Exception:
        # 실패 — 선점 행을 지워 같은 키로 재시도할 수 있게 한다. 핸들러가 중간에 커밋했으면
        # (sync_user 는 스스로 커밋한다) 행이 이미 커밋돼 있으므로 롤백만으로는 부족하다.
        try:
            await session.rollback()
            await session.execute(_RELEASE, params)
            await session.commit()
        except Exception:
            logger.warning("aicm.idempotency_release_failed", key=key, exc_info=True)
        raise

    status_code, body = out[0], out[1]
    after_commit = out[2] if len(out) > 2 else None
    encoded = jsonable_encoder(body) if body is not None else None
    stored = encoded
    if secret_fields and isinstance(encoded, dict):
        stored = {k: v for k, v in encoded.items() if k not in secret_fields}
        stored[SECRET_WITHHELD] = True
    await session.execute(
        _COMPLETE,
        {**params, "s": status_code, "r": json.dumps(stored) if stored is not None else None},
    )
    if after_commit is not None:
        await session.commit()
        try:
            await after_commit()
        except Exception:
            logger.warning("aicm.after_commit_failed", path=request.url.path, exc_info=True)
    return _response(status_code, encoded, replayed=False)


def _response(status_code: int, body: Any, *, replayed: bool) -> Response:
    headers = {"Idempotent-Replayed": "true" if replayed else "false"}
    if status_code == 204 or body is None:
        return Response(status_code=status_code, headers=headers)
    return JSONResponse(status_code=status_code, content=body, headers=headers)
