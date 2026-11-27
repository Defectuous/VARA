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
