"""Tests for ClaudeClient cost accounting and TokenUsage."""

from __future__ import annotations

from dataclasses import dataclass

from figma_audit.utils.claude_client import (
    DEFAULT_MODEL,
    DEFAULT_PRICING,
    PRICING,
    TokenUsage,
)


@dataclass
class _MockUsage:
    input_tokens: int = 1000
    output_tokens: int = 500
    cache_read_input_tokens: int = 0


@dataclass
class _MockResponse:
    usage: _MockUsage


def _resp(input_t: int = 1000, output_t: int = 500, cache_t: int = 0) -> _MockResponse:
    return _MockResponse(usage=_MockUsage(input_t, output_t, cache_t))


class TestTokenUsage:
    def test_initial_state(self) -> None:
        u = TokenUsage()
        assert u.input_tokens == 0
        assert u.output_tokens == 0
        assert u.calls == 0
        assert u.cache_read_tokens == 0
        assert u.total_tokens == 0
        assert u._by_phase == {}

    def test_add_single_response(self) -> None:
        u = TokenUsage()
        u.add(_resp(1000, 500))
        assert u.input_tokens == 1000
        assert u.output_tokens == 500
        assert u.total_tokens == 1500
        assert u.calls == 1

    def test_add_accumulates(self) -> None:
        u = TokenUsage()
        u.add(_resp(1000, 500))
        u.add(_resp(2000, 800))
        u.add(_resp(500, 200))
        assert u.input_tokens == 3500
        assert u.output_tokens == 1500
        assert u.calls == 3

    def test_cache_tokens_tracked(self) -> None:
        u = TokenUsage()
        u.add(_resp(1000, 500, cache_t=300))
        u.add(_resp(2000, 800, cache_t=1500))
        assert u.cache_read_tokens == 1800

    def test_phase_breakdown(self) -> None:
        u = TokenUsage()
        u.add(_resp(1000, 500), phase="analyze")
        u.add(_resp(2000, 800), phase="compare")
        u.add(_resp(500, 200), phase="compare")

        assert u._by_phase["analyze"]["input"] == 1000
        assert u._by_phase["analyze"]["output"] == 500
        assert u._by_phase["analyze"]["calls"] == 1

        assert u._by_phase["compare"]["input"] == 2500
        assert u._by_phase["compare"]["output"] == 1000
        assert u._by_phase["compare"]["calls"] == 2

    def test_phase_optional(self) -> None:
        """Calling add() without phase still updates totals but not _by_phase."""
        u = TokenUsage()
        u.add(_resp(1000, 500))
        assert u.input_tokens == 1000
        assert u._by_phase == {}

    def test_cost_default_model(self) -> None:
        u = TokenUsage()
        u.add(_resp(1_000_000, 500_000))
        # Sonnet 4.5: $3/M input + $15/M output
        # 1M * $3 + 0.5M * $15 = $3 + $7.5 = $10.5
        assert u.cost(DEFAULT_MODEL) == 10.5

    def test_cost_opus(self) -> None:
        u = TokenUsage()
        u.add(_resp(1_000_000, 500_000))
        # Opus: $15/M input + $75/M output
        # 1M * $15 + 0.5M * $75 = $15 + $37.5 = $52.5
        assert u.cost("claude-opus-4-6") == 52.5

    def test_cost_unknown_model_uses_default(self) -> None:
        u = TokenUsage()
        u.add(_resp(1_000_000, 0))
        # Unknown model falls back to DEFAULT_PRICING ($3/M input)
        assert u.cost("nonexistent-model") == 3.0

    def test_cost_zero_when_empty(self) -> None:
        u = TokenUsage()
        assert u.cost() == 0.0

    def test_cost_proportional_to_tokens(self) -> None:
        u = TokenUsage()
        u.add(_resp(100, 50))
        # 100 in * $3/M + 50 out * $15/M = 0.0003 + 0.00075 = 0.00105
        assert u.cost(DEFAULT_MODEL) == pytest_approx(0.00105)

    def test_summary_format(self) -> None:
        u = TokenUsage()
        u.add(_resp(1000, 500))
        u.add(_resp(2000, 1000))
        s = u.summary(DEFAULT_MODEL)
        assert "2 appels API" in s
        assert "3,000 input" in s
        assert "1,500 output" in s
        assert "$" in s

    def test_summary_empty(self) -> None:
        u = TokenUsage()
        s = u.summary()
        assert "0 appels" in s

    def test_phase_breakdown_format(self) -> None:
        u = TokenUsage()
        u.add(_resp(1000, 500), phase="analyze")
        u.add(_resp(2000, 800), phase="compare")
        breakdown = u.phase_breakdown(DEFAULT_MODEL)
        assert "analyze" in breakdown
        assert "compare" in breakdown
        # Each phase line includes its cost
        assert "$" in breakdown


class TestPricingTable:
    def test_default_pricing_keys(self) -> None:
        assert "input" in DEFAULT_PRICING
        assert "output" in DEFAULT_PRICING

    def test_default_model_in_pricing(self) -> None:
        assert DEFAULT_MODEL in PRICING

    def test_opus_more_expensive_than_sonnet(self) -> None:
        sonnet = PRICING["claude-sonnet-4-5-20250929"]
        opus = PRICING["claude-opus-4-6"]
        assert opus["input"] > sonnet["input"]
        assert opus["output"] > sonnet["output"]


def pytest_approx(expected: float, rel: float = 1e-6) -> object:
    """Helper for floating-point comparisons in assertions (avoids importing pytest.approx)."""
    import pytest as _pytest
    return _pytest.approx(expected, rel=rel)
