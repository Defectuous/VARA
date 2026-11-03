from dataclasses import asdict, dataclass

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


@dataclass
class UsageTotals:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_requests: int = 0


class UsageTracker:
    """Running per-provider, per-model totals since the server started (not persisted yet)."""

    def __init__(self) -> None:
        self._totals: dict[tuple[str, str], UsageTotals] = {}

    def record(
        self,
        provider: str,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: float | None,
    ) -> None:
        totals = self._totals.setdefault((provider, model), UsageTotals())
        totals.requests += 1
        totals.input_tokens += input_tokens or 0
        totals.output_tokens += output_tokens or 0
        if cost_usd is None:
            totals.unpriced_requests += 1
        else:
            totals.cost_usd += cost_usd

    def summary(self) -> list[dict]:
        return [
            {"provider": provider, "model": model, **asdict(totals)}
            for (provider, model), totals in self._totals.items()
        ]
