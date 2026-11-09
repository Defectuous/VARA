import json
from collections.abc import AsyncIterator, Sequence

import httpx

from vara.providers.base import (
    Capabilities,
    Completed,
    Message,
    ProviderError,
    StreamEvent,
    TextDelta,
)


class OllamaProvider:
    name = "ollama"
    capabilities = Capabilities(local=True, context_length=8192)

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
        # Models offered for a manual choice; the default always comes first.
        self.models = list(dict.fromkeys([default_model, *models]))
        self.keep_alive = keep_alive
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=120.0)
        # Whether each model can reason ("thinking" capability), learned from /api/show.
        self._thinking: dict[str, bool] = {}

    def request_body(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
        think: bool | None = None,
    ) -> dict:
        chat = [{"role": m.role, "content": m.content} for m in messages]
        if system:
            chat.insert(0, {"role": "system", "content": system})
        name = model or self.default_model
        body = {"model": name, "messages": chat, "stream": True}
        # Only reasoning models accept `think`; send it unless the model is known not to.
        if think is not None and self._thinking.get(name, True):
            body["think"] = think
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
    ) -> AsyncIterator[StreamEvent]:
        if think is not None:
            await self._learn_thinking(model or self.default_model)
        body = self.request_body(messages, model=model, system=system, think=think)
        try:
            async with self._client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")[:200]
                    raise ProviderError(f"Ollama error {response.status_code}: {detail}")
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if text := chunk.get("message", {}).get("content"):
                        yield TextDelta(text)
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
