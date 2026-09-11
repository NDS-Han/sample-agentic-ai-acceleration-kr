# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Drift guard: the two ``statusline.formatter`` copies name models identically.

``statusline/formatter.py`` is vendored twice — once in ``gateway-cli/`` (the source
tree developers run) and once here (what PyInstaller freezes into the shipped installer).
The copies are *not* byte-identical and are not meant to be: this one's fallback splits on
``"."``, the other's on ``"-"``. That divergence is exactly what broke GPT-5.6: after
migration 0032 added the standard-runtime aliases, ``gpt-5.6-terra`` rendered as "Terra"
in the source copy and ``"6-terra"`` here, so the same spend was labelled two ways
depending on which binary the user had.

So these tests pin the observable contract rather than the text:

* every alias the gateway can put in a ``usage_logs`` row renders to the same display
  name in both copies, and
* the two Bedrock planes stay distinguishable — ``gpt-5.6-terra`` (standard runtime,
  SigV4 + CRIS) and ``codex-gpt-5.6-terra`` (Mantle, bearer) are separate catalogue rows
  priced separately, and only the runtime one is captured by Bedrock invocation logging,
  so collapsing both to "Terra" would hide which plane the money and the audit trail are
  on.

The cross-copy comparison skips (rather than fails) when the sibling ``gateway-cli/``
tree is absent, so the packaging directory still tests standalone.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from statusline.formatter import (
    _MODEL_COLOR,
    Severity,
    StatuslineState,
    _short_name,
    format_status,
)
from statusline.usage_client import ModelUsage, UsageInfo

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Aliases the catalogue can hand the statusline, per migration:
#   0025 → codex-gpt-5.6-* (Mantle plane)   0032 → gpt-5.6-* (standard runtime plane)
_RUNTIME_ALIASES = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
_MANTLE_ALIASES = ("codex-gpt-5.6-sol", "codex-gpt-5.6-terra", "codex-gpt-5.6-luna")
_CLAUDE_ALIASES = (
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claudecode-opus-4.8",
    "cowork-opus-4-8",
)
_ALL_ALIASES = _RUNTIME_ALIASES + _MANTLE_ALIASES + _CLAUDE_ALIASES


def _plain(s: str) -> str:
    """Strip ANSI so assertions read as the text a user sees."""
    return _ANSI.sub("", s)


def _load_source_copy() -> ModuleType | None:
    """Import ``gateway-cli/src/statusline/formatter.py`` under a private module name.

    Both trees expose the package as ``statusline``, so a plain import would just return
    this copy again — the file has to be loaded by path. Its one dependency
    (``statusline.usage_client``) resolves to the installed copy, which is fine: the two
    ``ModelUsage``/``UsageInfo`` dataclasses are field-for-field identical, and a drift
    there would surface as an AttributeError in the format_status comparison below.
    """
    repo_root = Path(__file__).resolve().parents[5]
    path = repo_root / "gateway-cli" / "src" / "statusline" / "formatter.py"
    if not path.is_file():
        return None
    name = "_gateway_cli_statusline_formatter"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def source_copy() -> ModuleType:
    module = _load_source_copy()
    if module is None:
        pytest.skip("sibling gateway-cli/ source tree not present (standalone packaging run)")
    return module


class TestPackagedDisplayNames:
    """What the frozen installer binary shows, independent of the other copy."""

    def test_claude_tiers(self) -> None:
        assert _short_name("claude-opus-4-8") == "Opus"
        assert _short_name("claude-sonnet-4-6") == "Sonnet"
        assert _short_name("claude-haiku-4-5") == "Haiku"

    def test_runtime_aliases_are_not_split_on_the_version_dot(self) -> None:
        """The regression this file exists for: ``"." in alias`` gave ``"6-terra"``."""
        assert _short_name("gpt-5.6-sol") == "Sol"
        assert _short_name("gpt-5.6-terra") == "Terra"
        assert _short_name("gpt-5.6-luna") == "Luna"

    def test_mantle_aliases_stay_distinguishable(self) -> None:
        assert _short_name("codex-gpt-5.6-sol") == "Sol(M)"
        assert _short_name("codex-gpt-5.6-terra") == "Terra(M)"
        assert _short_name("codex-gpt-5.6-luna") == "Luna(M)"
        for runtime, mantle in zip(_RUNTIME_ALIASES, _MANTLE_ALIASES):
            assert _short_name(runtime) != _short_name(mantle), (runtime, mantle)

    def test_every_gpt_display_name_has_a_colour(self) -> None:
        """An uncoloured name falls back to white and stops matching its Claude siblings."""
        for alias in _RUNTIME_ALIASES + _MANTLE_ALIASES:
            assert _short_name(alias) in _MODEL_COLOR, alias

    def test_breakdown_renders_both_planes_on_one_line(self) -> None:
        state = StatuslineState(
            current=UsageInfo(
                used=Decimal("3.00"),
                limit=Decimal("100.00"),
                percentage=3.0,
                models=[
                    ModelUsage(
                        model="gpt-5.6-terra",
                        cost_usd=Decimal("2.00"),
                        input_tokens=1200,
                        output_tokens=300,
                    ),
                    ModelUsage(
                        model="codex-gpt-5.6-terra",
                        cost_usd=Decimal("1.00"),
                        input_tokens=500,
                    ),
                ],
            ),
            severity=Severity.NORMAL,
            is_online=True,
        )
        out = _plain(format_status(state))
        assert "Terra:$2.00" in out
        assert "Terra(M):$1.00" in out


class TestCrossCopyDrift:
    """The packaged copy and the source copy must agree on what the user sees."""

    def test_short_name_agrees_for_every_catalogue_alias(self, source_copy: ModuleType) -> None:
        mismatches = {
            alias: (_short_name(alias), source_copy._short_name(alias))
            for alias in _ALL_ALIASES
            if _short_name(alias) != source_copy._short_name(alias)
        }
        assert not mismatches, f"display-name drift (packaged, source): {mismatches}"

    def test_colour_tables_cover_the_same_names(self, source_copy: ModuleType) -> None:
        """A name coloured in one copy and not the other is drift a user can see."""
        assert set(_MODEL_COLOR) == set(source_copy._MODEL_COLOR)
        assert _MODEL_COLOR == source_copy._MODEL_COLOR

    def test_rendered_line_is_identical_for_a_mixed_plane_breakdown(
        self, source_copy: ModuleType
    ) -> None:
        models = [
            ModelUsage(model=alias, cost_usd=Decimal(f"{i + 1}.00"), input_tokens=100 * (i + 1))
            for i, alias in enumerate(_ALL_ALIASES)
        ]
        info = UsageInfo(
            used=Decimal("21.00"), limit=Decimal("100.00"), percentage=21.0, models=models
        )
        packaged = format_status(
            StatuslineState(current=info, severity=Severity.NORMAL, is_online=True)
        )
        source = source_copy.format_status(
            source_copy.StatuslineState(
                current=info, severity=source_copy.Severity.NORMAL, is_online=True
            )
        )
        assert packaged == source
