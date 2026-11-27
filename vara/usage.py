from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    """USD per million tokens. From `long_from` input tokens on, the whole request is billed at the
    long-context rates instead."""

    input: float
    output: float
    long_from: int | None = None
    long_input: float | None = None
    long_output: float | None = None


def _tiered(input: float, output: float, long_input: float, long_output: float, long_from: int):
    return Price(input, output, long_from, long_input, long_output)


# Standard-tier list prices, checked 2026-11-27 against each provider's own pricing page
# (anthropic.com, developers.openai.com/api/docs/pricing, docs.x.ai/docs/models).
# Cached input is billed lower by every provider, but token counts here don't separate it out, so a
# request that hit the cache is estimated a little high. Haiku 5.5's rate applies up to 100K tokens.
_OPENAI_LONG = 272_001  # OpenAI: long context is more than 272K input tokens
_XAI_LONG = 200_000  # xAI: the prompt reaches 200K tokens

_PRICES: dict[str, Price] = {
    # Anthropic
    "claude-opus-5-5": Price(4.00, 20.00),
    "claude-sonnet-5-5": Price(2.00, 10.00),
    "claude-haiku-5-5": Price(0.10, 0.50),
    # OpenAI
    "gpt-6.2-astra": _tiered(8.00, 40.00, 16.00, 60.00, _OPENAI_LONG),
    "gpt-6.2-sol": _tiered(2.00, 10.00, 4.00, 15.00, _OPENAI_LONG),
    "gpt-6-astra": _tiered(10.00, 50.00, 20.00, 75.00, _OPENAI_LONG),
    "gpt-6.1-sol": _tiered(2.00, 10.00, 4.00, 15.00, _OPENAI_LONG),
    "gpt-6.1-terra": _tiered(1.00, 5.00, 2.00, 7.50, _OPENAI_LONG),
    "gpt-6-sol": _tiered(2.00, 10.00, 4.00, 15.00, _OPENAI_LONG),
    "gpt-6-luna": _tiered(0.10, 0.50, 0.20, 0.75, _OPENAI_LONG),
    "gpt-5.6-sol": _tiered(4.00, 20.00, 8.00, 30.00, _OPENAI_LONG),
    "gpt-5.6-terra": _tiered(2.00, 12.00, 4.00, 18.00, _OPENAI_LONG),
    "gpt-5.6-luna": _tiered(0.20, 1.20, 0.40, 1.80, _OPENAI_LONG),
    "gpt-5.5": _tiered(5.00, 30.00, 10.00, 45.00, _OPENAI_LONG),
    "gpt-5.5-pro": _tiered(30.00, 180.00, 60.00, 270.00, _OPENAI_LONG),
    "gpt-5.4": _tiered(2.50, 15.00, 5.00, 22.50, _OPENAI_LONG),
    "gpt-5.4-pro": _tiered(30.00, 180.00, 60.00, 270.00, _OPENAI_LONG),
    "gpt-5.4-mini": Price(0.75, 4.50),
    "gpt-5.4-nano": Price(0.20, 1.25),
    "gpt-5.2": Price(1.75, 14.00),
    "gpt-5.2-pro": Price(21.00, 168.00),
    "gpt-5.1": Price(1.25, 10.00),
    "gpt-5": Price(1.25, 10.00),
    "gpt-5-mini": Price(0.25, 2.00),
    "gpt-5-nano": Price(0.05, 0.40),
    "gpt-5-pro": Price(15.00, 120.00),
    "gpt-4.1": Price(2.00, 8.00),
    "gpt-4.1-mini": Price(0.40, 1.60),
    "gpt-4o": Price(2.50, 10.00),
    "gpt-4o-mini": Price(0.15, 0.60),
    "o3": Price(2.00, 8.00),
    "o3-pro": Price(20.00, 80.00),
    # xAI (Grok)
    "grok-4.7": _tiered(2.00, 6.00, 4.00, 12.00, _XAI_LONG),
    "grok-4.6": _tiered(2.00, 6.00, 4.00, 12.00, _XAI_LONG),
    "grok-4.5": _tiered(2.00, 6.00, 4.00, 12.00, _XAI_LONG),
    "grok-4.3": _tiered(1.25, 2.50, 2.50, 5.00, _XAI_LONG),
    "grok-4.20-0309-reasoning": _tiered(1.25, 2.50, 2.50, 5.00, _XAI_LONG),
    "grok-4.20-0309-non-reasoning": _tiered(1.25, 2.50, 2.50, 5.00, _XAI_LONG),
    "grok-4.20-multi-agent-0309": _tiered(1.25, 2.50, 2.50, 5.00, _XAI_LONG),
}


def _price_for(model: str) -> Price | None:
    """The price for a model id, also matching dated snapshots ("gpt-5.4-2026-03-05" is gpt-5.4)."""
    if model in _PRICES:
        return _PRICES[model]
    matches = [
        name
        for name in _PRICES
        if model.startswith(name + "-") and model[len(name) + 1 : len(name) + 2].isdigit()
    ]
    return _PRICES[max(matches, key=len)] if matches else None


def estimate_cost(
    model: str, input_tokens: int | None, output_tokens: int | None, *, local: bool
) -> float | None:
    """Cost in USD, 0.0 for local models, or None when the price or token counts are unknown."""
    if local:
        return 0.0
    price = _price_for(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    per_input, per_output = price.input, price.output
    if price.long_from is not None and input_tokens >= price.long_from:
        per_input, per_output = price.long_input, price.long_output
    return (input_tokens * per_input + output_tokens * per_output) / 1_000_000
