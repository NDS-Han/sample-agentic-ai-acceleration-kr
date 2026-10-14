# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""R16 — 미들웨어/본문로깅 회귀 테스트.

커버:
- MaxBodySizeMiddleware — Content-Length 초과 시 본문을 읽기 전에 413.
- AuthMiddleware — auth_context 가 채워진 뒤(inner app) 일어난 예외는
  자격증명 거부(401+security 이벤트)로 위조하지 않고 재전파한다.
- JWTAuthStrategy — 위조 kid 로 오는 연속 캐시 미스는 DB 전체 키 스캔을
  스로틀한다(무인증 DoS 증폭 차단).
- BodyLogger — 큐가 레코드 수가 아니라 **바이트**로도 상한을 지킨다
  (대형 프롬프트 × 느린 flush = pod OOM 방지).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


# ── MaxBodySizeMiddleware ────────────────────────────────────────────────


async def test_max_body_size_rejects_oversized():
    from app.middleware.max_body_size import MaxBodySizeMiddleware

    reached = []

    async def app(scope, receive, send):
        reached.append(True)

    mw = MaxBodySizeMiddleware(app, max_bytes=100)
    scope = {
        "type": "http",
        "path": "/v1/messages",
        "headers": [(b"content-length", b"500")],
    }
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"x" * 500}

    await mw(scope, receive, send)
    assert not reached, "inner app reached despite oversized body"
    assert sent[0]["status"] == 413


async def test_max_body_size_allows_within_limit():
    from app.middleware.max_body_size import MaxBodySizeMiddleware

    reached = []

    async def app(scope, receive, send):
        reached.append(True)

    mw = MaxBodySizeMiddleware(app, max_bytes=100)
    scope = {
        "type": "http",
        "path": "/v1/messages",
        "headers": [(b"content-length", b"50")],
    }

    async def send(msg):
        pass

    async def receive():
        return {"type": "http.request", "body": b"x" * 50}

    await mw(scope, receive, send)
    assert reached


async def test_max_body_size_chunked_stream_capped():
    """Content-Length 없는 chunked 전송도 누적 상한을 넘으면 413 으로 끊는다 —
    헤더 비교만으로는 Transfer-Encoding 우회를 못 잡는다(R16 V-3)."""
    from app.middleware.max_body_size import MaxBodySizeMiddleware
    from starlette.requests import ClientDisconnect

    reached_body = []

    async def app(scope, receive, send):
        # 스트리밍 수신 시뮬레이션 — disconnect 를 만나면 멈춘다.
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                reached_body.append(len(msg.get("body") or b""))
            if msg["type"] == "http.disconnect":
                raise ClientDisconnect()

    mw = MaxBodySizeMiddleware(app, max_bytes=100)
    scope = {
        "type": "http",
        "path": "/v1/messages",
        "headers": [],  # content-length 없음 — chunked 우회 경로
    }
    sent = []
    chunks = [
        {"type": "http.request", "body": b"x" * 60, "more_body": True},
        {"type": "http.request", "body": b"x" * 60, "more_body": False},
    ]

    async def send(msg):
        sent.append(msg)

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    await mw(scope, receive, send)
    # 60+60=120 > 100 — 두 번째 청크가 넘지 못하고 413 이 나간다.
    assert sent and sent[0]["status"] == 413
    assert reached_body == [60], "상한 초과 청크가 앱에 전달됐다"


async def test_max_body_size_ignores_non_http():
    from app.middleware.max_body_size import MaxBodySizeMiddleware

    reached = []

    async def app(scope, receive, send):
        reached.append(True)

    mw = MaxBodySizeMiddleware(app, max_bytes=1)
    scope = {"type": "websocket", "headers": []}

    async def send(msg):
        pass

    async def receive():
        return {"type": "websocket.connect"}

    await mw(scope, receive, send)
    assert reached


# ── AuthMiddleware: downstream exceptions are not forged into 401s ───────


async def test_downstream_value_error_reraised_not_401(monkeypatch):
    """auth_context 이후(inner app)의 예외는 401+security 이벤트로 위조 금지."""
    from app.middleware.auth import AuthMiddleware
    import app.middleware.auth as auth_mod

    async def buggy_app(scope, receive, send):
        raise ValueError("router bug")

    strategy = MagicMock()
    strategy.authenticate = AsyncMock(
        return_value=MagicMock(user_id="u", sso_subject="s")
    )
    monkeypatch.setattr(auth_mod, "resolve_auth_strategy", lambda path: strategy)
    mw = AuthMiddleware(buggy_app)

    scope = {
        "type": "http",
        "path": "/v1/models",
        "method": "GET",
        "headers": [(b"authorization", b"Bearer fake")],
        "state": {},
    }
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    with pytest.raises(ValueError, match="router bug"):
        await mw(scope, receive, send)
    # 401 응답이 나가면 안 된다 — 버그는 401 로 위장하면 안 된다.
    assert not any(m.get("status") == 401 for m in sent if "status" in m)


async def test_authenticate_value_error_still_401(monkeypatch):
    """strategy.authenticate 단계의 거부는 여전히 401 이어야 한다."""
    from app.middleware.auth import AuthMiddleware
    import app.middleware.auth as auth_mod

    reached = []

    async def app(scope, receive, send):
        reached.append(True)

    strategy = MagicMock()
    strategy.authenticate = AsyncMock(
        side_effect=ValueError("malformed token")
    )
    monkeypatch.setattr(auth_mod, "resolve_auth_strategy", lambda path: strategy)
    mw = AuthMiddleware(app)

    scope = {
        "type": "http",
        "path": "/v1/models",
        "method": "GET",
        "headers": [(b"authorization", b"Bearer fake")],
        "state": {},
    }
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await mw(scope, receive, send)
    assert not reached
    assert any(m.get("status") == 401 for m in sent if "status" in m)


# ── JWTAuthStrategy: 위조 kid 캐시 미스의 DB 스캔 스로틀 ──────────────────


def _jwt_token_with_kid(kid: str) -> str:
    import base64

    def _b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{_b64({'alg': 'RS256', 'kid': kid})}.{_b64({'sub': 'x', 'exp': 9999999999})}.sig"


def _dict_redis():
    """get/setex 가 실제 저장되는 최소 Redis 흉내 — keyset 캐시 계약 검증용."""
    store: dict = {}
    redis = MagicMock()

    async def _get(k):
        return store.get(k)

    async def _setex(k, _ttl, v):
        store[k] = v

    redis.get = AsyncMock(side_effect=_get)
    redis.setex = AsyncMock(side_effect=_setex)
    return redis


async def test_jwt_keyset_cache_bounds_db_scans():
    """활성 키 keyset 캐시가 DB 스캔을 TTL 당 1회로 제한한다 — 위조 kid 가
    매번 달라도(프로세스 스로틀 우회 기법) DB 는 재접촉되지 않는다."""
    from app.services.auth_service import JWTAuthStrategy

    strategy = JWTAuthStrategy()
    redis = _dict_redis()
    db = AsyncMock()
    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=empty_result)

    for kid in ("evil-1", "evil-2", "evil-3"):
        with pytest.raises(PermissionError):
            await strategy.authenticate(
                f"Bearer {_jwt_token_with_kid(kid)}", redis, db
            )
    assert db.execute.await_count == 1, "keyset 캐시 미적용 — kid 당 DB 스캔"


async def test_forged_kid_does_not_lock_out_valid_jwt():
    """위조 kid 의 캐시 미스 직후에도 정상 JWT 는 keyset 캐시로 검증된다 —
    과거 전역 타임스탬프 스로틀은 이 상황에서 정상 사용자를 401 lockout 시켰다."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    import jwt as pyjwt
    from app.services.auth_service import JWTAuthStrategy

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    key_row = MagicMock()
    key_row.public_key_pem = pub_pem.decode()
    key_row.algorithm = "RS256"
    key_row.issuer = "test-iss"
    key_row.audience = "test-aud"

    def _scalars(rows):
        r = MagicMock()
        r.scalars.return_value.all.return_value = rows
        return r

    db = AsyncMock()

    async def _exec(stmt, *a, **kw):
        s = str(stmt).lower()
        if "jwt_public_keys" in s or "jwt" in s and "select" in s:
            return _scalars([key_row])
        if "allowed" in s:
            return _scalars([])
        # users 등 나머지 조회 — 검증 이후 컨텍스트 빌드 경로
        r = MagicMock()
        row = MagicMock()
        row.is_active = True
        row.team_id = "t1"
        r.first.return_value = row
        return r

    db.execute = AsyncMock(side_effect=_exec)

    strategy = JWTAuthStrategy()
    redis = _dict_redis()

    # 1) 위조 kid — keyset 캐시가 채워지지만 서명 불일치로 401.
    with pytest.raises(PermissionError):
        await strategy.authenticate(
            f"Bearer {_jwt_token_with_kid('evil')}", redis, db
        )

    # 2) 바로 다음 정상 토큰 — keyset 캐시 히트로 검증 통과(lockout 없음).
    import time as _t

    token = pyjwt.encode(
        {
            "user_id": "u1",
            "team_id": "t1",
            "dept_id": "d1",
            "roles": ["USER"],
            "iss": "test-iss",
            "aud": "test-aud",
            "exp": int(_t.time()) + 3600,
        },
        priv_pem,
        algorithm="RS256",
        headers={"kid": "real-kid"},
    )
    auth = await strategy.authenticate(f"Bearer {token}", redis, db)
    assert auth.user_id == "u1"
    # DB 스캔은 keyset 채울 때 1회뿐 — 정상 토큰은 캐시로 검증됐다.
    jwt_queries = sum(
        1 for c in db.execute.await_args_list if "jwt" in str(c.args[0]).lower()
    )
    assert jwt_queries <= 1


# ── BodyLogger: 바이트 상한 ──────────────────────────────────────────────


def _record(n: int):
    from app.schemas.body_log import BodyLogRecord

    return BodyLogRecord.make(
        request_id=f"r-{n}",
        provider="bedrock-runtime",
        client="claude-code",
        model_alias="m",
        status="success",
        is_streaming=False,
        request_body={"prompt": "x" * 200},
        response_body={"out": "y" * 200},
    )


async def test_body_logger_byte_cap_drops_oldest():
    """큐 바이트 상한 초과 시 가장 오래된 레코드부터 버린다(개수 상한만으로는
    대형 프롬프트의 무한 메모리 증가를 못 막는다 — R16)."""
    from app.services.body_logger import BodyLogger

    bl = BodyLogger(
        firehose_client=MagicMock(),
        stream_name="s",
        bucket="b",
        max_queue=10_000,
        max_queue_bytes=3_000,
    )

    # ~1.4KB 레코드 3개 = ~4.2KB > 3KB 상한 → 첫 레코드가 evict 돼야 한다.
    big = {"prompt": "x" * 1200}
    from app.schemas.body_log import BodyLogRecord

    def _big(n: int):
        return BodyLogRecord.make(
            request_id=f"r-{n}",
            provider="bedrock-runtime",
            client="claude-code",
            model_alias="m",
            status="success",
            is_streaming=False,
            request_body=big,
            response_body=big,
        )

    assert await bl.enqueue(_big(1))
    assert await bl.enqueue(_big(2))
    assert await bl.enqueue(_big(3))
    assert bl.queued_bytes <= 3_000, "byte cap not enforced"
    assert bl.dropped >= 1
    # 최신 레코드는 반드시 남는다(drop-oldest 정책).
    # 큐 아이템은 (request_id, partition_key, bytes) — 레코드 객체는 해제된다.
    remaining = [request_id for request_id, _, _ in bl._drain_batch()]
    assert remaining and remaining[-1] == "r-3"


async def test_body_logger_oversized_record_dropped():
    """레코드 하나가 상한 자체보다 크면 큐를 비우지 않고 그 레코드만 버린다."""
    from app.services.body_logger import BodyLogger

    bl = BodyLogger(
        firehose_client=MagicMock(),
        stream_name="s",
        bucket="b",
        max_queue_bytes=10,
    )
    ok = await bl.enqueue(_record(1))
    assert ok is False
    assert bl.queue_size == 0
    assert bl.dropped == 1


async def test_body_logger_queued_bytes_accounting():
    """enqueue/dequeue 쌍으로 바이트 회계가 일치해야 한다."""
    from app.services.body_logger import BodyLogger

    bl = BodyLogger(
        firehose_client=MagicMock(),
        stream_name="s",
        bucket="b",
        max_queue_bytes=1_000_000,
        batch_size=2,
    )
    await bl.enqueue(_record(1))
    await bl.enqueue(_record(2))
    total = bl.queued_bytes
    assert total > 0

    batch = bl._drain_batch()
    assert len(batch) == 2
    assert bl.queued_bytes == 0


async def test_max_body_size_no_double_response_start():
    """앱이 response.start 를 먼저 보낸 뒤 본문을 읽다 상한을 넘어도 두 번째
    http.response.start 는 나가지 않는다 — 프로토콜 위반으로 연결이 깨지는
    것보다 시작된 응답을 종결 body 로 닫는 쪽이 안전하다(R16 V-6)."""
    from app.middleware.max_body_size import MaxBodySizeMiddleware
    from starlette.requests import ClientDisconnect

    async def app(scope, receive, send):
        # 응답을 먼저 시작하고 본문을 스트리밍 수신 — 드물지만 가능한 순서.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                raise ClientDisconnect()

    mw = MaxBodySizeMiddleware(app, max_bytes=100)
    scope = {"type": "http", "path": "/v1/messages", "headers": []}
    sent = []
    chunks = [
        {"type": "http.request", "body": b"x" * 60, "more_body": True},
        {"type": "http.request", "body": b"x" * 60, "more_body": False},
    ]

    async def send(msg):
        sent.append(msg)

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    await mw(scope, receive, send)

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1, "response.start 가 두 번 나갔다(프로토콜 위반)"
    assert starts[0]["status"] == 200  # 이미 시작된 응답 — 상태 교체 불가
    # 마지막은 종결 body — 응답이 hang 하지 않는다.
    assert sent[-1] == {
        "type": "http.response.body",
        "body": b"",
        "more_body": False,
    }


async def test_jwt_local_keyset_survives_db_outage():
    """Redis+DB 둘 다 다운도 한 번 조회된 keyset(로컬 사본)으로 정상 JWT 는
    검증된다 — 캐시 데이터가 있으면 DB 장애가 전원 503 이 되지 않는다(R16 V-4/V-5)."""
    import time as _t

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    import jwt as pyjwt
    from app.services.auth_service import JWTAuthStrategy

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    strategy = JWTAuthStrategy()
    # DB 에서 한 번 가져온 것처럼 로컬 사본을 심는다.
    strategy._local_keyset = (
        _t.monotonic(),
        [
            {
                "pem": pub_pem.decode(),
                "algorithm": "RS256",
                "issuer": "test-iss",
                "audience": "test-aud",
            }
        ],
    )

    token = pyjwt.encode(
        {
            "user_id": "u1",
            "team_id": "t1",
            "dept_id": "d1",
            "roles": ["USER"],
            "iss": "test-iss",
            "aud": "test-aud",
            "exp": int(_t.time()) + 3600,
        },
        priv_pem,
        algorithm="RS256",
        headers={"kid": "real-kid"},
    )
    # redis=None, db=None — 백엔드 전무인데 캐시 데이터가 살린다.
    auth = await strategy.authenticate(f"Bearer {token}", None, None)
    assert auth.user_id == "u1"


async def test_jwt_no_cache_no_db_is_backend_unavailable():
    """캐시도 DB 도 없으면 자격증명 거부(401)가 아니라 백엔드 장애(503)다 —
    캐시 전무 + DB 다운을 'Invalid JWT' 로 위조하면 클라이언트가 재로그인을
    시도하고 지표가 오염된다(R16 V-5)."""
    from app.services.auth_service import (
        AuthBackendUnavailableError,
        JWTAuthStrategy,
    )

    strategy = JWTAuthStrategy()
    with pytest.raises(AuthBackendUnavailableError):
        await strategy.authenticate(
            f"Bearer {_jwt_token_with_kid('any')}", None, None
        )


async def test_scan_marks_invoke_error_frame():
    """invoke-with-response-stream 의 error 이벤트(네이티브 또는 어댑터 합성)는
    usage 가 아니라 outcome 실패 마킹 대상 — 없으면 상류 에러가 SUCCESS 로
    기록된다(R16-V6-MF1)."""
    from app.routers.bedrock import _scan_bedrock_stream_chunk

    state: dict = {}
    _scan_bedrock_stream_chunk(
        json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "modelTimeoutException",
                    "message": "timed out",
                },
            }
        ).encode(),
        "invoke-with-response-stream",
        state,
    )
    assert state["error_type"] == "modelTimeoutException"
    assert "hit" not in state  # usage 프레임으로 오인되면 안 된다


async def test_scan_marks_converse_exception_member():
    """converse-stream 의 예외 유니온 멤버 {internalServerException: {...}} 도
    outcome 실패 마킹 대상이다."""
    from app.routers.bedrock import _scan_bedrock_stream_chunk

    state: dict = {}
    _scan_bedrock_stream_chunk(
        json.dumps({"internalServerException": {"message": "boom"}}).encode(),
        "converse-stream",
        state,
    )
    assert state["error_type"] == "internalServerException"
    assert "hit" not in state


async def test_scan_converse_usage_not_mistaken_for_error():
    """정상 converse metadata usage 는 error_type 을 세우지 않는다."""
    from app.routers.bedrock import _scan_bedrock_stream_chunk

    state: dict = {}
    _scan_bedrock_stream_chunk(
        json.dumps(
            {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}}
        ).encode(),
        "converse-stream",
        state,
    )
    assert state.get("hit") is True
    assert "error_type" not in state
