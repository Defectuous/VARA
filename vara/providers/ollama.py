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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.default_model = default_model
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=120.0)

    def request_body(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
    ) -> dict:
        chat = [{"role": m.role, "content": m.content} for m in messages]
        if system:
            chat.insert(0, {"role": "system", "content": system})
        return {"model": model or self.default_model, "messages": chat, "stream": True}

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        body = self.request_body(messages, model=model, system=system)
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
