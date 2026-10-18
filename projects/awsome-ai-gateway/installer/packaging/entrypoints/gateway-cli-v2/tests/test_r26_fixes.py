# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R26 regression tests — findings fixed in the gateway-cli/cli-v2 review round.

Covered fixes:
  * token/VK caches created 0600 atomically (no write-then-chmod window)
  * api-key-helper warns (not silently falls back to STS) on partial OIDC env
  * env --persist escapes shell metacharacters and is not suppressed by comments
  * teardown's rc strip is scoped to our marker block
  * verify /health treats non-2xx as WARN (no false-green 404)
  * managed disable restores org values the gateway overwrote
  * user-settings revert restores values reconcile stripped
  * --model gating identical between `setup` and `onboard`
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from cli import env as env_mod
from cli import managed, teardown
from cli.main import _gate_model
from cli.verify import CheckStatus, VerifyOutcome, _check_url_health

# ---------------------------------------------------------------------------
# 0600 atomic secure writes (TOCTOU fix)
# ---------------------------------------------------------------------------

def test_save_tokens_created_0600(tmp_path, monkeypatch):
    """A fresh token cache is 0600 from creation — never umask-readable."""
    from gateway_cli_oidc import oidc_client

    monkeypatch.setenv("GATEWAY_CLI_OIDC_CACHE", str(tmp_path / "t.json"))
    monkeypatch.setenv("GATEWAY_CLI_VK_CACHE", str(tmp_path / "v.json"))
    # Worst-case permissive umask: the file must still be 0600 immediately.
    old = __import__("os").umask(0o022)
    try:
        oidc_client.save_tokens(oidc_client.Tokens(
            access_token="a", refresh_token="r", expires_at=1.0,
            issuer_url="i", client_id="c",
        ))
    finally:
        __import__("os").umask(old)
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
# Partial OIDC env → warn instead of silent STS fallback
# ---------------------------------------------------------------------------

def test_detect_mode_warns_on_partial_oidc(monkeypatch, capsys):
    from api_key_helper.main import _detect_mode

    monkeypatch.setenv("OIDC_ISSUER_URL", "https://idp")
    monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    assert _detect_mode(None) == "sts"
    err = capsys.readouterr().err
    assert "OIDC_CLIENT_ID" in err and "STS" in err


def test_detect_mode_full_oidc_quiet(monkeypatch, capsys):
    from api_key_helper.main import _detect_mode

    monkeypatch.setenv("OIDC_ISSUER_URL", "https://idp")
    monkeypatch.setenv("OIDC_CLIENT_ID", "cid")
    assert _detect_mode(None) == "oidc"
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# env --persist: escaping + anchored skip check
# ---------------------------------------------------------------------------

def test_persist_posix_escapes_shell_metachars(tmp_path):
    rc = tmp_path / ".bashrc"
    env_mod._persist_posix(rc, {"OIDC_ISSUER_URL": 'https://idp/$(rm -rf)/`x`"q"'})
    line = [ln for ln in rc.read_text().splitlines() if "OIDC_ISSUER_URL" in ln][0]
    assert "\\$" in line and "\\`" in line and '\\"' in line


def test_persist_posix_comment_does_not_suppress(tmp_path):
    """A commented-out mention must not be mistaken for a live definition."""
    rc = tmp_path / ".bashrc"
    rc.write_text("# OIDC_ISSUER_URL=https://old\n", encoding="utf-8")
    appended = env_mod._persist_posix(rc, {"OIDC_ISSUER_URL": "https://new"})
    assert appended == ["OIDC_ISSUER_URL"]


def test_persist_posix_real_export_still_skips(tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text('export OIDC_ISSUER_URL="https://old"\n', encoding="utf-8")
    assert env_mod._persist_posix(rc, {"OIDC_ISSUER_URL": "https://new"}) == []


# ---------------------------------------------------------------------------
# teardown rc strip is marker-block scoped
# ---------------------------------------------------------------------------

def test_strip_posix_rc_preserves_user_export_outside_block(tmp_path):
    """A user-authored export of one of our vars is NOT ours to delete."""
    rc = tmp_path / ".bashrc"
    marker_vars = next(iter(__import__("cli.manifest", fromlist=["x"]).os_persisted_keys()))
    rc.write_text(
        f'export {marker_vars}="https://mine"\n'
        "# LLM Gateway — added by gateway-cli env --persist\n"
        'export OIDC_ISSUER_URL="https://idp"\n',
        encoding="utf-8",
    )
    # The user's line precedes the block — it must survive the strip.
    assert teardown._strip_posix_rc(rc)
    text = rc.read_text()
    assert f'export {marker_vars}="https://mine"' in text
    assert "gateway-cli env --persist" not in text


# ---------------------------------------------------------------------------
# verify /health: non-2xx is WARN, not OK
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, code):
        self.status_code = code

    def json(self):
        return {"status": "ok"}


def test_health_404_is_warn_not_ok(monkeypatch):
    monkeypatch.setattr("cli.verify.requests.get", lambda *a, **k: _Resp(404))
    outcome = VerifyOutcome()
    _check_url_health("gw", "https://x", outcome)
    assert outcome.checks[0].status == CheckStatus.WARN


def test_health_2xx_still_ok(monkeypatch):
    monkeypatch.setattr("cli.verify.requests.get", lambda *a, **k: _Resp(200))
    outcome = VerifyOutcome()
    _check_url_health("gw", "https://x", outcome)
    assert outcome.checks[0].status == CheckStatus.OK


def test_health_5xx_still_fail(monkeypatch):
    monkeypatch.setattr("cli.verify.requests.get", lambda *a, **k: _Resp(503))
    outcome = VerifyOutcome()
    _check_url_health("gw", "https://x", outcome)
    assert outcome.checks[0].status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# managed disable restores org values we overwrote
# ---------------------------------------------------------------------------

@pytest.fixture
def managed_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "ClaudeCode"
    root.mkdir()
    monkeypatch.setattr(managed, "_managed_root", lambda: root)
    monkeypatch.setattr(managed, "_targets_windows_filesystem", lambda: True)
    monkeypatch.setenv("GATEWAY_CLI_BACKUP_DIR", str(tmp_path / "backups"))
    return root


def test_remove_restores_org_prior_values(managed_root: Path, tmp_path: Path) -> None:
    """Org had apiKeyHelper + env.NO_PROXY before us; disable restores them."""
    marker = managed.GATEWAY_MARKER_KEY
    # Earliest snapshot = true pre-gateway org state.
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "claude-code-managed.managed-settings.json.20250101T000000.bak").write_text(
        json.dumps({
            "env": {"NO_PROXY": "corp.internal", "ORG_KEEP": "v"},
            "apiKeyHelper": "/org/own-helper",
        }),
        encoding="utf-8",
    )
    mf = managed._managed_file()
    mf.write_text(json.dumps({
        "env": {"NO_PROXY": "gw-list", "ANTHROPIC_BASE_URL": "https://gw", "ORG_KEEP": "v"},
        "apiKeyHelper": "/our/helper",
        marker: {
            "managed": True,
            "envKeys": ["NO_PROXY", "ANTHROPIC_BASE_URL"],
            "topKeys": ["apiKeyHelper", "statusLine"],
            "fileExisted": True,
        },
    }), encoding="utf-8")

    assert managed.remove_gateway_settings() is True

    reduced = json.loads(mf.read_text(encoding="utf-8"))
    assert reduced["apiKeyHelper"] == "/org/own-helper"      # restored, not dropped
    assert reduced["env"]["NO_PROXY"] == "corp.internal"    # org's value back
    assert "ANTHROPIC_BASE_URL" not in reduced["env"]       # ours, no prior → removed
    assert reduced["env"]["ORG_KEEP"] == "v"                # untouched


def test_remove_without_snapshot_still_pops(managed_root: Path) -> None:
    """No backup history → plain removal (unchanged legacy behaviour)."""
    marker = managed.GATEWAY_MARKER_KEY
    mf = managed._managed_file()
    mf.write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "https://gw"},
        "apiKeyHelper": "/our/helper",
        marker: {"managed": True, "envKeys": ["ANTHROPIC_BASE_URL"],
                 "topKeys": ["apiKeyHelper"], "fileExisted": True},
    }), encoding="utf-8")
    assert managed.remove_gateway_settings() is True
    reduced = json.loads(mf.read_text(encoding="utf-8"))
    assert "apiKeyHelper" not in reduced
    assert "env" not in reduced  # emptied env block is dropped


# ---------------------------------------------------------------------------
# user-settings revert restores reconcile-stripped values
# ---------------------------------------------------------------------------

def test_revert_restores_stripped_bypass_keys(tmp_path, monkeypatch):
    """Pre-setup ANTHROPIC_API_KEY stripped by reconcile comes back on clear."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setenv("GATEWAY_CLI_BACKUP_DIR", str(backup_dir))
    # Earliest snapshot = pre-setup state incl. the user's own API key.
    (backup_dir / "claude-code.settings.json.20250101T000000.bak").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-user-own"}}),
        encoding="utf-8",
    )
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "apiKeyHelper": "/helper",
        "env": {"ANTHROPIC_BASE_URL": "https://gw"},
    }), encoding="utf-8")

    result = teardown.revert_user_settings(settings)

    data = json.loads(settings.read_text())
    assert data["env"]["ANTHROPIC_API_KEY"] == "sk-user-own"
    assert "env.ANTHROPIC_API_KEY" in result.restored


def test_revert_does_not_resurrect_key_user_readded(tmp_path, monkeypatch):
    """A bypass key the user re-added after setup is left alone."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setenv("GATEWAY_CLI_BACKUP_DIR", str(backup_dir))
    (backup_dir / "claude-code.settings.json.20250101T000000.bak").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-old"}}),
        encoding="utf-8",
    )
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-new", "ANTHROPIC_BASE_URL": "x"}}),
        encoding="utf-8",
    )
    teardown.revert_user_settings(settings)
    data = json.loads(settings.read_text())
    assert data["env"]["ANTHROPIC_API_KEY"] == "sk-new"  # current value kept


# ---------------------------------------------------------------------------
# --model gating parity between setup and onboard
# ---------------------------------------------------------------------------

def test_gate_model_accepts_unknown_alias_without_roster():
    """No --available-models → any valid alias accepted (site alias needs no rebuild)."""
    import click
    assert _gate_model("claudecode-sonnet-5", None) is None
    with pytest.raises(click.ClickException):
        _gate_model("us.anthropic.claude-sonnet-4", None)  # Bedrock profile id → reject


def test_gate_model_enforces_membership_with_roster():
    import click
    with pytest.raises(click.ClickException):
        _gate_model("claude-haiku-4-5", "claude-sonnet-4-6")
    assert _gate_model("claude-sonnet-4-6", "claude-sonnet-4-6,claude-haiku-4-5") == [
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ]


def test_gate_model_rejects_bad_roster_alias():
    import click
    with pytest.raises(click.ClickException):
        _gate_model("claude-sonnet-4-6", "claude-sonnet-4-6,us.anthropic.bad")


# ---------------------------------------------------------------------------
# naive server timestamps are UTC (login VK exchange + oidc_client exchange)
# ---------------------------------------------------------------------------

def _fake_exchange_response(expires_at: str | None):
    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            data = {"virtual_key": "vk-x"}
            if expires_at is not None:
                data["expires_at"] = expires_at
            return data

    return _Resp()


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
    """The OIDC exchange path must return an aware expires_at — api-key-helper
    subtracts datetime.now(timezone.utc) and a naive value crashes the daemon."""
    from datetime import datetime, timezone

    from gateway_cli_oidc import oidc_client

    monkeypatch.setattr(
        oidc_client.requests, "post", lambda *a, **k: _fake_exchange_response("2099-01-01T00:00:00")
    )
    vk = oidc_client.exchange_jwt_for_vk("https://admin.example.com", "tok", "dev")
    assert vk.expires_at.tzinfo is not None
    (vk.expires_at - datetime.now(timezone.utc)).total_seconds()
