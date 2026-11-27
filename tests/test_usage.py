from datetime import UTC, datetime

import pytest

from vara.usage import estimate_cost


def test_local_models_cost_nothing() -> None:
    assert estimate_cost("qwen3:8b", 1000, 1000, local=True) == 0.0


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5-5", 4.00 + 20.00),
        ("gpt-6.2-sol", 2.00 + 10.00),
        ("gpt-5-mini", 0.25 + 2.00),
        ("grok-4.7", 2.00 + 6.00),
        ("grok-4.3", 1.25 + 2.50),
    ],
)
def test_cloud_cost_per_million_tokens(model: str, expected: float) -> None:
    # 100K tokens each way: under every long-context threshold.
    assert estimate_cost(model, 100_000, 100_000, local=False) == pytest.approx(expected / 10)


def test_dated_snapshots_use_their_model_price() -> None:
    assert estimate_cost("gpt-5.4-2026-03-05", 100_000, 0, local=False) == pytest.approx(0.25)
    # "gpt-5-mini" is its own model, not a snapshot of gpt-5.
    assert estimate_cost("gpt-5-mini", 1_000_000, 0, local=False) == pytest.approx(0.25)


def test_long_prompts_bill_the_whole_request_at_the_long_rate() -> None:
    # OpenAI: more than 272K input tokens.
    assert estimate_cost("gpt-6.2-sol", 272_000, 0, local=False) == pytest.approx(0.544)
    assert estimate_cost("gpt-6.2-sol", 272_001, 1_000_000, local=False) == pytest.approx(
        272_001 * 4.00 / 1e6 + 15.00
    )
    # xAI: the prompt reaches 200K tokens.
    assert estimate_cost("grok-4.7", 199_999, 0, local=False) == pytest.approx(199_999 * 2.00 / 1e6)
    assert estimate_cost("grok-4.7", 200_000, 0, local=False) == pytest.approx(0.80)


def test_unknown_models_and_missing_counts_have_no_cost() -> None:
    assert estimate_cost("some-new-model", 100, 100, local=False) is None
    assert estimate_cost("grok-4.7", None, 100, local=False) is None


def test_gemini_prices_step_up_on_their_dates() -> None:
    before, after = datetime(2026, 11, 30, 12, tzinfo=UTC), datetime(2026, 12, 1, 12, tzinfo=UTC)

    assert estimate_cost("gemini-4-carbon", 100_000, 0, local=False, at=before) == pytest.approx(
        0.20
    )
    assert estimate_cost("gemini-4-carbon", 100_000, 0, local=False, at=after) == pytest.approx(
        0.40
    )
    later = datetime(2027, 1, 1, tzinfo=UTC)
    assert estimate_cost("gemini-3.8-flash", 0, 100_000, local=False, at=later) == pytest.approx(
        0.75
    )
    # Gemini sometimes names models "models/...".
    assert estimate_cost("models/gemini-4-hydrogen", 100_000, 0, local=False) == pytest.approx(0.15)


def test_deepseek_is_half_price_off_peak() -> None:
    peak = datetime(2026, 11, 25, 7, tzinfo=UTC)  # a Wednesday, 07:00 UTC
    evening = datetime(2026, 11, 25, 15, tzinfo=UTC)
    weekend = datetime(2026, 11, 28, 7, tzinfo=UTC)  # a Saturday

    def cost(at: datetime) -> float:
        return estimate_cost("deepseek-v4-pro", 1_000_000, 1_000_000, local=False, at=at)

    assert cost(peak) == pytest.approx(1.32 + 3.96)
    assert cost(evening) == pytest.approx((1.32 + 3.96) / 2)
    assert cost(weekend) == pytest.approx((1.32 + 3.96) / 2)


def test_models_through_perplexity_use_their_makers_price() -> None:
    assert estimate_cost("perplexity/glm-5.3", 100_000, 0, local=False) == pytest.approx(0.14)
    assert estimate_cost("xai/grok-4.7", 100_000, 0, local=False) == pytest.approx(0.20)


def test_kimi_has_no_price_yet() -> None:
    assert estimate_cost("kimi-k3.1-pro", 100, 100, local=False) is None
