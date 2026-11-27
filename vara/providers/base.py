from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class Capabilities:
    local: bool
    context_length: int
    vision: bool = False
    tools: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may ask to use: its name, what it does, and a JSON schema for its arguments.
    Asking is all the model can do; the agent loop (vara/agent.py) decides whether it runs."""

    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolCall:
    """The model asking to use a tool. Also a stream event, yielded before `Completed`."""

    id: str
    name: str
    arguments: dict
    # Provider data that must go back with the call on the next request (for example a model's
    # reasoning, or Gemini's per-call fields). Only the provider that made the call reads it.
    extra: dict | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Message:
    """One turn. An assistant turn may carry `tool_calls`; a "tool" turn is the result of one call,
    linked by `tool_call_id`."""

    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ToolCall, ...] = field(default=())
    tool_call_id: str | None = None
    name: str | None = None  # the tool's name, on "tool" turns


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Completed:
    model: str
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


StreamEvent = TextDelta | ToolCall | Completed


class ProviderError(Exception):
    """A provider could not answer (unreachable, rejected the request, bad credentials)."""


class ModelProvider(Protocol):
    """One model backend. Every provider streams text (and, when given `tools`, tool calls) and ends
    with exactly one `Completed`.

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
        tools: Sequence[ToolSpec] | None = None,
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
        tools: Sequence[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]: ...
