# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""AICM_API_ENABLED — 꺼져 있으면 /aicm/v1 이 존재하지 않고 기존 API 표면이 그대로다.

- 플래그 기본값 false, false 면 라우트 0개·OpenAPI 경로 0개·요청 404, 모듈 import 도 없다.
- 켜도 기존 경로·스키마의 OpenAPI 정의는 한 글자도 바뀌지 않는다(추가만 된다).
- /admin 라우터는 통합 전용 모듈을 import 하지 않는다.
- 커밋된 docs/NDS/aicm/aicm-v1-openapi.json 은 코드에서 다시 만든 명세와 같다.

base 대비 바이트 동일성(라우트 집합·OpenAPI 전체)은 REPORT.md 의 diff 명령으로 따로 보였다 —
고정 해시를 테스트에 박으면 이후 모든 라우트 추가가 이 테스트를 깨뜨린다.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from fastapi.routing import APIRoute

_ADMIN_API = Path(__file__).resolve().parents[2]
_SPEC = _ADMIN_API.parent / "docs" / "NDS" / "aicm" / "aicm-v1-openapi.json"


def _app(monkeypatch, enabled: bool):
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "AICM_API_ENABLED", enabled)
    return create_app()


def test_flag_defaults_off(monkeypatch):
    from app.core.config import Settings

    monkeypatch.delenv("AICM_API_ENABLED", raising=False)
    assert Settings(_env_file=None).AICM_API_ENABLED is False


def test_flag_off_mounts_nothing(monkeypatch):
    app = _app(monkeypatch, False)
    assert not [r.path for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/aicm")]
    assert not [p for p in app.openapi()["paths"] if p.startswith("/aicm")]
    assert not [n for n in app.openapi()["components"]["schemas"] if n.startswith("Aicm")]


async def test_flag_off_requests_404(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    app = _app(monkeypatch, False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for method, path in (("GET", "/aicm/v1/service-tokens"), ("POST", "/aicm/v1/users/provision"),
                             ("PUT", "/aicm/v1/admin/budgets/spent")):
            r = await c.request(method, path, headers={"Authorization": "Bearer itg-" + "0" * 64})
            assert r.status_code == 404, (method, path, r.status_code)


def test_flag_off_does_not_import_integration_modules():
    code = (
        "import os, sys; os.environ.pop('AICM_API_ENABLED', None);"
        "os.environ.setdefault('VIRTUAL_KEY_ENCRYPTION_KEY', '0'*64);"
        "import app.main;"
        "mods = ['app.routers.aicm', 'app.services.user_lifecycle_service', 'app.services.cognito_admin',"
        " 'app.core.idempotency', 'app.core.integration_auth', 'app.routers.aicm_admin'];"
        "print([m for m in mods if m in sys.modules])"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=_ADMIN_API, capture_output=True, text=True,
                         env={"PYTHONPATH": str(_ADMIN_API / "src"), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "[]"


def test_flag_on_only_adds_paths(monkeypatch):
    off = _app(monkeypatch, False).openapi()
    on = _app(monkeypatch, True).openapi()
    assert {p: v for p, v in on["paths"].items() if not p.startswith("/aicm/")} == off["paths"]
    added = set(on["components"]["schemas"]) - set(off["components"]["schemas"])
    assert all(n.startswith("Aicm") for n in added), added
    for name, schema in off["components"]["schemas"].items():
        assert on["components"]["schemas"][name] == schema, name
    assert {k: v for k, v in on.items() if k not in ("paths", "components")} == {
        k: v for k, v in off.items() if k not in ("paths", "components")}


def test_flag_on_routes_commit_before_response(monkeypatch):
    from app.core.db import CommittingRoute

    app = _app(monkeypatch, True)
    aicm = [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/aicm/v1")]
    assert len(aicm) == 25  # admin mirror 만 — legacy 는 제거됨(aicm-legacy-cleanup)
    assert all(isinstance(r, CommittingRoute) for r in aicm)


def test_admin_routers_do_not_import_integration_modules():
    # integration_token_service 는 scoped 발급이 /admin/service-tokens 로 이전되면서
    # admin 공유 서비스가 됐다(aicm-legacy-cleanup) — banned 가 아니다.
    banned = ("user_lifecycle_service", "cognito_admin", "integration_auth", "core.idempotency",
              "routers.aicm")
    for f in (_ADMIN_API / "src" / "app" / "routers").glob("*.py"):
        if f.name in ("aicm.py", "aicm_admin.py"):
            continue
        text = f.read_text(encoding="utf-8")
        assert not [b for b in banned if b in text], f.name


def test_committed_openapi_spec_is_current(monkeypatch):
    spec = importlib.util.spec_from_file_location("export_aicm_openapi", _ADMIN_API / "scripts" /
                                                  "export_aicm_openapi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected = mod.render(mod.build_spec(_app(monkeypatch, True).openapi()))
    assert _SPEC.read_text(encoding="utf-8") == expected, (
        "docs/NDS/aicm/aicm-v1-openapi.json is stale — run scripts/export_aicm_openapi.py")
    data = json.loads(expected)
    assert all(p.startswith("/aicm/v1/admin/") for p in data["paths"])
    assert "/aicm/v1/service-tokens" not in data["paths"]
    assert set(data["components"]["securitySchemes"]) == {"integrationTokenSvc"}
    assert "itg-" not in expected
    securities = {
        tuple(sorted(next(iter(s)) for s in op.get("security", [])))
        for ops in data["paths"].values() for op in ops.values()
    }
    assert securities == {("integrationTokenSvc",)}


