from dataclasses import dataclass
from datetime import UTC, date, datetime


@dataclass(frozen=True)
class Price:
    """USD per million tokens. From `long_from` input tokens on, the whole request is billed at the
    long-context rates instead. `off_peak_half` is DeepSeek's: half price outside its peak hours."""

    input: float
    output: float
    long_from: int | None = None
    long_input: float | None = None
    long_output: float | None = None
    off_peak_half: bool = False


def _deepseek_peak(at: datetime) -> bool:
    """01:00-04:00 and 06:00-10:00 UTC on weekdays. (Chinese public holidays are off-peak too; they
    aren't tracked, so a holiday request is estimated at the peak rate.)"""
    at = at.astimezone(UTC)
    return at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)


def _tiered(input: float, output: float, long_input: float, long_output: float, long_from: int):
    return Price(input, output, long_from, long_input, long_output)


# Standard-tier list prices, checked 2026-11-27 against each provider's own pricing page
# (anthropic.com, developers.openai.com/api/docs/pricing, docs.x.ai/docs/models,
# aistudio.google.com/docs/pricing, api-docs.deepseek.com/quick_start/pricing,
# docs.perplexity.ai/docs/orchestrator/models). Kimi's pricing page didn't show its table, so Kimi models
# have no price yet. A model whose price changes on a known date has a list of (from date, price).
# Cached input is billed lower by every provider, but token counts here don't separate it out, so a
# request that hit the cache is estimated a little high. Haiku 5.5's rate applies up to 100K tokens.
_OPENAI_LONG = 272_001  # OpenAI: long context is more than 272K input tokens
_XAI_LONG = 200_000  # xAI: the prompt reaches 200K tokens
_GEMINI_LONG = 200_001  # Gemini 3.1 Pro: more than 200K prompt tokens

_GEMINI_4_FULL = (
    (date.min, Price(2.00, 10.00)),
    (date(2026, 12, 1), Price(4.00, 20.00)),
)
_GEMINI_FLASH = (
    (date.min, Price(0.75, 3.75)),
    (date(2027, 1, 1), Price(1.50, 7.50)),
)
_DEEPSEEK_FLASH = Price(0.30, 1.20, off_peak_half=True)
_DEEPSEEK_PRO = Price(1.32, 3.96, off_peak_half=True)

_PRICES: dict[str, Price | tuple[tuple[date, Price], ...]] = {
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
    # Google Gemini (output includes thinking tokens)
    "gemini-4-carbon": _GEMINI_4_FULL,
    "gemini-4-argon": _GEMINI_4_FULL,
    "gemini-4-neon": Price(3.00, 15.00),
    "gemini-4-hydrogen": Price(1.50, 7.50),
    "gemini-3.8-flash": _GEMINI_FLASH,
    "gemini-3.6-flash": _GEMINI_FLASH,
    "gemini-3.5-flash-lite": Price(0.30, 2.50),
    "gemini-3.1-pro-preview": _tiered(2.00, 12.00, 4.00, 18.00, _GEMINI_LONG),
    # DeepSeek (peak rates; off-peak is half). The older names are served by the current models.
    "deepseek-flash": _DEEPSEEK_FLASH,
    "deepseek-v4.1-flash-1017": _DEEPSEEK_FLASH,
    "deepseek-v4-flash": _DEEPSEEK_FLASH,
    "deepseek-v4-pro": _DEEPSEEK_PRO,
    "deepseek-v4-pro-0813": _DEEPSEEK_PRO,
    # Perplexity-hosted models. Other companies' models through Perplexity ("openai/gpt-6.2-sol") are
    # billed at that company's price, so they use its entry above.
    "perplexity/glm-5.3": Price(1.40, 4.40),
    "perplexity/glm-5.3-flash": Price(0.15, 0.50),
    "perplexity/deepseek-v4-pro-0813": Price(1.32, 3.96),
    "perplexity/kimi-k3": Price(3.00, 15.00),
}


def _lookup(model: str) -> Price | tuple[tuple[date, Price], ...] | None:
    if model in _PRICES:
        return _PRICES[model]
    # A dated snapshot: "gpt-5.4-2026-03-05" is gpt-5.4 (but "gpt-5-mini" is its own model).
    matches = [
        name
        for name in _PRICES
        if model.startswith(name + "-") and model[len(name) + 1 : len(name) + 2].isdigit()
    ]
    return _PRICES[max(matches, key=len)] if matches else None


def _price_for(model: str, on: date) -> Price | None:
    """The price for a model id on a day. Also matches dated snapshots, Gemini's "models/..." form,
    and another company's model through Perplexity ("xai/grok-4.7")."""
    model = model.lower().removeprefix("models/")
    found = _lookup(model)
    if found is None and "/" in model:
        found = _lookup(model.rsplit("/", 1)[1])
    if isinstance(found, tuple):
        found = next(price for start, price in reversed(found) if start <= on)
    return found


def estimate_cost(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    *,
    local: bool,
    at: datetime | None = None,
) -> float | None:
    """Cost in USD, 0.0 for local models, or None when the price or token counts are unknown.
    `at` is when the request was made (now if not given)."""
    if local:
        return 0.0
    at = at or datetime.now(UTC)
    price = _price_for(model, at.astimezone(UTC).date())
    if price is None or input_tokens is None or output_tokens is None:
        return None
    per_input, per_output = price.input, price.output
    if price.long_from is not None and input_tokens >= price.long_from:
        per_input, per_output = price.long_input, price.long_output
    cost = (input_tokens * per_input + output_tokens * per_output) / 1_000_000
    if price.off_peak_half and not _deepseek_peak(at):
        cost /= 2
    return cost
