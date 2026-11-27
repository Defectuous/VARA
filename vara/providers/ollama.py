import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence

import httpx

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


def _chat_message(m: Message) -> dict:
    if m.role == "tool":
        return {"role": "tool", "content": m.content, "tool_name": m.name}
    entry: dict = {"role": m.role, "content": m.content}
    if m.tool_calls:
        entry["tool_calls"] = [
            {"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls
        ]
    return entry


class OllamaProvider:
    name = "ollama"
    # Tool calling depends on the model (qwen3 and gemma4 have it); a model without it answers in text.
    capabilities = Capabilities(local=True, context_length=8192, tools=True)

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        default_model: str = "llama3.1:8b",
        keep_alive: str | None = None,
        models: Sequence[str] = (),
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.default_model = default_model
        # Models offered for a manual choice: the default first, then the configured ones, then any other
        # model installed on the Ollama server (see refresh_models).
        self._configured = list(dict.fromkeys([default_model, *models]))
        self.models = list(self._configured)
        self._installed_checked = float("-inf")
        self.keep_alive = keep_alive
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=120.0)
        # Whether each model can reason ("thinking" capability), learned from /api/show.
        self._thinking: dict[str, bool] = {}

    async def refresh_models(self, max_age: float = 300.0) -> list[str]:
        """Add every model installed on the server (GET /api/tags) to `models`, at most every `max_age`
        seconds. If the server can't be reached, the configured models are offered."""
        now = time.monotonic()
        if now - self._installed_checked < max_age:
            return self.models
        self._installed_checked = now
        try:
            response = await self._client.get("/api/tags", timeout=3.0)
            installed = [m["name"] for m in response.json().get("models", [])]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return self.models
        # Ollama names a model without a tag "name:latest"; keep the configured spelling.
        configured = {m if ":" in m else f"{m}:latest" for m in self._configured}
        extra = sorted(m for m in installed if m not in configured and m not in self._configured)
        self.models = list(dict.fromkeys([*self._configured, *extra]))
        return self.models

    def request_body(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
        think: bool | None = None,
        tools: Sequence[ToolSpec] | None = None,
    ) -> dict:
        chat = [_chat_message(m) for m in messages]
        if system:
            chat.insert(0, {"role": "system", "content": system})
        name = model or self.default_model
        body = {"model": name, "messages": chat, "stream": True}
        # Only reasoning models accept `think`; send it unless the model is known not to.
        if think is not None and self._thinking.get(name, True):
            body["think"] = think
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        return body

    async def _learn_thinking(self, model: str) -> None:
        if model in self._thinking:
            return
        try:
            response = await self._client.post("/api/show", json={"model": model})
            if not response.is_success:
                return
            capabilities = response.json().get("capabilities") or []
        except (httpx.HTTPError, ValueError):
            return  # try again next time; meanwhile `think` is left out only if known unsupported
        self._thinking[model] = "thinking" in capabilities

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
        think: bool | None = None,
        tools: Sequence[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if think is not None:
            await self._learn_thinking(model or self.default_model)
        body = self.request_body(messages, model=model, system=system, think=think, tools=tools)
        try:
            async with self._client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")[:200]
                    raise ProviderError(f"Ollama error {response.status_code}: {detail}")
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    message = chunk.get("message", {})
                    if text := message.get("content"):
                        yield TextDelta(text)
                    for call in message.get("tool_calls") or []:
                        function = call.get("function", {})
                        arguments = function.get("arguments") or {}
                        if isinstance(arguments, str):  # some models send a JSON string
                            try:
                                arguments = json.loads(arguments)
                            except ValueError:
                                arguments = {"_unparsed": arguments}
                        yield ToolCall(
                            id=call.get("id") or uuid.uuid4().hex[:12],
                            name=function.get("name", ""),
                            arguments=arguments if isinstance(arguments, dict) else {},
                        )
                    if chunk.get("done"):
                        yield Completed(
                            model=chunk.get("model", body["model"]),
                            stop_reason=chunk.get("done_reason"),
                            input_tokens=chunk.get("prompt_eval_count"),
                            output_tokens=chunk.get("eval_count"),
                        )
                        return
        except httpx.HTTPError as exc:
            raise ProviderError("Could not reach Ollama; is it running?") from exc
        raise ProviderError("Ollama ended the stream without finishing")
