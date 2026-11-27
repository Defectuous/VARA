"""Providers that speak OpenAI's Chat Completions API: OpenAI itself and xAI (Grok), and later Groq and
OpenRouter, which use the same format at their own base URL."""

import json
import uuid
from collections.abc import AsyncIterator, Sequence

import httpx

from vara.logging import redact
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

OPENAI_URL = "https://api.openai.com/v1"
XAI_URL = "https://api.x.ai/v1"


def _chat_message(m: Message) -> dict:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    entry: dict = {"role": m.role, "content": m.content}
    if m.tool_calls:
        entry["content"] = m.content or None
        entry["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in m.tool_calls
        ]
    return entry


def _parse_arguments(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"_unparsed": raw}
    return parsed if isinstance(parsed, dict) else {}


class OpenAICompatibleProvider:
    """One Chat Completions backend. `label` is what error messages call it ("OpenAI", "xAI")."""

    def __init__(
        self,
        name: str,
        api_key: str,
        *,
        label: str,
        base_url: str,
        default_model: str,
        context_length: int = 128_000,
        vision: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.label = label
        self.default_model = default_model
        self.capabilities = Capabilities(
            local=False, context_length=context_length, vision=vision, tools=True
        )
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120.0,
        )

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
        body: dict = {
            "model": model or self.default_model,
            "messages": chat,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
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
        return body

    def _status_error(self, status: int, detail: str) -> ProviderError:
        if status == 401:
            return ProviderError(f"{self.label} rejected the API key")
        if status == 403:
            return ProviderError(f"{self.label} API key lacks permission for this request")
        if status == 429:
            return ProviderError(f"{self.label} rate limit reached")
        return ProviderError(f"{self.label} error {status}: {redact(detail[:200])}")

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
        think: bool | None = None,
        tools: Sequence[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        body = self.request_body(messages, model=model, system=system, tools=tools)
        answered_by = body["model"]
        stop_reason: str | None = None
        usage: dict = {}
        # Tool calls arrive in pieces, keyed by index: the id and name first, the arguments in parts.
        calls: dict[int, dict] = {}
        finished = False
        try:
            async with self._client.stream("POST", "/chat/completions", json=body) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")
                    raise self._status_error(response.status_code, detail)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        finished = True
                        break
                    chunk = json.loads(data)
                    answered_by = chunk.get("model") or answered_by
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or ():
                        delta = choice.get("delta") or {}
                        if text := delta.get("content"):
                            yield TextDelta(text)
                        for piece in delta.get("tool_calls") or ():
                            call = calls.setdefault(
                                piece.get("index", 0), {"id": None, "name": "", "arguments": ""}
                            )
                            call["id"] = piece.get("id") or call["id"]
                            function = piece.get("function") or {}
                            call["name"] += function.get("name") or ""
                            call["arguments"] += function.get("arguments") or ""
                        if choice.get("finish_reason"):
                            stop_reason = choice["finish_reason"]
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach the {self.label} API") from exc
        if not finished and stop_reason is None:
            raise ProviderError(f"{self.label} ended the stream without finishing")

        for _, call in sorted(calls.items()):
            yield ToolCall(
                id=call["id"] or uuid.uuid4().hex[:12],
                name=call["name"],
                arguments=_parse_arguments(call["arguments"]),
            )
        yield Completed(
            model=answered_by,
            stop_reason=stop_reason,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
