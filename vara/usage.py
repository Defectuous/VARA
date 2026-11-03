# USD per million tokens (input, output) at Anthropic's first-party list prices.
# Haiku 5.5's rate applies to prompts up to 100K tokens.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def estimate_cost(
    model: str, input_tokens: int | None, output_tokens: int | None, *, local: bool
) -> float | None:
    """Cost in USD, 0.0 for local models, or None when the price or token counts are unknown."""
    if local:
        return 0.0
    price = _PRICES.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
