from collections.abc import AsyncIterator, Sequence

import anthropic

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

# Server-side fallback re-runs a request on another Claude model if a safety classifier declines it.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _api_messages(messages: Sequence[Message]) -> list[dict]:
    """Claude's message format: tool calls are `tool_use` blocks in the assistant turn, and the results
    of one turn's calls go back together as `tool_result` blocks in a single user turn."""
    converted: list[dict] = []
    for m in messages:
        if m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            previous = converted[-1] if converted else None
            if (
                previous is not None
                and previous["role"] == "user"
                and isinstance(previous["content"], list)
                and previous["content"]
                and previous["content"][0].get("type") == "tool_result"
            ):
                previous["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
        elif m.tool_calls:
            blocks: list[dict] = [{"type": "text", "text": m.content}] if m.content else []
            blocks += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in m.tool_calls
            ]
            converted.append({"role": "assistant", "content": blocks})
        else:
            converted.append({"role": m.role, "content": m.content})
    return converted


class AnthropicProvider:
    name = "anthropic"
    capabilities = Capabilities(local=False, context_length=1_000_000, vision=True, tools=True)

    def __init__(
        self,
        api_key: str,
        *,
        default_model: str = "claude-opus-5-5",
        effort: str = "medium",
        max_tokens: int = 64000,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.default_model = default_model
        self._effort = effort
        self._max_tokens = max_tokens
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=120.0)

    def request_body(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
        think: bool | None = None,
        tools: Sequence[ToolSpec] | None = None,
    ) -> dict:
        params = {
            "model": model or self.default_model,
            "max_tokens": self._max_tokens,
            "messages": _api_messages(messages),
            "output_config": {"effort": self._effort},
            "betas": [_FALLBACK_BETA],
            "fallbacks": "default",
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        return params

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
        think: bool | None = None,
        tools: Sequence[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        params = self.request_body(messages, model=model, system=system, tools=tools)
        try:
            async with self._client.beta.messages.stream(**params) as stream:
                async for text in stream.text_stream:
                    yield TextDelta(text)
                final = await stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Anthropic rejected the API key") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderError("Anthropic API key lacks permission for this request") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("Anthropic rate limit reached") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                f"Anthropic error {exc.status_code}: {redact(exc.message)}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Could not reach the Anthropic API") from exc

        for block in getattr(final, "content", None) or ():
            if getattr(block, "type", None) == "tool_use":
                yield ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
        yield Completed(
            model=final.model,
            stop_reason=final.stop_reason,
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens,
        )
