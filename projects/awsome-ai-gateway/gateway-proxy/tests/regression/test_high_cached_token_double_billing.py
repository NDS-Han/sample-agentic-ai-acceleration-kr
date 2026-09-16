# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: cached prompt tokens were billed TWICE on the OpenAI Responses dialect.

Root cause (pre-existing, documented in db/versions/0025_add_gpt56_aliases.py:55-95 but not
fixed there): OpenAI Responses ``usage.input_tokens`` is the GRAND TOTAL prompt count and
ALREADY INCLUDES ``input_tokens_details.cached_tokens``. The Anthropic/Bedrock-Converse wire
reports the two disjointly. Three Responses parsers copied the cache-inclusive value straight
into ``TokenUsage.input_tokens`` while ALSO mapping ``cached_tokens`` to
``cache_read_input_tokens``, so ``cost_recorder.calculate_cost`` billed every cached token at
``1.1x`` the input rate when ``0.1x`` is correct — an **11x** error on the cached portion.

Measured on the dev fleet 2026-08-26 (``usage.usage_logs``, provider BEDROCK_MANTLE_OPENAI,
alias ``codex-gpt``, 63 requests): recorded $2.670309 vs correct $1.662318 = **1.6064x**
over-charge. ``cost_usd`` feeds Redis ``budget_deduct`` → next-request ``budget_check`` → 429,
so the over-charge also burned user budgets ~61% too fast.

These tests pin all three parsers, the dialect parity between them, the untouched Anthropic
path (negative control), the wire-vs-billing split, and the TPM definition.
"""

from __future__ import annotations

import json
from decimal import Decimal

from app.providers.bedrock_adapter import _extract_bedrock_usage
from app.providers.mantle_openai_adapter import _extract_responses_usage
from app.schemas.domain import ModelPricingSchema, TokenUsage, split_cached_input
from app.services.cost_recorder import calculate_cost
from app.services.rate_limit_scope import compute_tpm_incr
from app.services.web_search_loop import _finalize_responses_obj

# Live dev rates for alias `codex-gpt` (model.model_pricings, per 1k USD), read 2026-08-26.
CODEX_GPT_PRICING = ModelPricingSchema(
    input_per_1k=Decimal("0.001250"),
    output_per_1k=Decimal("0.010000"),
    cache_write_per_1k=Decimal("0"),
    cache_write_1h_per_1k=Decimal("0"),
    cache_read_per_1k=Decimal("0.000125"),
)


class _Cfg:
    """Minimal stand-in for ModelConfigSchema — calculate_cost only reads .pricing."""

    def __init__(self, pricing: ModelPricingSchema) -> None:
        self.pricing = pricing


# ── split_cached_input properties ────────────────────────────────────────────────


def test_split_basic():
    assert split_cached_input(100, 30) == (70, 30)


def test_split_no_cache_is_identity():
    assert split_cached_input(100, 0) == (100, 0)


def test_split_fully_cached_leaves_zero_billable_input():
    assert split_cached_input(100, 100) == (0, 100)


def test_split_clamps_impossible_cached_greater_than_input():
    """cached ⊆ prompt by definition; a provider violating it must not yield negative input.

    A negative input_tokens would flow into calculate_cost and Redis budget_deduct as a
    CREDIT, letting a malformed upstream payload refund real budget.
    """
    assert split_cached_input(10, 999) == (0, 10)


def test_split_clamps_negative_input():
    assert split_cached_input(-5, 3) == (0, 0)


def test_split_preserves_the_sum():
    """The invariant that lets callers keep the provider's own total_tokens."""
    for total, cached in ((0, 0), (1, 0), (1, 1), (12345, 6789), (100, 100), (100, 250)):
        non_cached, c = split_cached_input(total, cached)
        assert non_cached + c == max(total, 0)


# ── the money: cost regression with hand-computed values ─────────────────────────


def test_cached_tokens_are_not_billed_twice():
    """A 90%-cached prompt. Hand-computed, no reference to implementation internals.

    Provider reports: input_tokens=100_000 (of which 90_000 cached), output_tokens=1_000.

    correct   = 10_000/1k*0.00125 + 1_000/1k*0.01 + 90_000/1k*0.000125
              = 0.012500      + 0.010000     + 0.011250      = 0.033750
    pre-fix   = 100_000/1k*0.00125 + 0.010000 + 0.011250     = 0.146250   (4.333x)
    """
    usage = _extract_responses_usage(
        {
            "usage": {
                "input_tokens": 100_000,
                "input_tokens_details": {"cached_tokens": 90_000},
                "output_tokens": 1_000,
                "total_tokens": 101_000,
            }
        }
    )
    assert usage.input_tokens == 10_000
    assert usage.cache_read_input_tokens == 90_000

    cost = calculate_cost(usage, _Cfg(CODEX_GPT_PRICING))
    assert cost == Decimal("0.033750")

    # The exact pre-fix number, to pin the regression rather than merely assert "less".
    buggy = calculate_cost(
        TokenUsage(
            input_tokens=100_000, output_tokens=1_000, cache_read_input_tokens=90_000
        ),
        _Cfg(CODEX_GPT_PRICING),
    )
    assert buggy == Decimal("0.146250")
    assert round(buggy / cost, 3) == Decimal("4.333")


def test_cached_portion_costs_exactly_one_tenth_of_uncached():
    """The 11x claim, isolated: a cached token must cost cache_read, not input+cache_read."""
    cfg = _Cfg(CODEX_GPT_PRICING)
    uncached = calculate_cost(TokenUsage(input_tokens=1_000), cfg)
    cached = calculate_cost(
        _extract_responses_usage(
            {"usage": {"input_tokens": 1_000, "input_tokens_details": {"cached_tokens": 1_000}}}
        ),
        cfg,
    )
    assert uncached == Decimal("0.001250")
    assert cached == Decimal("0.000125")
    assert uncached == cached * 10
    # Pre-fix the same request cost 0.001250 + 0.000125 = 0.001375 → 11x the correct 0.000125.
    assert Decimal("0.001375") / cached == 11


def test_dev_fleet_overcharge_closed_form():
    """Reproduces the measured dev aggregate.

    Because the only mis-billed term is input on the cached tokens, the total over-charge
    collapses to sum(cache_read_tokens) * input_rate — independent of output and of how the
    requests were distributed. Dev: sum(cache_read) = 806_393 over 63 codex-gpt requests.
    """
    sum_cache_read = 806_393
    overcharge = (Decimal(sum_cache_read) / 1000) * CODEX_GPT_PRICING.input_per_1k
    assert overcharge.quantize(Decimal("0.000001")) == Decimal("1.007991")
    recorded, correct = Decimal("2.670309"), Decimal("1.662318")
    assert (recorded - correct).quantize(Decimal("0.000001")) == Decimal("1.007991")
    assert round(recorded / correct, 4) == Decimal("1.6064")


# ── dialect parity: streamed and non-streamed must agree ─────────────────────────


def test_streaming_and_nonstreaming_agree_on_the_same_usage_object():
    """Same prompt must cost the same whether streamed or not.

    Before the fix both paths were wrong identically; a one-sided fix would have made
    streaming and non-streaming bill differently, which is worse than a consistent error.
    """
    from app.services.streaming import responses_sse_stream  # noqa: F401  (import guard)

    raw = {
        "usage": {
            "input_tokens": 5_000,
            "input_tokens_details": {"cached_tokens": 4_096},
            "output_tokens": 700,
            "output_tokens_details": {"reasoning_tokens": 500},
            "total_tokens": 5_700,
        }
    }
    non_stream = _extract_responses_usage(raw)

    # streaming.py's parser is a closure; drive it through the public stream instead.
    import asyncio

    captured: dict = {}

    async def _run():
        async def on_usage(u, _ttft=None):
            captured["u"] = u

        async def _aiter():
            yield json.dumps({"type": "response.completed", "response": raw}).encode()

        class _Req:
            async def is_disconnected(self):
                return False

        async for _ in responses_sse_stream(_Req(), _aiter(), on_usage=on_usage):
            pass

    asyncio.run(_run())
    streamed = captured["u"]

    assert streamed.input_tokens == non_stream.input_tokens == 904
    assert streamed.cache_read_input_tokens == non_stream.cache_read_input_tokens == 4_096
    assert streamed.total_tokens == non_stream.total_tokens == 5_700
    cfg = _Cfg(CODEX_GPT_PRICING)
    assert calculate_cost(streamed, cfg) == calculate_cost(non_stream, cfg)


# ── negative control: the Anthropic path must NOT change ─────────────────────────


def test_anthropic_converse_path_is_untouched():
    """Bedrock/Converse reports the buckets disjointly already — subtracting would UNDER-bill.

    This is the negative control for the whole patch: 5_000 stays 5_000.
    """
    usage = _extract_bedrock_usage(
        {
            "usage": {
                "input_tokens": 5_000,
                "output_tokens": 700,
                "cache_read_input_tokens": 4_096,
                "cache_creation_input_tokens": 128,
            }
        }
    )
    assert usage.input_tokens == 5_000  # NOT 904
    assert usage.cache_read_input_tokens == 4_096
    assert usage.cache_creation_input_tokens == 128
    assert usage.total_tokens == 5_700  # input + output, cache excluded (unchanged)


# ── TPM: the fix repairs the documented definition for free ──────────────────────


def test_tpm_now_matches_its_documented_total_minus_cached_definition():
    """compute_tpm_incr's docstring promises ``total - cached_tokens``.

    On the Responses dialect it previously returned the full provider total (cached
    included), over-counting TPM by the cached amount and tightening rate limits. The parser
    fix makes the promise true without touching compute_tpm_incr.
    """
    provider_input, cached, output = 5_000, 4_096, 700
    usage = _extract_responses_usage(
        {
            "usage": {
                "input_tokens": provider_input,
                "input_tokens_details": {"cached_tokens": cached},
                "output_tokens": output,
            }
        }
    )
    provider_total = provider_input + output
    assert compute_tpm_incr(usage) == provider_total - cached == 1_604


# ── wire vs billing: the client-visible payload stays spec-legal ─────────────────


def test_websearch_wire_usage_is_cache_inclusive():
    """web_search_loop rewrites the client's usage object.

    It must emit the cache-INCLUSIVE prompt count (OpenAI Responses semantics, and what
    Codex CLI reads for context tracking). Emitting the billing bucket would produce
    ``cached_tokens > input_tokens`` — an impossible payload.
    """
    merged = TokenUsage(
        input_tokens=904, output_tokens=700, cache_read_input_tokens=4_096, reasoning_tokens=500
    )
    obj = _finalize_responses_obj({}, merged, 0, "response.completed", set())
    u = obj["usage"]
    assert u["input_tokens"] == 5_000  # 904 billing + 4_096 cached
    assert u["input_tokens_details"]["cached_tokens"] == 4_096
    # The impossible-payload guard: cached can never exceed the reported prompt count.
    assert u["input_tokens_details"]["cached_tokens"] <= u["input_tokens"]
    assert u["total_tokens"] == 5_700
    assert u["output_tokens_details"]["reasoning_tokens"] == 500
