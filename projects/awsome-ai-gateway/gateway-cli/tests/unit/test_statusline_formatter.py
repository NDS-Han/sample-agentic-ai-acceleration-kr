# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for statusline.formatter — display formatting and severity."""

from __future__ import annotations

from decimal import Decimal

import re

from statusline.formatter import (
    Severity,
    StatuslineState,
    _short_name,
    determine_severity,
    format_status,
)
from statusline.usage_client import ModelUsage, UsageInfo

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    """Strip ANSI so assertions read as the text a user sees.

    The formatter emits colour codes; asserting on the raw string would make every test
    a test of the escape sequences instead of the layout (which is how the assertions
    below drifted out of date once colour was added).
    """
    return _ANSI.sub("", s)


class TestDetermineSeverity:
    def test_normal(self) -> None:
        assert determine_severity(50.0, True) == Severity.NORMAL

    def test_warning_at_80(self) -> None:
        assert determine_severity(80.0, True) == Severity.WARNING

    def test_warning_at_99(self) -> None:
        assert determine_severity(99.9, True) == Severity.WARNING

    def test_critical_at_100(self) -> None:
        assert determine_severity(100.0, True) == Severity.CRITICAL

    def test_critical_over_100(self) -> None:
        assert determine_severity(120.0, True) == Severity.CRITICAL

    def test_offline(self) -> None:
        assert determine_severity(50.0, False) == Severity.OFFLINE

    def test_zero_percent(self) -> None:
        assert determine_severity(0.0, True) == Severity.NORMAL


class TestFormatStatus:
    def test_normal(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("12.50"),
                limit=Decimal("100.00"),
                percentage=12.5,
            ),
            severity=Severity.NORMAL,
            is_online=True,
        )
        assert _plain(format_status(state)) == "$12.50/$100.00(12%)"

    def test_warning(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("82.00"),
                limit=Decimal("100.00"),
                percentage=82.0,
            ),
            severity=Severity.WARNING,
            is_online=True,
        )
        assert _plain(format_status(state)) == "$82.00/$100.00(82%) [!]"

    def test_critical(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("100.00"),
                limit=Decimal("100.00"),
                percentage=100.0,
            ),
            severity=Severity.CRITICAL,
            is_online=True,
        )
        assert _plain(format_status(state)) == "$100.00/$100.00(100%) [!!]"

    def test_offline_with_data(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("12.50"),
                limit=Decimal("100.00"),
                percentage=12.5,
            ),
            severity=Severity.OFFLINE,
            is_online=False,
        )
        assert _plain(format_status(state)) == "$12.50/$100.00(12%) [offline]"

    def test_no_data(self) -> None:
        state = StatuslineState()
        assert _plain(format_status(state)) == "-- / -- (--)"

    def test_decimal_formatting(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("0.10"),
                limit=Decimal("50.00"),
                percentage=0.2,
            ),
            severity=Severity.NORMAL,
            is_online=True,
        )
        assert _plain(format_status(state)) == "$0.10/$50.00(0%)"


class TestShortName:
    """A statusline row has to name the model AND its Bedrock plane."""

    def test_claude_tiers_unchanged(self) -> None:
        assert _short_name("claude-sonnet-4-6") == "Sonnet"
        assert _short_name("claudecode-opus-4.8") == "Opus"
        assert _short_name("claude-haiku-4-5") == "Haiku"

    def test_gpt56_runtime_aliases_use_the_tier_name(self) -> None:
        # Without the explicit tier branch the generic tail split gives "Terra" here but
        # "6-terra" in the installer copy (the alias contains a dot) — the two clients
        # would label the same spend differently.
        assert _short_name("gpt-5.6-sol") == "Sol"
        assert _short_name("gpt-5.6-terra") == "Terra"
        assert _short_name("gpt-5.6-luna") == "Luna"

    def test_mantle_aliases_are_distinguishable_from_runtime_ones(self) -> None:
        """The two planes must not collapse to one label.

        ``gpt-5.6-terra`` and ``codex-gpt-5.6-terra`` are the same model reached two ways.
        They are separate usage_logs rows, and only the runtime one is captured by Bedrock
        invocation logging — so a statusline that shows one "Terra" line for both would
        make plane-level spend and auditability invisible.
        """
        assert _short_name("codex-gpt-5.6-terra") == "Terra(M)"
        assert _short_name("codex-gpt-5.6-sol") == "Sol(M)"
        assert _short_name("codex-gpt-5.6-luna") == "Luna(M)"
        assert _short_name("gpt-5.6-terra") != _short_name("codex-gpt-5.6-terra")

    def test_both_planes_are_coloured(self) -> None:
        """Every name _short_name can produce for GPT-5.6 has a colour, or the row falls
        back to white and stops matching its Claude siblings' styling."""
        from statusline.formatter import _MODEL_COLOR

        for alias in (
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "codex-gpt-5.6-sol",
            "codex-gpt-5.6-terra",
            "codex-gpt-5.6-luna",
        ):
            assert _short_name(alias) in _MODEL_COLOR, alias

    def test_model_breakdown_renders_both_planes(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("3.00"),
                limit=Decimal("100.00"),
                percentage=3.0,
                models=[
                    ModelUsage(model="gpt-5.6-terra", cost_usd=Decimal("2.00"),
                               input_tokens=1200, output_tokens=300),
                    ModelUsage(model="codex-gpt-5.6-terra", cost_usd=Decimal("1.00"),
                               input_tokens=500),
                ],
            ),
            severity=Severity.NORMAL,
            is_online=True,
        )
        out = _plain(format_status(state))
        assert "Terra:$2.00" in out
        assert "Terra(M):$1.00" in out
        assert "in:1.2K" in out and "out:300" in out
