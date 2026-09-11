# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""같은 API 표면이 두 가지 호환되지 않는 에러 형상을 반환하던 결함.

배경:
  * 코드베이스에는 `raise HTTPException(...)` 이 53곳(AST 집계) 있고, 정규화 핸들러가
    없어서 전부 FastAPI 기본 핸들러를 타고 `{"detail": ...}` 로 나갔다
    (예: src/app/core/auth.py:91 의 401).
  * 반면 main.py 의 프로젝트 핸들러들(NotFoundError/ForbiddenError/422/최후의 그물)은
    모두 `{"error": {"type", "code", "message"}}` 봉투를 낸다.
  * admin-ui 의 api-client 는 `error.code`/`error.message` 를 먼저 읽는다
    (admin-ui/src/lib/api-client.ts:54) → detail 형상에서는 error_code 가
    'UNKNOWN_ERROR' 로 뭉개지고, detail 이 dict 면 토스트가 "[object Object]" 가 된다.
  * routers/auth_oidc.py 는 detail 에 봉투를 직접 넣어서 FastAPI 가 그걸 또 감싼
    `{"detail": {"error": {...}}}` **이중 봉투**를 내보냈다.
  * 500(미처리 예외) 응답만 `x-request-id` **헤더**가 빠졌다. Exception 핸들러는
    Starlette 의 ServerErrorMiddleware(미들웨어 스택 바깥)가 호출하므로
    add_request_id 미들웨어의 `response.headers[...] = ...` 를 밟지 못한다.

수정은 53곳을 손대는 게 아니라 main.py 에 **정규화 핸들러 하나**를 등록하는 것이다.
이 파일은 그 핸들러가 지켜야 하는 계약을 못 박는다 — 특히 (a) 봉투, (b) raise 쪽이
붙인 헤더 보존, (c) 422 는 기존 핸들러가 계속 담당, (d) 이미 봉투인 detail 재포장 금지,
(e) 500 의 x-request-id 헤더.

⚠️ 핸들러는 반드시 **starlette** 의 HTTPException 에 등록돼야 한다. fastapi 쪽 서브클래스에만
등록하면 라우터가 직접 내는 404/405 는 FastAPI 기본 핸들러가 계속 처리해서
`{"detail": "Not Found"}` 가 새어 나간다 — 그래서 아래에 404/405 테스트가 있다.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI, HTTPException, Query
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict

import app.main as main_module
from app.core.db import get_db_session
from app.core.exceptions import NotFoundError
from app.main import create_app, register_exception_handlers


def _build_app() -> FastAPI:
    """핸들러 배선만 프로덕션과 공유하는 최소 앱.

    ⚠️ 핸들러를 여기서 복제하지 않는다 — register_exception_handlers 를 그대로 호출한다.
    복제본을 검증하면 프로덕션 앱과 조용히 갈라진다(main.py:197 의 경고와 같은 이유).
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    @app.get("/raise-403")
    async def raise_403():
        raise HTTPException(403, "x")

    @app.get("/raise-401-with-header")
    async def raise_401_with_header():
        # 실제 401 경로가 붙이는 인증 챌린지. 이게 사라지면 클라이언트는 재인증 방법을
        # 알 수 없다(RFC 7235 요구사항).
        raise HTTPException(
            401,
            "Missing or invalid Authorization header",
            headers={"WWW-Authenticate": 'Bearer realm="admin-api", error="invalid_token"'},
        )

    @app.get("/raise-enveloped")
    async def raise_enveloped():
        # auth_oidc.py 처럼 이미 프로젝트 봉투인 detail.
        raise HTTPException(
            503,
            detail={"error": {"type": "oidc_disabled", "code": "OIDC_DISABLED", "message": "off"}},
        )

    @app.get("/raise-list-detail")
    async def raise_list_detail():
        raise HTTPException(400, detail=[{"loc": ["body", "alias"], "msg": "too short"}])

    @app.get("/raise-304")
    async def raise_304():
        raise HTTPException(304)

    @app.get("/raise-teapot")
    async def raise_teapot():
        raise HTTPException(418, "brew")

    @app.get("/raise-app-error")
    async def raise_app_error():
        # AppError 계열 핸들러가 새 핸들러에 가려지지 않았는지(둘 다 살아 있어야 한다).
        raise NotFoundError("Key", "abc")

    @app.get("/typed")
    async def typed(limit: int = Query(...)):
        return {"limit": limit}

    return app


def _client(app: FastAPI) -> AsyncClient:
    """raise_app_exceptions=False — ServerErrorMiddleware 는 500 을 **보낸 뒤** 예외를
    다시 던진다. 기본값이면 httpx 가 그 예외를 올려서 정작 클라이언트가 받는 응답
    (본문·헤더)을 검사할 수 없다. uvicorn 뒤의 실제 클라이언트는 500 JSON 을 받는다."""
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")


# ── (a) 공허성 대조군 ──────────────────────────────────────────────────────────


async def test_vacuity_control_harness_reaches_the_route_and_the_new_handler(monkeypatch):
    """이 하네스가 정말로 코드를 밟는지 먼저 증명한다.

    * 정상 라우트가 200 을 낸다 → ASGI 앱/클라이언트 배선이 살아 있다.
    * 봉투 생성 함수가 예외 1건당 정확히 1회 호출된다 → 아래 단정들이 '우연히 통과'가
      아니라 실제로 **우리 핸들러**를 통과한 응답을 본 것이다.
    """
    seen: list[int] = []
    real = main_module._http_exception_envelope

    def spy(exc):
        seen.append(exc.status_code)
        return real(exc)

    monkeypatch.setattr(main_module, "_http_exception_envelope", spy)

    app = _build_app()
    async with _client(app) as ac:
        healthy = await ac.get("/ok")
        assert healthy.status_code == 200 and healthy.json() == {"ok": True}
        assert seen == [], "예외가 없는데 에러 핸들러가 불렸다 — 하네스가 이상하다"

        res = await ac.get("/raise-403")

    assert res.status_code == 403
    assert seen == [403], (
        f"핸들러가 호출되지 않았다(호출기록={seen}) — 이 파일의 모든 단정이 공허해진다. "
        "register_exception_handlers 가 StarletteHTTPException 핸들러를 등록하는지 확인"
    )


# ── (b) 봉투 정규화 ────────────────────────────────────────────────────────────


async def test_plain_http_exception_returns_the_project_envelope_not_detail():
    """`raise HTTPException(403, "x")` → {"error": {...}}. {"detail": ...} 는 금지."""
    app = _build_app()
    async with _client(app) as ac:
        res = await ac.get("/raise-403")

    assert res.status_code == 403
    assert res.headers["content-type"].startswith("application/json")
    body = res.json()
    assert "detail" not in body, f"FastAPI 기본 형상이 그대로 나갔다: {body}"
    assert body == {"error": {"type": "forbidden", "code": "FORBIDDEN", "message": "x"}}, body


async def test_real_app_401_from_core_auth_is_enveloped():
    """실제 프로덕션 앱·실제 라우터로 확인 — src/app/core/auth.py:91 이 이 결함의 대표 사례다.

    Authorization 헤더 없이 /admin/keys 를 부르면 인증 의존성이 401 을 던진다(DB 접근 전).
    최소 앱만 검증하면 라우터/미들웨어 조합에서만 나타나는 회귀를 놓친다.
    """
    app = create_app()
    async with _client(app) as ac:
        res = await ac.get("/admin/keys")

    assert res.status_code == 401
    body = res.json()
    assert "detail" not in body, f"53곳 중 하나가 아직 detail 형상이다: {body}"
    assert body["error"]["type"] == "unauthorized"
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert body["error"]["message"] == "Missing or invalid Authorization header"
    # 성공 경로와 같은 상관관계 헤더.
    assert res.headers.get("x-request-id")


async def test_router_404_and_405_are_enveloped_too():
    """경로 없음/메서드 불일치는 **starlette** HTTPException 이다.

    fastapi.HTTPException 에만 핸들러를 달면 이 둘은 FastAPI 기본 핸들러로 빠져서
    `{"detail": "Not Found"}` 가 계속 나간다 — 그래서 base class 에 등록해야 한다.
    """
    app = _build_app()
    async with _client(app) as ac:
        missing = await ac.get("/no-such-path")
        wrong_method = await ac.post("/ok")

    assert missing.status_code == 404
    assert "detail" not in missing.json(), missing.json()
    assert missing.json()["error"]["type"] == "not_found"

    assert wrong_method.status_code == 405
    assert "detail" not in wrong_method.json(), wrong_method.json()
    assert wrong_method.json()["error"]["type"] == "method_not_allowed"


async def test_unmapped_status_still_gets_an_envelope():
    """매핑 표에 없는 상태코드(418)도 형상은 같아야 한다 — 여기서 형상이 갈라지면
    클라이언트 파서는 다시 두 갈래가 된다."""
    app = _build_app()
    async with _client(app) as ac:
        res = await ac.get("/raise-teapot")

    assert res.status_code == 418
    assert res.json() == {"error": {"type": "http_error", "code": "HTTP_418", "message": "brew"}}


async def test_non_string_detail_becomes_a_readable_message_and_keeps_the_raw():
    """detail 이 dict/list 여도 message 는 사람이 읽는 문자열이어야 한다.

    그대로 흘리면 admin-ui 토스트가 "[object Object]" 가 된다(422 배열에서 겪은 결함).
    """
    app = _build_app()
    async with _client(app) as ac:
        res = await ac.get("/raise-list-detail")

    assert res.status_code == 400
    err = res.json()["error"]
    assert err["type"] == "validation_error"
    assert isinstance(err["message"], str) and "too short" in err["message"]
    assert "object Object" not in err["message"]
    # 원본은 기계 소비자를 위해 보존(422 핸들러가 error.fields 로 남기는 것과 같은 이유).
    assert err["detail"] == [{"loc": ["body", "alias"], "msg": "too short"}]


async def test_app_error_handlers_are_not_shadowed():
    """NotFoundError 같은 프로젝트 예외는 계속 자기 핸들러(code=not_found)를 타야 한다.

    새 핸들러가 HTTPException 계열만 잡는지 확인하는 대조군 — 여기서 code 가
    'NOT_FOUND'(새 핸들러의 형식)로 바뀌면 예외 라우팅이 잘못된 것이다.
    """
    app = _build_app()
    async with _client(app) as ac:
        res = await ac.get("/raise-app-error")

    assert res.status_code == 404
    assert res.json() == {
        "error": {"type": "not_found", "message": "Key not found: abc", "code": "not_found"}
    }


# ── (c) raise 쪽 헤더 보존 ─────────────────────────────────────────────────────


async def test_401_keeps_the_www_authenticate_header():
    """401 의 WWW-Authenticate 를 잃으면 인증 계약이 깨진다(FastAPI 기본 핸들러는 보존했다).

    정규화하면서 headers 를 흘리는 건 흔한 회귀라 봉투와 함께 못 박는다.
    """
    app = _build_app()
    async with _client(app) as ac:
        res = await ac.get("/raise-401-with-header")

    assert res.status_code == 401
    assert res.headers["www-authenticate"] == 'Bearer realm="admin-api", error="invalid_token"'
    assert res.json()["error"]["type"] == "unauthorized"


async def test_bodyless_status_stays_bodyless():
    """304 에 JSON 본문을 실으면 h11 이 프로토콜 위반으로 끊는다 — 봉투를 씌우지 않는다."""
    app = _build_app()
    async with _client(app) as ac:
        res = await ac.get("/raise-304")

    assert res.status_code == 304
    assert res.content == b"", f"본문이 실렸다: {res.content!r}"


# ── (d) 422 는 기존 핸들러가 계속 담당 ────────────────────────────────────────


async def test_422_validation_shape_is_unchanged():
    """RequestValidationError 는 HTTPException 계열이 아니므로 전용 핸들러가 계속 처리한다.

    새 핸들러가 이걸 가로채면 error.fields(폼 인라인 에러의 원본 배열)가 사라진다.
    """
    app = _build_app()
    async with _client(app) as ac:
        res = await ac.get("/typed", params={"limit": "not-an-int"})

    assert res.status_code == 422
    err = res.json()["error"]
    assert err["type"] == "validation_error"
    assert err["code"] == "REQUEST_VALIDATION_ERROR", (
        f"새 핸들러가 422 를 가로챘다(HTTP_422 로 바뀌었나?): {err}"
    )
    assert "limit" in err["message"]
    assert isinstance(err["fields"], list) and err["fields"]
    assert "limit" in [str(p) for p in err["fields"][0]["loc"]]


# ── (e) 이미 봉투인 detail 은 재포장 금지 ─────────────────────────────────────


async def test_already_enveloped_detail_is_passed_through_once():
    app = _build_app()
    async with _client(app) as ac:
        res = await ac.get("/raise-enveloped")

    assert res.status_code == 503
    body = res.json()
    assert "detail" not in body, f"이중 봉투: {body}"
    assert body == {
        "error": {"type": "oidc_disabled", "code": "OIDC_DISABLED", "message": "off"}
    }
    assert "error" not in body["error"], f"봉투 안에 봉투: {body}"


async def test_real_auth_oidc_503_is_not_double_enveloped():
    """실제 라우터(routers/auth_oidc.py:55)로 확인 — 이 결함의 발생지다.

    OIDC 미설정(app.state.oidc_service is None)이면 503 을 던지는데, 예전 응답은
    `{"detail": {"error": {...}}}` 였다. CLI/UI 가 다른 엔드포인트와 다른 깊이를
    파고들어야 error.type 을 볼 수 있었다.
    """
    from app.routers import auth_oidc

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth_oidc.router)
    app.state.oidc_service = None

    async def _no_db():
        # 503 은 세션을 쓰기 전에 던져지지만, 의존성 해석 자체는 엔드포인트 실행 **전에**
        # 일어난다 → 실 DB 커넥션을 막으려면 오버라이드가 필요하다.
        yield None

    app.dependency_overrides[get_db_session] = _no_db

    async with _client(app) as ac:
        res = await ac.post("/v1/auth/exchange", json={})

    assert res.status_code == 503, res.text
    body = res.json()
    assert "detail" not in body, f"이중 봉투가 그대로다: {body}"
    err = body["error"]
    assert err["type"] == "oidc_disabled"
    assert err["code"] == "OIDC_DISABLED"
    assert "OIDC_ISSUER_URL" in err["message"]
    assert "error" not in err, f"봉투 안에 봉투: {body}"


# ── (f) 500 의 x-request-id 헤더 ──────────────────────────────────────────────


@pytest.fixture()
def boom_app() -> FastAPI:
    """프로덕션 앱 + 터지는 라우트.

    ⚠️ 최소 앱으로는 이 결함을 재현할 수 없다: x-request-id 는 create_app() 의
    add_request_id 미들웨어가 붙이는데, 결함의 본질이 "Exception 핸들러는 그 미들웨어
    **바깥**(ServerErrorMiddleware)에서 호출된다" 는 것이다. 미들웨어가 있는 실제
    앱에서만 헤더 누락이 드러난다.
    """
    app = create_app()

    @app.get("/__boom")
    async def boom():
        raise RuntimeError("db exploded with password hunter2")

    return app


async def test_500_carries_the_x_request_id_header(boom_app: FastAPI):
    """클라이언트가 보낸 x-request-id 가 500 응답 **헤더**에도 있어야 한다.

    본문에만 있으면 본문을 파싱하지 않는 프록시/ALB 로그·CLI 가 상관관계를 잃는다
    (성공 응답과 4xx 는 이미 헤더를 준다 → 500 만 다른 것이 더 나쁘다).
    """
    async with _client(boom_app) as ac:
        res = await ac.get("/__boom", headers={"x-request-id": "req-abc-123"})

    assert res.status_code == 500
    assert res.headers.get("x-request-id") == "req-abc-123", (
        f"500 응답에 x-request-id 헤더가 없다: {dict(res.headers)}"
    )
    err = res.json()["error"]
    assert err["code"] == "INTERNAL_ERROR"
    assert err["request_id"] == "req-abc-123"
    # 예외 문자열은 본문에 담지 않는다(내부 구조/자격증명 노출 방지).
    assert "hunter2" not in res.text


async def test_500_header_matches_the_generated_id_when_client_sends_none(boom_app: FastAPI):
    """클라이언트가 안 보내면 미들웨어가 만든 uuid 가 헤더·본문에 **같은 값**으로 나가야 한다."""
    async with _client(boom_app) as ac:
        res = await ac.get("/__boom")

    header_id = res.headers.get("x-request-id")
    assert header_id, f"헤더 누락: {dict(res.headers)}"
    assert res.json()["error"]["request_id"] == header_id
    uuid.UUID(header_id)  # 형식까지 — 빈 문자열/'None' 같은 값이 새는 걸 막는다


async def test_4xx_and_500_agree_on_the_header_contract(boom_app: FastAPI):
    """같은 앱에서 4xx 와 500 이 같은 헤더 계약을 지키는지(둘 중 하나만 주면 회귀다)."""
    async with _client(boom_app) as ac:
        not_found = await ac.get("/no-such-path", headers={"x-request-id": "rid-1"})
        server_error = await ac.get("/__boom", headers={"x-request-id": "rid-2"})

    assert not_found.headers.get("x-request-id") == "rid-1"
    assert server_error.headers.get("x-request-id") == "rid-2"


# ── (i) 422 fields[].input 의 민감값 반사 차단 ────────────────────────────────
#
# ⚠️ 이 노출면은 요청 스키마에 extra="forbid" 를 넣으면서 **새로 생겼다.** forbid 전에는
#    선언되지 않은 키가 조용히 버려져 반사될 일이 없었는데, forbid 는 그 키를
#    extra_forbidden 에러로 이 핸들러에 넘기고 pydantic 은 키의 **값**을 input 에 담는다.
#    실측(적대적 검증): x_api_key 를 보내면 422 본문에 "input": "sk-SECRET-abc123" 그대로.


class _ForbidBody(BaseModel):
    """⚠️ 이 클래스는 **모듈 레벨**이어야 한다.

    이 모듈 맨 위에 ``from __future__ import annotations`` 이 있어서 라우트 핸들러의
    애노테이션은 문자열(``"_ForbidBody"``)로 남고, FastAPI 는 그걸 모듈 전역에서 해석한다.
    함수 안에 정의하면 해석에 실패해 body 파라미터를 인식하지 못하고 모든 요청이
    ``422 "body: Field required"`` 가 된다 — 실제로 그렇게 한 번 틀렸고, 공허성 대조군이
    잡았다(그게 없었으면 "422 가 났으니 통과" 로 넘어갔을 것이다).
    """

    model_config = ConfigDict(extra="forbid")
    description: str | None = None


def _forbid_app() -> FastAPI:
    """extra="forbid" 스키마를 받는 최소 앱 — 프로덕션과 같은 핸들러 배선."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/forbid")
    async def forbid(body: _ForbidBody):  # pragma: no cover - 422 경로만 검사
        return {"ok": True}

    return app


async def test_vacuity_control_the_forbid_app_really_rejects_extras():
    """대조군 — 앱이 extra 키를 실제로 거부하는지부터. 안 그러면 아래가 전부 공허하다."""
    app = _forbid_app()
    async with _client(app) as ac:
        res = await ac.post("/forbid", json={"description": "ok", "bogus": "v"})
    assert res.status_code == 422, res.text[:200]
    err = res.json()["error"]
    assert "bogus" in err["message"]
    assert err["fields"], "fields 가 비었다 — input 검사가 공허해진다"


async def test_secret_looking_extra_key_value_is_not_reflected():
    app = _forbid_app()
    secret = "sk-SECRET-abc123"
    async with _client(app) as ac:
        res = await ac.post("/forbid", json={"description": "ok", "x_api_key": secret})

    assert res.status_code == 422
    body = res.text
    assert secret not in body, (
        f"민감해 보이는 extra 키의 값이 응답에 그대로 반사됐다 — 로그/토스트/버그리포트로 "
        f"전파된다: {body[:300]}"
    )
    err = res.json()["error"]
    field = next(f for f in err["fields"] if f["loc"][-1] == "x_api_key")
    assert field["input"] == "[REDACTED]"
    # ⚠️ 가리는 건 값뿐이다. 어느 키가 왜 거부됐는지는 남아야 정규화의 목적이 유지된다.
    assert "x_api_key" in err["message"]
    assert field["msg"], "거부 이유(msg)까지 지워버리면 운영자가 원인을 알 수 없다"


async def test_non_secret_extra_key_value_is_still_shown():
    """대조군 — 전부 가리면 디버깅이 불가능해진다. 민감하지 않은 값은 남는다."""
    app = _forbid_app()
    async with _client(app) as ac:
        res = await ac.post("/forbid", json={"description": "ok", "provider": "BEDROCK"})

    err = res.json()["error"]
    field = next(f for f in err["fields"] if f["loc"][-1] == "provider")
    assert field["input"] == "BEDROCK", f"민감하지 않은 값까지 가렸다: {field}"


async def test_nested_secret_inside_a_dict_value_is_redacted():
    """extra 키의 값이 dict 면 그 안의 민감 키도 가려야 한다."""
    app = _forbid_app()
    async with _client(app) as ac:
        res = await ac.post(
            "/forbid",
            json={"description": "ok", "config": {"region": "ap-northeast-2",
                                                  "client_secret": "shh-1234"}},
        )
    assert "shh-1234" not in res.text, f"중첩된 민감값이 반사됐다: {res.text[:300]}"
    err = res.json()["error"]
    field = next(f for f in err["fields"] if f["loc"][-1] == "config")
    assert field["input"]["region"] == "ap-northeast-2", "중첩 비민감값까지 가렸다"
    assert field["input"]["client_secret"] == "[REDACTED]"


async def test_case_and_affix_variants_are_caught():
    """`X-Api-Key` / `refresh_token` 처럼 대소문자·접두접미가 붙는 실제 형태."""
    app = _forbid_app()
    for key, value in (("X-Api-Key", "AKIA-nope"), ("refresh_token", "rt-9999")):
        async with _client(app) as ac:
            res = await ac.post("/forbid", json={"description": "ok", key: value})
        assert value not in res.text, f"{key} 의 값이 반사됐다: {res.text[:200]}"


# ── (j) 502/504 는 상류 실패로 분류된다 ───────────────────────────────────────


async def test_upstream_failures_are_not_reported_as_our_internal_error():
    """502 가 매핑에 없으면 fallback 이 internal_error 로 뭉갠다.

    상류(Bedrock/AgentCore/S3) 장애를 우리 결함으로 보고하면 클라이언트가 재시도 가능
    여부를 구분할 수 없다. 실제 발생지: routers/chat_agent.py 의 query/S3/upstream 실패.
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/upstream-502")
    async def upstream_502():
        raise HTTPException(502, "bedrock query failed")

    @app.get("/upstream-504")
    async def upstream_504():
        raise HTTPException(504, "upstream timed out")

    async with _client(app) as ac:
        r502 = await ac.get("/upstream-502")
        r504 = await ac.get("/upstream-504")

    assert r502.status_code == 502
    assert r502.json()["error"]["type"] == "bad_gateway", (
        f"상류 실패가 우리 내부 오류로 보고된다: {r502.json()['error']}"
    )
    assert r504.json()["error"]["type"] == "gateway_timeout"
    # 대조군 — 진짜 우리 오류(500)는 여전히 internal_error 여야 한다.
    assert r502.json()["error"]["type"] != "internal_error"


# ── (k) 직접 만든 JSONResponse 는 봉투를 우회한다 ─────────────────────────────
#
# ⚠️ exception handler 는 **raise 된** 예외만 본다. 라우터가
#    `return JSONResponse(status_code=400, content={"error": "문자열"})` 을 하면 어떤
#    핸들러도 개입할 수 없고, `error` 가 dict 가 아니라 문자열인 **제3의 shape** 가 된다.
#    admin-ui 파서는 code/message 를 못 찾아 화면에 `UNKNOWN_ERROR` +
#    "Request failed with status 400" 만 띄운다 — 즉 위의 봉투 정규화를 다 해놓고도
#    사용자에게는 원인이 안 보인다. 실제로 users.py 의 Cognito 미설정 경로 3곳이 그랬다.


async def test_cognito_sync_helper_raises_instead_of_returning_a_bare_error_dict(monkeypatch):
    """미설정 시 raise 여야 한다. 2-튜플 반환이면 호출부가 봉투를 우회하게 된다."""
    from app.core.config import get_settings
    from app.routers.users import _build_cognito_sync_service

    monkeypatch.setenv("COGNITO_USER_POOL_ID", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(HTTPException) as caught:
            _build_cognito_sync_service()
    finally:
        get_settings.cache_clear()

    assert caught.value.status_code == 400
    # 원인이 문구에 남아야 운영자가 "Cognito 를 안 켰다"를 알 수 있다.
    assert "COGNITO_USER_POOL_ID" in str(caught.value.detail)


def test_no_router_hand_builds_an_error_string_response():
    """AST 가드 — `JSONResponse(content={"error": "<문자열>"})` 를 전면 금지한다.

    개별 3곳을 고치는 것보다 클래스를 막는다. `error` 가 dict 인 경우(봉투를 직접 만든
    정당한 케이스)는 허용하고, 문자열인 경우만 잡는다.

    ⚠️ 한계: **dict 리터럴만** 본다. `content=err` 처럼 변수로 넘기면 여기서 못 잡는다
    (음성대조군에서 실제로 통과했다). 그 형태는 위의
    ``test_cognito_sync_helper_raises_instead_of_returning_a_bare_error_dict`` 같은
    행동 테스트가 잡는다 — 이 가드만 믿으면 안 된다.
    """
    import ast
    from pathlib import Path

    routers_dir = Path(main_module.__file__).resolve().parent / "routers"
    offenders: list[str] = []
    scanned = 0

    for path in sorted(routers_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scanned += 1
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "JSONResponse":
                continue
            for kw in node.keywords:
                if kw.arg != "content" or not isinstance(kw.value, ast.Dict):
                    continue
                for k, v in zip(kw.value.keys, kw.value.values):
                    if (
                        isinstance(k, ast.Constant)
                        and k.value == "error"
                        and isinstance(v, ast.Constant)
                        and isinstance(v.value, str)
                    ):
                        offenders.append(f"{path.name}:{node.lineno}")

    # 대조군 — 파일을 못 훑었으면 위 결과가 공허하다.
    assert scanned >= 10, f"라우터를 {scanned}개만 훑었다 — 경로가 바뀌었나: {routers_dir}"
    assert offenders == [], (
        "라우터가 봉투를 우회하는 문자열 error 응답을 직접 만든다 — admin-ui 에서 "
        "UNKNOWN_ERROR 로만 보인다. `raise HTTPException(...)` 으로 바꿀 것:\n  "
        + "\n  ".join(offenders)
    )
