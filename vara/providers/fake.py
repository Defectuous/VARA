import asyncio
from collections.abc import AsyncIterator, Sequence

from vara.providers.base import (
    Capabilities,
    Completed,
    Message,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
)

# One scripted model turn: plain text, or tool calls (optionally with some text first).
ScriptedTurn = str | list[ToolCall] | tuple[str, list[ToolCall]]


class FakeProvider:
    """Deterministic provider for tests: echoes the last user message, or fails on demand.

    With `script`, each call plays the next scripted turn instead (text, or tool calls), so tests can
    drive the agent loop step by step. When the script runs out, it echoes again.
    """

    def __init__(
        self,
        name: str = "fake",
        *,
        local: bool = True,
        reply: str | None = None,
        fail: bool = False,
        usage: tuple[int, int] | None = None,
        delay: float = 0.0,
        script: Sequence[ScriptedTurn] = (),
        tools: bool = True,
    ) -> None:
        self.name = name
        self.default_model = f"{name}-model"
        self.capabilities = Capabilities(local=local, context_length=8192, tools=tools)
        self._reply = reply
        self._fail = fail
        self._usage = usage
        self._delay = delay  # seconds before the first text, like a model loading or reasoning
        self._script = list(script)
        self.calls: list[list[Message]] = []
        self.thinks: list[bool | None] = []
        self.tools_offered: list[list[str]] = []
        self.systems: list[str | None] = []

    def request_body(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
        think: bool | None = None,
        tools: Sequence[ToolSpec] | None = None,
    ) -> dict:
        body = {
            "model": model or self.default_model,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if tools:
            body["tools"] = [t.name for t in tools]
        return body

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
        think: bool | None = None,
        tools: Sequence[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(list(messages))
        self.thinks.append(think)
        self.tools_offered.append([t.name for t in tools or ()])
        self.systems.append(system)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise ProviderError(f"{self.name} is unavailable")
        text: str | None
        calls: list[ToolCall] = []
        if self._script:
            turn = self._script.pop(0)
            if isinstance(turn, str):
                text = turn
            elif isinstance(turn, tuple):
                text, calls = turn
            else:
                text, calls = "", turn
        else:
            text = self._reply if self._reply is not None else f"echo: {messages[-1].content}"
        if text:
            for word in text.split(" "):
                yield TextDelta(word + " ")
        for call in calls:
            yield call
        yield Completed(
            model=model or self.default_model,
            stop_reason="tool_use" if calls else "end_turn",
            input_tokens=self._usage[0] if self._usage else None,
            output_tokens=self._usage[1] if self._usage else None,
        )
