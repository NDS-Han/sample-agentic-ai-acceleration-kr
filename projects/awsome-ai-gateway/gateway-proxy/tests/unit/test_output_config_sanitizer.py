"""Bedrock 이 모르는 `output_config.format` 을 걷어낸다 (2026-09-16 US 실측: Cowork 400 ×4)."""

from __future__ import annotations

from app.services.thinking_normalizer import sanitize_output_config


def test_format_is_dropped_but_effort_survives():
    body = {
        "messages": [],
        "output_config": {"effort": "high", "format": {"type": "json_schema", "schema": {}}},
    }
    out = sanitize_output_config(body, request_id="r1")
    assert out is body
    assert body["output_config"] == {"effort": "high"}


def test_output_config_with_only_format_is_removed_entirely():
    body = {"messages": [], "output_config": {"format": {"type": "json_schema", "schema": {}}}}
    sanitize_output_config(body)
    assert "output_config" not in body


def test_untouched_when_absent_or_already_clean():
    a = {"messages": []}
    sanitize_output_config(a)
    assert a == {"messages": []}
    b = {"messages": [], "output_config": {"effort": "low"}}
    sanitize_output_config(b)
    assert b["output_config"] == {"effort": "low"}


def test_non_object_output_config_is_removed():
    body = {"messages": [], "output_config": "weird"}
    sanitize_output_config(body)
    assert "output_config" not in body
