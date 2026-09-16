# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""`/admin/audit/invocation-log/*` 라우터 검증 (모킹된 DB/AWS).

라우터 층에서만 깨질 수 있고 서비스 단위테스트가 잡지 못하는 계약들:

- **경로 선언 순서**: `{bedrock_request_id}` 를 먼저 선언하면 FastAPI 가 `/config`·
  `/reconcile` 를 path param 으로 삼켜 두 엔드포인트가 사라진다(404 아님 — 엉뚱한 핸들러가
  200 을 준다). 리터럴 경로 테스트가 이걸 잡는다.
- **미설정 = 503**, 404 나 500 이 아니다. UI 가 "안 켰음" 과 "없음" 을 구분해야 한다.
- **RBAC**: ADMIN 전용.
- **본문 게이트 2겹** + 열람 시 감사 기록.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.services.invocation_log_service import LogWindow

pytestmark = pytest.mark.asyncio

_BASE = "/admin/audit/invocation-log"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """`get_settings` 는 lru_cache 다 — env 를 바꾼 테스트가 다음 테스트로 새지 않게 비운다."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("BEDROCK_INVOCATION_LOG_GROUP", "/aws/bedrock/modelinvocations")
    monkeypatch.setenv("BEDROCK_INVOCATION_LOG_REGION", "us-east-2")
    get_settings.cache_clear()


class _FakeService:
    """InvocationLogService 대역 — boto3 없이 라우터 경로만 검증."""

    log_group = "/aws/bedrock/modelinvocations"
    region = "us-east-2"

    def __init__(self, *, window: LogWindow | None = None, record: dict | None = None) -> None:
        self._window = window or LogWindow()
        self._record = record
        self.fetch_record_calls: list[dict] = []

    async def fetch_request_ids(self, *, start, end):
        return self._window

    async def fetch_record(self, bedrock_request_id, *, start, end, include_bodies=False):
        self.fetch_record_calls.append(
            {"id": bedrock_request_id, "include_bodies": include_bodies}
        )
        return self._record


def _install_fake(monkeypatch, fake: _FakeService) -> None:
    from app.routers import audit_reconcile

    monkeypatch.setattr(audit_reconcile, "_build_invocation_log_service", lambda: fake)


# ── 설정 상태 ─────────────────────────────────────────────────────────────────


async def test_config_reports_unconfigured_without_failing(client: AsyncClient, admin_headers, monkeypatch):
    monkeypatch.setenv("BEDROCK_INVOCATION_LOG_GROUP", "")
    get_settings.cache_clear()
    res = await client.get(f"{_BASE}/config", headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["configured"] is False


async def test_config_exposes_the_plane_capture_table(client: AsyncClient, admin_headers, configured):
    """UI 가 'Mantle 은 원래 로그가 없음' 을 설명할 수 있어야 한다 — 서버가 표를 준다."""
    body = (await client.get(f"{_BASE}/config", headers=admin_headers)).json()
    assert body["configured"] is True
    assert body["region"] == "us-east-2"
    assert body["capture_by_plane"]["BEDROCK_RUNTIME_OPENAI"] is True
    assert body["capture_by_plane"]["BEDROCK_MANTLE_OPENAI"] is False


async def test_config_is_admin_only(client: AsyncClient, dev_headers, configured):
    res = await client.get(f"{_BASE}/config", headers=dev_headers)
    assert res.status_code == 403


# ── 경로 선언 순서 (리터럴 vs path param) ─────────────────────────────────────


async def test_literal_paths_are_not_swallowed_by_the_id_route(
    client: AsyncClient, admin_headers, configured, monkeypatch
):
    """`/config`·`/reconcile` 가 `{bedrock_request_id}` 핸들러로 라우팅되면 안 된다."""
    fake = _FakeService(record={"bedrock_request_id": "x", "records": []})
    _install_fake(monkeypatch, fake)

    assert (await client.get(f"{_BASE}/config", headers=admin_headers)).json()["configured"] is True
    body = (await client.get(f"{_BASE}/reconcile", headers=admin_headers)).json()
    assert "usage_rows" in body
    # id 라우트가 실제로 불렸다면 fetch_record 호출 기록이 남는다.
    assert fake.fetch_record_calls == []


# ── reconcile ─────────────────────────────────────────────────────────────────


async def test_reconcile_requires_configuration_and_says_503(
    client: AsyncClient, admin_headers, monkeypatch
):
    """미설정을 404/500 으로 주면 UI 가 '안 켰음' 을 안내할 수 없다."""
    monkeypatch.setenv("BEDROCK_INVOCATION_LOG_GROUP", "")
    get_settings.cache_clear()
    res = await client.get(f"{_BASE}/reconcile", headers=admin_headers)
    assert res.status_code == 503
    # ⚠️ `detail` 이 아니라 `error.message` 다. HTTPException 을 프로젝트 표준 envelope
    #    (`{"error": {"type","code","message"}}`) 으로 정규화하는 핸들러가 main.py 에
    #    추가됐다 — 예전엔 FastAPI 기본 핸들러의 `{"detail": ...}` 와 프로젝트 핸들러의
    #    envelope 두 shape 가 같은 API 에서 섞여 나왔고, admin-ui 파서는 후자만 읽었다.
    assert "BEDROCK_INVOCATION_LOG_GROUP" in res.json()["error"]["message"]


async def test_reconcile_returns_a_report_shape(client: AsyncClient, admin_headers, configured, monkeypatch):
    _install_fake(monkeypatch, _FakeService(window=LogWindow(records_matched=0, records_returned=0)))
    body = (await client.get(f"{_BASE}/reconcile?hours=2", headers=admin_headers)).json()
    for key in ("usage_rows", "matched", "missing_count", "skipped_null", "orphan_count", "log_group"):
        assert key in body
    assert body["log_group"] == "/aws/bedrock/modelinvocations"
    assert body["pad_minutes"] == 10
    assert body["usage_rows_truncated"] is False


async def test_reconcile_rejects_a_half_specified_window(client: AsyncClient, admin_headers, configured):
    res = await client.get(f"{_BASE}/reconcile?start=2026-09-01T00:00:00Z", headers=admin_headers)
    assert res.status_code == 400


async def test_reconcile_rejects_an_inverted_window(client: AsyncClient, admin_headers, configured):
    res = await client.get(
        f"{_BASE}/reconcile?start=2026-09-02T00:00:00Z&end=2026-09-01T00:00:00Z",
        headers=admin_headers,
    )
    assert res.status_code == 400


async def test_reconcile_rejects_an_overlong_window(client: AsyncClient, admin_headers, configured):
    """구간 상한이 없으면 Insights 쿼리 비용·응답시간이 예측 불가해진다."""
    res = await client.get(
        f"{_BASE}/reconcile?start=2026-01-01T00:00:00Z&end=2026-09-01T00:00:00Z",
        headers=admin_headers,
    )
    assert res.status_code == 400


async def test_reconcile_is_admin_only(client: AsyncClient, dev_headers, configured):
    res = await client.get(f"{_BASE}/reconcile", headers=dev_headers)
    assert res.status_code == 403


# ── 단일 레코드 / 본문 게이트 ─────────────────────────────────────────────────


async def test_bodies_are_refused_when_the_setting_is_off(
    client: AsyncClient, admin_headers, configured, monkeypatch
):
    monkeypatch.setenv("BEDROCK_INVOCATION_LOG_BODIES_ENABLED", "false")
    get_settings.cache_clear()
    fake = _FakeService(record={"records": []})
    _install_fake(monkeypatch, fake)
    res = await client.get(f"{_BASE}/aws-1?include_bodies=true", headers=admin_headers)
    assert res.status_code == 403
    # 게이트가 AWS 호출 **전** 에 걸려야 한다 — 막을 거면 조회도 하지 않는다.
    assert fake.fetch_record_calls == []


async def test_metadata_only_read_is_allowed_without_the_body_setting(
    client: AsyncClient, admin_headers, configured, monkeypatch
):
    fake = _FakeService(record={"bedrock_request_id": "aws-1", "records": [{"requestId": "aws-1"}]})
    _install_fake(monkeypatch, fake)
    res = await client.get(f"{_BASE}/aws-1", headers=admin_headers)
    assert res.status_code == 200
    assert fake.fetch_record_calls == [{"id": "aws-1", "include_bodies": False}]


async def test_body_read_is_audited(client: AsyncClient, admin_headers, configured, monkeypatch):
    """본문 열람 기록이 없으면 그 권한은 승인받을 수 없다."""
    monkeypatch.setenv("BEDROCK_INVOCATION_LOG_BODIES_ENABLED", "true")
    get_settings.cache_clear()
    _install_fake(monkeypatch, _FakeService(record={"bedrock_request_id": "aws-1", "records": [{}]}))

    logged: list[dict] = []

    from app.routers import audit_reconcile

    async def _capture(session, **kwargs):
        logged.append(kwargs)

    monkeypatch.setattr(audit_reconcile.audit_logger, "log", _capture)
    res = await client.get(f"{_BASE}/aws-1?include_bodies=true", headers=admin_headers)
    assert res.status_code == 200
    assert len(logged) == 1
    assert logged[0]["action"] == "VIEW_INVOCATION_LOG_BODY"
    assert logged[0]["resource_id"] == "aws-1"
    assert logged[0]["result"] == "SUCCESS"


async def test_body_read_of_a_missing_record_is_still_audited(
    client: AsyncClient, admin_headers, configured, monkeypatch
):
    """'없더라' 도 열람 **시도** 의 사실이다 — 404 라고 기록을 빼면 감사에 구멍이 난다."""
    monkeypatch.setenv("BEDROCK_INVOCATION_LOG_BODIES_ENABLED", "true")
    get_settings.cache_clear()
    _install_fake(monkeypatch, _FakeService(record=None))

    logged: list[dict] = []

    from app.routers import audit_reconcile

    async def _capture(session, **kwargs):
        logged.append(kwargs)

    monkeypatch.setattr(audit_reconcile.audit_logger, "log", _capture)
    res = await client.get(f"{_BASE}/aws-1?include_bodies=true", headers=admin_headers)
    assert res.status_code == 404
    assert len(logged) == 1
    assert logged[0]["result"] == "FAILURE"


async def test_missing_record_404_explains_the_mantle_case(
    client: AsyncClient, admin_headers, configured, monkeypatch
):
    """Mantle plane 이면 레코드가 없는 게 정상이다 — 404 문구가 그걸 알려줘야 한다."""
    _install_fake(monkeypatch, _FakeService(record=None))
    res = await client.get(f"{_BASE}/aws-nope", headers=admin_headers)
    assert res.status_code == 404
    # `detail` → `error.message` (위 503 테스트의 주석과 같은 이유 — envelope 정규화).
    assert "Mantle" in res.json()["error"]["message"]


async def test_malformed_request_id_is_400_not_500(client: AsyncClient, admin_headers, configured, monkeypatch):
    from app.routers import audit_reconcile

    class _Raiser(_FakeService):
        async def fetch_record(self, *a, **kw):
            raise ValueError("bedrock_request_id 형식이 올바르지 않습니다")

    monkeypatch.setattr(audit_reconcile, "_build_invocation_log_service", lambda: _Raiser())
    res = await client.get(f"{_BASE}/not%20valid", headers=admin_headers)
    assert res.status_code == 400


async def test_record_is_admin_only(client: AsyncClient, dev_headers, configured):
    res = await client.get(f"{_BASE}/aws-1", headers=dev_headers)
    assert res.status_code == 403
