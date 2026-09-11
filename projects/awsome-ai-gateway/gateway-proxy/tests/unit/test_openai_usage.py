# Copyright 2026 © Amazon.com and Affiliates.
"""Pin the ONE OpenAI-dialect usage parser shared by both Bedrock planes and both wires.

The point of ``providers/openai_usage.py`` is that four billing call sites agree; a test
that only checked one wire would let the other drift, which is exactly what happened before
the module existed. So the load-bearing case here is
:func:`test_responses_and_chat_agree_on_the_same_call` — same call, two dialects, identical
TokenUsage — plus the mutual-exclusivity invariant that cost_recorder depends on.
"""
from __future__ import annotations

import pytest

from app.providers.openai_usage import extract_chat_usage, extract_responses_usage
from app.schemas.domain import TokenUsage


def _responses(input_total, cached=0, cache_write=0, output=0, reasoning=0, total=None):
    u = {
        "input_tokens": input_total,
        "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": cache_write},
        "output_tokens": output,
        "output_tokens_details": {"reasoning_tokens": reasoning},
    }
    if total is not None:
        u["total_tokens"] = total
    return {"usage": u}


def _chat(prompt_total, cached=0, cache_write=0, completion=0, reasoning=0, total=None):
    u = {
        "prompt_tokens": prompt_total,
        "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": cache_write},
        "completion_tokens": completion,
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }
    if total is not None:
        u["total_tokens"] = total
    return u


def _assert_exclusive(u: TokenUsage, prompt_total: int) -> None:
    """The invariant cost_recorder bills against: the buckets partition the prompt."""
    assert (
        u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
        == prompt_total
    )
    assert u.input_tokens >= 0


def test_responses_and_chat_agree_on_the_same_call():
    """Same underlying invocation, two wire dialects → identical TokenUsage.

    This is the whole reason the module exists. gpt-5.6 on the runtime plane serves BOTH
    /v1/responses and /v1/chat/completions, so one model can be billed through either
    parser; if they disagree, the same request costs a different amount depending on which
    endpoint the client happened to call.
    """
    r = extract_responses_usage(
        _responses(3617, cached=3615, output=42, reasoning=30, total=3659)
    )
    c = extract_chat_usage(_chat(3617, cached=3615, completion=42, reasoning=30, total=3659))
    assert r == c


def test_cache_read_is_carved_out_of_the_prompt_not_added():
    # Measured live: input_tokens never grows past the prompt, so a cached token is one of
    # the 3617 rather than an extra charge. Billing 3617 at the input rate AND 3615 at the
    # cache-read rate double-charges the cached prefix.
    u = extract_responses_usage(_responses(3617, cached=3615, output=10, total=3627))
    assert u.input_tokens == 2
    assert u.cache_read_input_tokens == 3615
    assert u.cache_creation_input_tokens == 0
    _assert_exclusive(u, 3617)


def test_cache_write_is_also_carved_out():
    # The first call under a prompt_cache_key reports cache_write_tokens, nested in the
    # same grand total. Leaving it inside input_tokens under-bills by ~20% because the
    # published cache-write rate is 1.25x input.
    u = extract_responses_usage(_responses(3617, cache_write=3615, output=10, total=3627))
    assert u.input_tokens == 2
    assert u.cache_creation_input_tokens == 3615
    assert u.cache_read_input_tokens == 0
    _assert_exclusive(u, 3617)


def test_reasoning_is_a_submetric_not_added_to_output():
    u = extract_chat_usage(_chat(10, completion=100, reasoning=90, total=110))
    assert u.output_tokens == 100  # NOT 190
    assert u.reasoning_tokens == 90


def test_provider_total_is_preserved_verbatim():
    """total_tokens comes from the provider, so the split must not silently restate it."""
    u = extract_responses_usage(_responses(100, cached=40, output=7, total=107))
    assert u.total_tokens == 107


def test_total_is_derived_when_absent():
    u = extract_responses_usage(_responses(5, output=7))
    assert u.total_tokens == 12


def test_vllm_shape_without_details_is_unchanged():
    """OPENMODEL (vLLM) sends only the three top-level counters — the split is a no-op."""
    u = extract_chat_usage({"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14})
    assert (u.input_tokens, u.output_tokens, u.total_tokens) == (11, 3, 14)
    assert u.cache_read_input_tokens == 0
    assert u.cache_creation_input_tokens == 0


@pytest.mark.parametrize(
    "usage",
    [
        None,
        "not-a-dict",
        {},
        {"prompt_tokens": None, "completion_tokens": None},
        {"prompt_tokens": "abc", "prompt_tokens_details": None},
    ],
)
def test_malformed_usage_degrades_to_zero_never_raises(usage):
    """A usage object we cannot parse must mean "no usage recorded", not a 500.

    The model already answered and charged for the request by this point, so raising here
    would turn a metering gap into a client-visible failure.
    """
    assert extract_chat_usage(usage) == TokenUsage()
    assert extract_responses_usage({"usage": usage}) == TokenUsage()


def test_absent_usage_object_is_zero_not_an_error():
    assert extract_responses_usage({"output": []}) == TokenUsage()
    assert extract_responses_usage({}) == TokenUsage()


def test_corrupt_counters_exceeding_the_total_stay_non_negative():
    """Upstream corruption must not produce a negative input that reaches budget deduction.

    cache_read is clamped first because it is the money-dominant bucket (0.1x input) and
    the one a provider reports on every steady-state request.
    """
    u = extract_responses_usage(_responses(100, cached=900, cache_write=900, output=1))
    assert u.input_tokens == 0
    assert u.cache_read_input_tokens == 100
    assert u.cache_creation_input_tokens == 0
    _assert_exclusive(u, 100)


def test_negative_counters_are_floored():
    u = extract_chat_usage(_chat(-5, cached=-1, completion=-2))
    assert (u.input_tokens, u.cache_read_input_tokens, u.output_tokens) == (0, 0, 0)
