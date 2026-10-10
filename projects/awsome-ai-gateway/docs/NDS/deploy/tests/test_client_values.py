# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""client-values — 직원 env 블록 추출의 URL/OIDC 해석 로직."""
from __future__ import annotations

import types

import pytest
import yaml

from .. import cli, schema
from ..render import eks as eks_render

BASE = {
    "version": 1,
    "env": "dev",
    "aws": {"region": "ap-south-1"},
    "deploy": {"target": "eks", "size_tier": "t2"},
    "network": {"mode": "public", "allowed_cidrs": ["10.0.0.0/8"]},
    "domain": {"mode": "route53-acm", "name": "gw.example.com"},
    "features": {"notifications": {"provider": "mock"}},
    "images": {"tag": "1.0.170"},
    "clients": {"models_profile": "global"},
    "oidc": {"issuer_url": "https://cognito-idp.ap-south-1.amazonaws.com/ap-x",
             "client_id": "cid",
             "authorize_url": "https://a.example.com/oauth2/authorize",
             "token_url": "https://a.example.com/oauth2/token"},
}


def _cfg(tmp_path, doc=BASE):
    p = tmp_path / "gateway.yaml"
    p.write_text(yaml.safe_dump(doc))
    return p


def test_eks_ingress_hosts_reads_overlay(tmp_path, monkeypatch):
    vals = tmp_path / "values.yaml"
    vals.write_text(yaml.safe_dump({
        "ingress": {"gateway": {"host": "gateway-x.io"},
                    "adminApi": {"host": "admin-api-x.io"},
                    "adminUi": {"host": "admin-x.io"}}}))
    monkeypatch.setattr(eks_render, "env_values_file",
                        lambda cfg: vals)
    cfg = schema.from_dict(BASE)
    hosts = cli._eks_ingress_hosts(cfg)
    assert hosts == {"gateway": "gateway-x.io",
                     "adminApi": "admin-api-x.io",
                     "adminUi": "admin-x.io"}


def test_eks_ingress_hosts_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(eks_render, "env_values_file", lambda cfg: None)
    assert cli._eks_ingress_hosts(schema.from_dict(BASE)) == {}


def test_client_values_eks_prefers_live_hosts(tmp_path, monkeypatch, capsys):
    """eks 는 values overlay 의 실제 host 를 우선 — 없으면 domain 네이밍."""
    cfg_path = _cfg(tmp_path)
    vals = tmp_path / "values.yaml"
    vals.write_text(yaml.safe_dump({
        "ingress": {"gateway": {"host": "gw-live.example.io"},
                    "adminApi": {"host": "api-live.example.io"}}}))
    monkeypatch.setattr(eks_render, "env_values_file", lambda cfg: vals)
    args = types.SimpleNamespace(config=str(cfg_path))
    assert cli.cmd_client_values(args) == 0
    out = capsys.readouterr().out
    assert 'ANTHROPIC_BASE_URL="https://gw-live.example.io"' in out
    assert 'ADMIN_API_URL="https://api-live.example.io"' in out
    assert 'OIDC_ISSUER_URL="https://cognito-idp.ap-south-1' in out


def test_client_values_domain_fallback_and_warnings(tmp_path, monkeypatch, capsys):
    """values 없는 eks + domain.name → 도메인 네이밍으로 URL 구성."""
    monkeypatch.setattr(eks_render, "env_values_file", lambda cfg: None)
    cfg_path = _cfg(tmp_path)
    assert cli.cmd_client_values(types.SimpleNamespace(config=str(cfg_path))) == 0
    out = capsys.readouterr().out
    assert 'ANTHROPIC_BASE_URL="https://gateway.gw.example.com"' in out
    assert 'ADMIN_API_URL="https://admin-api.gw.example.com"' in out


def test_client_values_compose_http_and_cowork_warning(tmp_path, monkeypatch, capsys):
    """compose + domain none → http:port + Cowork 경고."""
    doc = dict(BASE, deploy={"target": "compose", "size_tier": "t0"},
               domain={"mode": "none", "name": ""})
    cfg_path = _cfg(tmp_path, doc)
    monkeypatch.setattr(cli, "_public_ip", lambda: "1.2.3.4")
    assert cli.cmd_client_values(types.SimpleNamespace(config=str(cfg_path))) == 0
    out = capsys.readouterr().out
    assert 'ANTHROPIC_BASE_URL="http://1.2.3.4:8000"' in out
    assert "Cowork" in out  # https 필수 경고
