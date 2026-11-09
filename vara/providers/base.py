from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class Capabilities:
    local: bool
    context_length: int
    vision: bool = False
    tools: bool = False


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Completed:
    model: str
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


StreamEvent = TextDelta | Completed


class ProviderError(Exception):
    """A provider could not answer (unreachable, rejected the request, bad credentials)."""


class ModelProvider(Protocol):
    """One model backend. Every provider streams text and ends with exactly one `Completed`.

    Providers are only called through `ModelRouter`, which enforces the privacy mode.
    `think` asks the model to reason before answering (True) or to answer straight away (False);
    None leaves the model's own default. Providers that can't switch it ignore it.
    """

    name: str
    default_model: str
    capabilities: Capabilities

    def request_body(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
        think: bool | None = None,
    ) -> dict:
        """The exact payload `stream` would send, for inspection. Never contains credentials."""
        ...

    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
        think: bool | None = None,
    ) -> AsyncIterator[StreamEvent]: ...
