from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from vara.router import PrivacyMode

DEFAULT_SYSTEM_PROMPT = (
    "You are V.A.R.A. (Visual and Audio Reality Assistant), a personal assistant running "
    "locally on the user's own PC. Be direct, accurate, and concise. If you don't know "
    "something or can't do it, say so plainly instead of guessing. Answer conversationally, the way a "
    "person would: for a simple question, just give the answer in a sentence or two. Use Markdown "
    "(lists, bold, code blocks) only when it genuinely helps. Never use LaTeX (no $...$, $$...$$, or "
    '\\boxed), and never add a "Final Answer" heading or summary section. Today is {date}, and the '
    "local time is {time} ({timezone})."
)

# V.A.R.A.'s character, added to the system prompt (set VARA_PERSONALITY to change it, or to "" for none).
# The snark is gated: the model is told when it's welcome and when to drop it.
DEFAULT_PERSONALITY = (
    "Personality: you have a dry, quick, snarky wit, like a sharp friend who happens to know "
    "everything. When the moment invites it (casual chat, banter, obvious or silly questions, the user "
    "teasing you, small everyday mishaps), feel free to add a playful jab, a deadpan aside, or a "
    "sarcastic quip, keeping it to a line or so and always affectionate, never mean. Snark is "
    "seasoning, not the meal: most replies should have little or none, and the answer itself always "
    "comes first and stays correct and complete. Drop the snark entirely when the user is stressed, "
    "upset, grieving, or in a hurry, and for health, safety, money, legal, or other serious matters; "
    "then be warm and straightforward."
)


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Runtime settings, read from `VARA_*` environment variables or a local `.env` file.

    API keys do not belong here; they live in the OS credential store.
    """

    model_config = SettingsConfigDict(env_prefix="VARA_", env_file=".env", extra="ignore")

    environment: Environment = Environment.DEVELOPMENT
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    data_dir: Path = Path("data")
    retention_on_startup: bool = True  # apply retention rules each time the server starts

    # Used when a request sends no `system` of its own. `{date}`, `{time}`, and `{timezone}` are filled
    # in from this PC's clock on every request (the model has no clock of its own).
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Added after `system_prompt` (not to a request that sends its own `system`).
    personality: str = DEFAULT_PERSONALITY
    # When a streamed reply hasn't started after this many seconds (a model loading, long reasoning, a web
    # search), V.A.R.A. says it will take a minute, then "still working on it" every `hold_repeat_seconds`
    # until the reply starts. 0 turns the first or the repeated notices off.
    hold_notice_seconds: float = 6.0
    hold_repeat_seconds: float = 120.0

    privacy_mode: PrivacyMode = PrivacyMode.LOCAL_ONLY
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.1:8b"
    # More Ollama models to offer in the chat page's Model menu, comma-separated (e.g. "qwen3:14b").
    # `ollama_model` stays the default; only one model fits in GPU memory at a time, so switching
    # between them reloads the model (about a minute).
    ollama_models: str = ""
    # How long Ollama keeps the model in GPU memory after a request ("-1" keeps it loaded).
    # Loading it again takes over a minute on the RTX 5060.
    ollama_keep_alive: str = "2h"
    # "auto" pulls every Ollama model when V.A.R.A. starts and once a day after that; "off" never does.
    model_updates: Literal["auto", "off"] = "auto"
    # Reasoning before answering (models that support it, e.g. qwen3): "auto" decides per message,
    # skipping it for small talk and quick facts and using it for hard questions; "on"/"off" force it.
    reasoning: Literal["auto", "on", "off"] = "auto"
    # Speech-to-text. "auto" uses the NVIDIA GPU when it can and the CPU otherwise: "small" transcribes a
    # short request in about 0.16 s on the RTX 5060 against about 2 s on the 5800X. (The chat model runs on
    # defectserver, so the PC's GPU is free.) "auto" compute type: float16 on the GPU, int8 on the CPU.
    whisper_model: str = "small"
    whisper_device: Literal["auto", "cuda", "cpu"] = "auto"
    whisper_compute_type: str = "auto"
    # Load and run the speech models in the background when the server starts, so the first spoken
    # request doesn't wait for them.
    voice_warm_up: bool = True

    # Web search through V.A.R.A.'s SearXNG module (data/modules/searxng): "auto" searches when a question
    # needs current information (only the search words leave this PC); "off" never searches.
    web_search: Literal["auto", "off"] = "auto"
    searxng_port: int = 8890

    # Text-to-speech: Kokoro-82M voices, as in SiteRecorder/SiteReader. V.A.R.A.'s voice is bf_lily (British
    # female, the owner's choice). Others: bf_alice, bf_emma, bf_isabella; US female af_heart, af_bella, ...
    tts_voice: str = "bf_lily"
    tts_speed: float = 1.0

    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"

    @property
    def ollama_model_list(self) -> list[str]:
        """The default Ollama model first, then the extra ones, without duplicates."""
        extras = [m.strip() for m in self.ollama_models.split(",") if m.strip()]
        return list(dict.fromkeys([self.ollama_model, *extras]))

    @property
    def database_path(self) -> Path:
        return self.data_dir / "vara.db"

    @property
    def searxng_dir(self) -> Path:
        return self.data_dir / "modules" / "searxng"

    @property
    def kokoro_model_path(self) -> Path:
        return self.data_dir / "models" / "kokoro" / "kokoro-v1.0.int8.onnx"

    @property
    def kokoro_voices_path(self) -> Path:
        return self.data_dir / "models" / "kokoro" / "voices-v1.0.bin"

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    return Settings()
