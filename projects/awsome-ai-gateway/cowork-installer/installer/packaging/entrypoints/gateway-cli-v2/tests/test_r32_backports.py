# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R32 regression tests — R26 fixes backported to the cowork fork.

The cowork entrypoints tree forked from installer/…/gateway-cli-v2 before the
R26 review fixes landed, so these defects shipped here unfixed:
  * token/VK caches written via write_text()+chmod → umask-readable window
  * naive server `expires_at` → naive/aware TypeError in the daemon, and a
    ~host-offset skew (KST = +9h) in `is_expiring` after .timestamp()
"""

from __future__ import annotations

import json
import stat

import pytest


def _fake_exchange_response(expires_at: str | None):
    class _Resp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            data = {"virtual_key": "vk-x"}
            if expires_at is not None:
                data["expires_at"] = expires_at
            return data

    return _Resp()


class _HelperCfg:
    gateway_url = "https://admin.example.com"
    connect_timeout = 5
    read_timeout = 15


# ---------------------------------------------------------------------------
# 0600 atomic secure writes (TOCTOU fix)
# ---------------------------------------------------------------------------

def test_save_tokens_created_0600(tmp_path, monkeypatch):
    """A fresh token cache is 0600 from creation — never umask-readable."""
    import os

    from gateway_cli_oidc import oidc_client

    monkeypatch.setenv("GATEWAY_CLI_OIDC_CACHE", str(tmp_path / "t.json"))
    old = os.umask(0o022)  # worst-case permissive umask
    try:
        oidc_client.save_tokens(oidc_client.Tokens(
            access_token="a", refresh_token="r", expires_at=1.0,
            issuer_url="i", client_id="c",
        ))
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "t.json").stat().st_mode) == 0o600


def test_save_vk_created_0600(tmp_path, monkeypatch):
    from gateway_cli_oidc import oidc_client

    monkeypatch.setenv("GATEWAY_CLI_VK_CACHE", str(tmp_path / "v.json"))
    oidc_client.save_vk_cache(oidc_client.CachedVK(
        virtual_key="vk", expires_at=1.0, issuer_url="i", admin_api_url="a",
    ))
    assert stat.S_IMODE((tmp_path / "v.json").stat().st_mode) == 0o600


def test_login_secure_write_0600(tmp_path, monkeypatch):
    """cli.login's own token store uses the same atomic-0600 helper."""
    from cli import login

    monkeypatch.setattr(login, "oidc_tokens_path", lambda: tmp_path / "t.json")
    login.save_tokens(login.Tokens(
        access_token="a", refresh_token=None, expires_at=1.0,
        issuer_url="i", client_id="c",
    ))
    assert stat.S_IMODE((tmp_path / "t.json").stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# naive server timestamps are UTC
# ---------------------------------------------------------------------------

def test_login_vk_naive_expiry_is_utc(monkeypatch):
    """A naive `expires_at` is UTC — .timestamp() on a naive datetime assumes
    LOCAL time, so a KST (UTC+9) host would treat the VK as fresh ~9h past its
    real expiry."""
    import time

    from cli import login

    monkeypatch.setattr(
        login.requests, "post", lambda *a, **k: _fake_exchange_response("2099-01-01T00:00:00")
    )
    vk = login._exchange_jwt_for_vk("https://admin.example.com", "tok", "dev")
    expected = time.mktime(time.strptime("2099-01-01 00:00:00", "%Y-%m-%d %H:%M:%S")) - time.timezone
    # mktime gives local-epoch; subtract local offset to get the UTC epoch that
    # datetime(..., tz=utc).timestamp() produces — assert within a minute.
    assert abs(vk.expires_at - expected) < 60


def test_oidc_client_vk_naive_expiry_is_aware(monkeypatch):
    """The OIDC exchange path must return an aware expires_at — api-key-helper's
    daemon subtracts datetime.now(timezone.utc) and a naive value crashes it."""
    from datetime import datetime, timezone

    from gateway_cli_oidc import oidc_client

    monkeypatch.setattr(
        oidc_client.requests, "post", lambda *a, **k: _fake_exchange_response("2099-01-01T00:00:00")
    )
    vk = oidc_client.exchange_jwt_for_vk("https://admin.example.com", "tok", "dev")
    assert vk.expires_at.tzinfo is not None
    (vk.expires_at - datetime.now(timezone.utc)).total_seconds()


def test_vk_client_naive_expiry_is_aware(monkeypatch):
    """The STS-mode vk_client exchange has the same naive/aware crash surface."""
    from datetime import datetime, timezone

    from api_key_helper import vk_client

    monkeypatch.setattr(
        vk_client.requests, "post", lambda *a, **k: _fake_exchange_response("2099-01-01T00:00:00")
    )
    resp = vk_client.request_virtual_key(_HelperCfg(), "sts", "dev")
    assert resp.expires_at.tzinfo is not None
    (resp.expires_at - datetime.now(timezone.utc)).total_seconds()


def test_vk_client_missing_expiry_is_aware(monkeypatch):
    """A response without expires_at falls back to an aware datetime."""
    from api_key_helper import vk_client

    monkeypatch.setattr(
        vk_client.requests, "post", lambda *a, **k: _fake_exchange_response(None)
    )
    resp = vk_client.request_virtual_key(_HelperCfg(), "sts", "dev")
    assert resp.expires_at.tzinfo is not None


def test_sso_expiry_naive_is_aware(monkeypatch):
    """AWS_CREDENTIAL_EXPIRATION without an offset normalises to UTC."""
    import subprocess

    from api_key_helper import sso

    class _Result:
        returncode = 0
        stdout = "AWS_CREDENTIAL_EXPIRATION=2099-01-01T00:00:00\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
    expires_at = sso.get_sso_session_expiry()
    assert expires_at is not None
    assert expires_at.tzinfo is not None
