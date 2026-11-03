from collections.abc import AsyncIterator, Sequence

from vara.providers.base import (
    Capabilities,
    Completed,
    Message,
    ProviderError,
    StreamEvent,
    TextDelta,
)


class FakeProvider:
    """Deterministic provider for tests: echoes the last user message, or fails on demand."""

    def __init__(
        self,
        name: str = "fake",
        *,
        local: bool = True,
        reply: str | None = None,
        fail: bool = False,
        usage: tuple[int, int] | None = None,
    ) -> None:
        self.name = name
        self.default_model = f"{name}-model"
        self.capabilities = Capabilities(local=local, context_length=8192)
        self._reply = reply
        self._fail = fail
        self._usage = usage
        self.calls: list[list[Message]] = []

    def request_body(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
    ) -> dict:
        return {
            "model": model or self.default_model,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(list(messages))
        if self._fail:
            raise ProviderError(f"{self.name} is unavailable")
        reply = self._reply if self._reply is not None else f"echo: {messages[-1].content}"
        for word in reply.split(" "):
            yield TextDelta(word + " ")
        yield Completed(
            model=model or self.default_model,
            stop_reason="end_turn",
            input_tokens=self._usage[0] if self._usage else None,
            output_tokens=self._usage[1] if self._usage else None,
        )
