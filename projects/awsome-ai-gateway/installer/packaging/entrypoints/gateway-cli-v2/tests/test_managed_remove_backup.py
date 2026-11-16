# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""T6.1: `disable` snapshots managed-settings.json before it unmerges/deletes it.

The write path already backs up before a merge; the removal path did not, so an
org that regretted disabling had no restore point — in the delete-the-file branch
the state was gone entirely. :func:`cli.managed.remove_gateway_settings` now calls
:func:`cli.managed._backup_existing` first. These tests assert a timestamped
backup is produced in BOTH branches and that reading it reproduces the exact
pre-removal file (the restore path).

To run without elevation on a POSIX dev box we point the managed root at a tmp
dir and force the "Windows filesystem" write path — its helpers (``write_text`` /
``unlink``) are plain filesystem ops, whereas the Unix helpers shell out to
``sudo``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import managed

_ORG_TOP = "orgPolicy"
_MARKER = managed.GATEWAY_MARKER_KEY


@pytest.fixture
def managed_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the managed root to a tmp dir and use plain-file write/remove ops."""
    root = tmp_path / "ClaudeCode"
    root.mkdir()
    monkeypatch.setattr(managed, "_managed_root", lambda: root)
    monkeypatch.setattr(managed, "_targets_windows_filesystem", lambda: True)
    monkeypatch.setenv("GATEWAY_CLI_BACKUP_DIR", str(tmp_path / "backups"))
    return root


def _backups(tmp_backup_dir: Path) -> list[Path]:
    return sorted(tmp_backup_dir.glob("claude-code-managed.managed-settings.json.*.bak"))


def test_remove_backs_up_before_unmerge(managed_root: Path, tmp_path: Path) -> None:
    """A pre-existing org file: backup captures full state; reduced file keeps org keys."""
    original = {
        "env": {"ANTHROPIC_BASE_URL": "https://gw", "ORG_KEEP": "v"},
        "apiKeyHelper": "helper",
        "statusLine": {"type": "command", "command": "sl"},
        _ORG_TOP: "keepme",
        _MARKER: {
            "managed": True,
            "envKeys": ["ANTHROPIC_BASE_URL"],
            "topKeys": ["apiKeyHelper", "statusLine"],
            "fileExisted": True,  # the org had this file before gateway-cli
        },
    }
    mf = managed._managed_file()
    mf.write_text(json.dumps(original), encoding="utf-8")

    assert managed.remove_gateway_settings() is True

    # Restore path: exactly one timestamped backup, and it round-trips the pre-removal file.
    backups = _backups(tmp_path / "backups")
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == original

    # Reduced file: org keys survive, gateway-cli's keys and marker are gone.
    reduced = json.loads(mf.read_text(encoding="utf-8"))
    assert reduced[_ORG_TOP] == "keepme"
    assert reduced["env"] == {"ORG_KEEP": "v"}
    assert _MARKER not in reduced
    assert "apiKeyHelper" not in reduced


def test_remove_backs_up_before_delete(managed_root: Path, tmp_path: Path) -> None:
    """A gateway-cli-created file: it is deleted, so the backup is the only survivor."""
    original = {
        "env": {"ANTHROPIC_BASE_URL": "https://gw"},
        "apiKeyHelper": "helper",
        _MARKER: {
            "managed": True,
            "envKeys": ["ANTHROPIC_BASE_URL"],
            "topKeys": ["apiKeyHelper"],
            "fileExisted": False,  # gateway-cli created it → delete on remove
        },
    }
    mf = managed._managed_file()
    mf.write_text(json.dumps(original), encoding="utf-8")

    assert managed.remove_gateway_settings() is True

    # The file is gone (we created it and nothing remains) ...
    assert not mf.exists()
    # ... but the snapshot survives and reproduces the pre-removal state.
    backups = _backups(tmp_path / "backups")
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == original


# ---------------------------------------------------------------------------
# Diff-regression: pre-fix builds wrote OTEL_EXPORTER_OTLP_HEADERS (a bearer
# credential) into the 0644 managed file. The marker's envKeys never listed it
# again after the fix, so an upgrade-merge that only adds keys would leave the
# stale token in the world-readable file forever. write_gateway_settings now
# scrubs it from the merged env — unless site-extra explicitly re-pins it.
# ---------------------------------------------------------------------------

def test_write_scrubs_stale_otel_bearer(managed_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Upgrade path: a pre-fix bearer in managed env must not survive re-setup."""
    monkeypatch.setattr("cli.managed.managed_extra", lambda: {})
    mf = managed._managed_file()
    mf.write_text(json.dumps({
        "env": {
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer stale",
            "ORG_KEEP": "v",
        },
        _MARKER: {
            "managed": True,
            "envKeys": ["OTEL_EXPORTER_OTLP_HEADERS"],
            "topKeys": [],
            "fileExisted": True,
        },
    }), encoding="utf-8")

    managed.write_gateway_settings(
        gateway_url="https://gw", admin_api_url="https://admin",
        api_key_helper_path="/x/api-key-helper",
        otel_auth_token="fresh-token",
    )

    data = json.loads(mf.read_text(encoding="utf-8"))
    assert data["env"]["ORG_KEEP"] == "v"
    # 스크럽됨 — 신규 토큰도 managed 에 심지 않고 user tier 로만 간다.
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in data["env"]
    # marker 도 이전 envKeys 에 남은 키를 추적하지 않는다.
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in data[_MARKER]["envKeys"]


def test_write_site_extra_explicit_otel_header_survives(
    managed_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator escape hatch: site-extra managed.env OTEL_* pins still win."""
    monkeypatch.setattr("cli.managed.managed_extra", lambda: {
        "env": {"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer site-pin"}
    })
    mf = managed._managed_file()
    mf.write_text(json.dumps({"env": {"ORG_KEEP": "v"}}), encoding="utf-8")

    managed.write_gateway_settings(
        gateway_url="https://gw", admin_api_url="https://admin",
        api_key_helper_path="/x/api-key-helper",
    )

    data = json.loads(mf.read_text(encoding="utf-8"))
    assert data["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer site-pin"
