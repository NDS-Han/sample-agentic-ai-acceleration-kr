"""Bedrock 이 모르는 `output_config.format` 을 걷어낸다 — 계열별 (2026-09-16 US 실측).

opus-5·sonnet-5(adaptive): format → 400 "Extra inputs are not permitted" → 걷어낸다.
haiku-4-5(legacy): format → 200, 스키마대로 응답 → 그대로 둔다.
"""

from __future__ import annotations

from app.services.thinking_normalizer import sanitize_output_config

OPUS5 = "us.anthropic.claude-opus-5"
SONNET5 = "us.anthropic.claude-sonnet-5"
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
FMT = {"type": "json_schema", "schema": {"type": "object", "additionalProperties": False}}


def test_adaptive_family_drops_format_but_keeps_effort():
    for mid in (OPUS5, SONNET5):
        body = {"messages": [], "output_config": {"effort": "high", "format": FMT}}
        out = sanitize_output_config(body, mid, request_id="r1")
        assert out is body
        assert body["output_config"] == {"effort": "high"}, mid


def test_adaptive_family_removes_output_config_entirely_when_only_format():
    body = {"messages": [], "output_config": {"format": FMT}}
    sanitize_output_config(body, OPUS5)
    assert "output_config" not in body


def test_legacy_family_keeps_format_because_bedrock_honors_it():
    body = {"messages": [], "output_config": {"format": FMT}}
    sanitize_output_config(body, HAIKU)
    assert body["output_config"] == {"format": FMT}


def test_alias_is_enough_when_provider_id_unknown_yet():
    adaptive = {"messages": [], "output_config": {"format": FMT}}
    sanitize_output_config(adaptive, alias="claude-opus-5")
    assert "output_config" not in adaptive
    legacy = {"messages": [], "output_config": {"format": FMT}}
    sanitize_output_config(legacy, alias="claude-haiku-4-5-20251001")
    assert legacy["output_config"] == {"format": FMT}


def test_unknown_family_is_conservative_and_drops_format():
    body = {"messages": [], "output_config": {"format": FMT, "effort": "low"}}
    sanitize_output_config(body, None)
    assert body["output_config"] == {"effort": "low"}


def test_untouched_when_absent_or_already_clean():
    a = {"messages": []}
    sanitize_output_config(a, OPUS5)
    assert a == {"messages": []}
    b = {"messages": [], "output_config": {"effort": "low"}}
    sanitize_output_config(b, OPUS5)
    assert b["output_config"] == {"effort": "low"}


def test_non_object_output_config_is_removed():
    body = {"messages": [], "output_config": "weird"}
    sanitize_output_config(body, OPUS5)
    assert "output_config" not in body
