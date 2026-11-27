"""The current models each cloud provider offers, and what each is good at, for the chat page's Model menu.

Checked 2026-11-27 against each provider's own model pages (Anthropic's model list, OpenAI
developers.openai.com/api/docs/models, Google aistudio.google.com/docs/models, xAI docs.x.ai/docs/models,
DeepSeek api-docs.deepseek.com, Kimi platform.kimi.ai/docs, Perplexity docs.perplexity.ai). The first model
of each provider is the strongest; the provider's default (VARA_<PROVIDER>_MODEL) is always offered too.
VARA_MODEL_LABELS overrides any label here, and labels local Ollama models.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CloudProvider:
    name: str  # also the credential name
    label: str
    models: tuple[tuple[str, str], ...]  # (model id, "Name · what it's good at")


CLOUD = (
    CloudProvider(
        "anthropic",
        "Anthropic Claude",
        (
            (
                "claude-fable-5-2",
                "Claude Fable 5.2 · most capable: hardest reasoning, long agent work",
            ),
            ("claude-opus-5-5", "Claude Opus 5.5 · strong all-round: complex coding, agents"),
            ("claude-sonnet-5-5", "Claude Sonnet 5.5 · fast: everyday coding and agents"),
            ("claude-haiku-5-5", "Claude Haiku 5.5 · fastest, cheapest: simple tasks"),
        ),
    ),
    CloudProvider(
        "openai",
        "OpenAI",
        (
            ("gpt-6.2-astra", "GPT-6.2 Astra · most capable: hardest projects"),
            ("gpt-6.2-sol", "GPT-6.2 Sol · everyday work: coding, agents"),
            ("gpt-6.1-terra", "GPT-6.1 Terra · fast: visual reasoning, natural writing"),
            ("gpt-6-luna", "GPT-6 Luna · cheapest: focused, high-volume tasks"),
        ),
    ),
    CloudProvider(
        "xai",
        "xAI Grok",
        (
            ("grok-4.7", "Grok 4.7 · most capable Grok: chat and code"),
            ("grok-4.3", "Grok 4.3 · cheaper, 1M context"),
        ),
    ),
    CloudProvider(
        "gemini",
        "Google Gemini",
        (
            ("gemini-4-carbon", "Gemini 4 Carbon · strongest reasoning: complex engineering"),
            ("gemini-4-neon", "Gemini 4 Neon · frontier, fast task completion"),
            ("gemini-4-argon", "Gemini 4 Argon · efficient reasoning: faster, fewer tokens"),
            ("gemini-4-hydrogen", "Gemini 4 Hydrogen · fast: everyday coding and agents"),
            ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite · fastest, cheapest"),
        ),
    ),
    CloudProvider(
        "deepseek",
        "DeepSeek",
        (
            ("deepseek-v4-pro", "DeepSeek V4 Pro · larger model, reasoning, 1M context"),
            ("deepseek-flash", "DeepSeek Flash · fast, cheap, reads images"),
        ),
    ),
    CloudProvider(
        "kimi",
        "Kimi (Moonshot)",
        (
            ("kimi-k3.1-pro", "Kimi K3.1 Pro · flagship, always reasons"),
            ("kimi-k3.1-pro-highspeed", "Kimi K3.1 Pro Highspeed · same model, 2-3× faster"),
        ),
    ),
    CloudProvider(
        "perplexity",
        "Perplexity",
        (
            ("perplexity/glm-5.3", "GLM 5.3 via Perplexity · general"),
            ("perplexity/glm-5.3-flash", "GLM 5.3 Flash via Perplexity · fast, cheap"),
        ),
    ),
)

BY_NAME = {p.name: p for p in CLOUD}
PROVIDER_LABELS = {"ollama": "Local (Ollama)", **{p.name: p.label for p in CLOUD}}


def models_for(provider: str, default: str) -> list[str]:
    """The models to offer for a cloud provider: its default first, then the catalog's."""
    known = BY_NAME.get(provider)
    return list(dict.fromkeys([default, *(m for m, _ in known.models)] if known else [default]))


def labels(overrides: dict[str, str]) -> dict[str, str]:
    """Every model's label: the catalog's, with VARA_MODEL_LABELS on top."""
    found = {model: label for p in CLOUD for model, label in p.models}
    return {**found, **overrides}
