# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Usage display formatter and severity determination (BR-SL-02, BR-SL-03)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from statusline.usage_client import UsageInfo

# Model alias → short display name (prefix-match via _short_name())
_MODEL_SHORT = {
    "opus": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
}

# GPT-5.6 tier names, handled separately from _MODEL_SHORT because the same tier is
# served by two aliases on two Bedrock planes and the display has to tell them apart.
_GPT_TIERS = ("sol", "terra", "luna")


def _short_name(alias: str) -> str:
    """Extract display name from model alias (e.g. 'claudecode-opus-4.8' → 'Opus').

    GPT-5.6 needs an explicit branch rather than the generic tail split: the aliases
    contain a dot (``gpt-5.6-terra``) and the ``"." in alias`` fallback below would
    render them as "6-terra". The Mantle-plane alias (``codex-gpt-5.6-terra``, migration
    0025) is suffixed "(M)" to distinguish it from the standard-runtime alias
    (``gpt-5.6-terra``, migration 0032): they are separate ``usage_logs`` rows, priced
    per plane, and only the runtime one appears in Bedrock invocation logs.
    """
    low = alias.lower()
    for key, name in _MODEL_SHORT.items():
        if key in low:
            return name
    for tier in _GPT_TIERS:
        if tier in low:
            return f"{tier.capitalize()}(M)" if low.startswith("codex-") else tier.capitalize()
    return alias.split(".")[-1] if "." in alias else alias.split("-")[-1]


class Severity(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    OFFLINE = "offline"


@dataclass
class StatuslineState:
    current: Optional[UsageInfo] = None
    severity: Severity = Severity.OFFLINE
    is_online: bool = False
    last_success_at: Optional[datetime] = None
    error_count: int = 0


def determine_severity(percentage: float, is_online: bool) -> Severity:
    if not is_online:
        return Severity.OFFLINE
    if percentage >= 100:
        return Severity.CRITICAL
    if percentage >= 80:
        return Severity.WARNING
    return Severity.NORMAL


def _fmt_tokens(n: int) -> str:
    """Format token count: 1234567 → 1.23M, 12345 → 12.3K, 123 → 123."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# ANSI color codes
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_BLUE = "\033[34m"
_WHITE = "\033[37m"

_SEVERITY_COLOR = {
    Severity.NORMAL: _GREEN,
    Severity.WARNING: _YELLOW,
    Severity.CRITICAL: _RED,
    Severity.OFFLINE: _DIM,
}

_MODEL_COLOR = {
    "Opus": _MAGENTA,
    "Sonnet": _CYAN,
    "Haiku": _BLUE,
    # GPT-5.6 tiers, best→cheapest. Colours are reused from the Claude set on purpose:
    # no row ever shares both a colour and a name with a Claude row, and new ANSI codes
    # would cost more legibility in a one-line statusline than they buy.
    "Sol": _MAGENTA,
    "Terra": _CYAN,
    "Luna": _BLUE,
    "Sol(M)": _MAGENTA,
    "Terra(M)": _CYAN,
    "Luna(M)": _BLUE,
}


def format_status(state: StatuslineState) -> str:
    """Format statusline with model breakdown and ANSI colors."""
    if state.current is None:
        return f"{_DIM}-- / -- (--){_RESET}"

    info = state.current
    color = _SEVERITY_COLOR.get(state.severity, _WHITE)
    pct = f"{info.percentage:.0f}"

    header = f"{color}{_BOLD}${info.used:.2f}/${info.limit:.2f}({pct}%){_RESET}"

    suffix_map = {
        Severity.NORMAL: "",
        Severity.WARNING: f" {_YELLOW}{_BOLD}[!]{_RESET}",
        Severity.CRITICAL: f" {_RED}{_BOLD}[!!]{_RESET}",
        Severity.OFFLINE: f" {_DIM}[offline]{_RESET}",
    }
    header += suffix_map.get(state.severity, "")

    if not info.models:
        return header

    sorted_models = sorted(info.models, key=lambda m: m.cost_usd, reverse=True)

    parts = [header]
    for m in sorted_models:
        short = _short_name(m.model)
        mc = _MODEL_COLOR.get(short, _WHITE)

        tokens = []
        if m.input_tokens:
            tokens.append(f"in:{_fmt_tokens(m.input_tokens)}")
        if m.cache_write_tokens:
            tokens.append(f"cw:{_fmt_tokens(m.cache_write_tokens)}")
        if m.cache_read_tokens:
            tokens.append(f"cr:{_fmt_tokens(m.cache_read_tokens)}")
        if m.output_tokens:
            tokens.append(f"out:{_fmt_tokens(m.output_tokens)}")

        token_str = f" {_DIM}{' '.join(tokens)}{_RESET}" if tokens else ""
        parts.append(f"{mc}{_BOLD}{short}{_RESET}:${m.cost_usd:.2f}{token_str}")

    return f" {_DIM}|{_RESET} ".join(parts)
