# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""capture 테스트 — 렌더 → 캡처 roundtrip 으로 역변환 정확도 검증."""
from __future__ import annotations

import yaml
import pytest

from .. import capture, schema
from ..render import compose as compose_render

BASE = {
    "version": 1,
    "env": "acme-eval",
    "aws": {"region": "ap-northeast-2"},
    "deploy": {"target": "compose", "size_tier": "t0"},
    "network": {"mode": "public", "allowed_cidrs": ["10.0.0.0/8", "172.16.0.0/12"]},
    "domain": {"mode": "none"},
    "features": {"notifications": {"provider": "ses", "ses_from": "ops@acme.io"},
                 "web_search": True},
    "clients": {"models_profile": "global"},
    "oidc": {"issuer_url": "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-xx",
             "client_id": "cid123",
             "authorize_url": "https://x.auth.amazoncognito.com/oauth2/authorize",
             "token_url": "https://x.auth.amazoncognito.com/oauth2/token",
             "required_group": "admins"},
}


def _roundtrip(tmp_path, doc):
    cfg = schema.from_dict(doc)
    out = tmp_path / "gen" / cfg.env
    compose_render.render(cfg, out)
    return capture.capture_compose(out)


def test_compose_roundtrip_core_fields(tmp_path):
    cap, notes = _roundtrip(tmp_path, BASE)
    assert cap["env"] == "acme-eval"
    assert cap["aws"]["region"] == "ap-northeast-2"
    assert cap["deploy"]["target"] == "compose"
    assert cap["network"]["allowed_cidrs"] == ["10.0.0.0/8", "172.16.0.0/12"]
    assert cap["features"]["notifications"]["provider"] == "ses"
    assert cap["features"]["notifications"]["ses_from"] == "ops@acme.io"
    assert cap["features"]["web_search"] is True
    assert cap["oidc"]["client_id"] == "cid123"
    assert cap["oidc"]["issuer_url"].startswith("https://cognito-idp")


def test_compose_roundtrip_domain_and_observability(tmp_path):
    doc = dict(BASE)
    doc["domain"] = {"mode": "route53-acm", "name": "gw.acme.io"}
    doc["features"] = {"notifications": {"provider": "mock"},
                       "observability": True}
    cap, _ = _roundtrip(tmp_path, doc)
    assert cap["domain"]["name"] == "gw.acme.io"
    assert cap["features"]["observability"] is True


def test_compose_captured_doc_is_schema_valid(tmp_path):
    cap, _ = _roundtrip(tmp_path, BASE)
    schema.from_dict(cap)  # 예외 없어야 함 — 못 채운 필드는 notes 가 알린다


def test_compose_capture_missing_dir(tmp_path):
    with pytest.raises(capture.CaptureError):
        capture.capture_compose(tmp_path / "nope")


def test_diff_docs_finds_changes():
    declared = {"a": {"b": 1, "c": "x"}}
    captured = {"a": {"b": 2, "d": "new"}}
    diffs = dict((k, (dv, cv)) for k, dv, cv in capture.diff_docs(declared, captured))
    assert diffs["a.b"] == (1, 2)
    assert diffs["a.c"] == ("x", None)
    assert diffs["a.d"] == (None, "new")
