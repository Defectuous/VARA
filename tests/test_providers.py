import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from vara.providers.anthropic import AnthropicProvider
from vara.providers.base import Completed, Message, ProviderError, TextDelta
from vara.providers.ollama import OllamaProvider

HELLO = [Message("user", "hello")]


def drain(provider, **kwargs) -> list:
    async def run() -> list:
        return [event async for event in provider.stream(HELLO, **kwargs)]

    return asyncio.run(run())


class _FakeAnthropicStream:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    @property
    async def text_stream(self):
        for chunk in self._chunks:
            yield chunk

    async def get_final_message(self):
        return SimpleNamespace(
            model="claude-opus-5-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
        )


class _FakeAnthropicClient:
    def __init__(self) -> None:
        self.params: dict = {}
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        self.params = params
        return _FakeAnthropicStream(["Hi", " there"])


def test_anthropic_streams_text_and_usage() -> None:
    fake = _FakeAnthropicClient()
    provider = AnthropicProvider("unused", client=fake)

    events = drain(provider, system="be brief")

    assert events == [
        TextDelta("Hi"),
        TextDelta(" there"),
        Completed("claude-opus-5-5", "end_turn", 5, 2),
    ]
    assert fake.params["model"] == "claude-opus-5-5"
    assert fake.params["system"] == "be brief"
    assert fake.params["messages"] == [{"role": "user", "content": "hello"}]
    assert fake.params["output_config"] == {"effort": "medium"}
    assert fake.params["fallbacks"] == "default"


def _ollama(handler) -> OllamaProvider:
    client = httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler))
    return OllamaProvider(client=client)


def test_ollama_streams_ndjson_chunks() -> None:
    lines = [
        {"message": {"content": "Hi"}, "done": False},
        {"message": {"content": " there"}, "done": False},
        {
            "model": "llama3.1:8b",
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 4,
            "eval_count": 2,
        },
    ]
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, text="\n".join(json.dumps(line) for line in lines))

    events = drain(_ollama(handler), system="be brief")

    assert events == [TextDelta("Hi"), TextDelta(" there"), Completed("llama3.1:8b", "stop", 4, 2)]
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    assert sent["stream"] is True


def test_ollama_unreachable_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError, match="Could not reach Ollama"):
        drain(_ollama(handler))


def test_ollama_http_error_raises_provider_error() -> None:
    provider = _ollama(lambda request: httpx.Response(404, text='{"error":"model not found"}'))

    with pytest.raises(ProviderError, match="404"):
        drain(provider)


def test_ollama_sends_keep_alive_when_configured() -> None:
    provider = OllamaProvider(keep_alive="2h")

    assert provider.request_body([Message("user", "hi")])["keep_alive"] == "2h"
    assert "keep_alive" not in OllamaProvider().request_body([Message("user", "hi")])


def _ollama_with_capabilities(capabilities: list[str], sent: list[dict]) -> OllamaProvider:
    done = {"model": "llama3.1:8b", "done": True, "done_reason": "stop"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": capabilities})
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=json.dumps(done))

    return _ollama(handler)


def test_ollama_sends_think_to_reasoning_models() -> None:
    sent: list[dict] = []
    provider = _ollama_with_capabilities(["completion", "thinking"], sent)

    drain(provider, think=False)
    drain(provider, think=True)

    assert [body["think"] for body in sent] == [False, True]


def test_ollama_leaves_think_out_for_models_that_cannot_reason() -> None:
    sent: list[dict] = []

    drain(_ollama_with_capabilities(["completion"], sent), think=True)

    assert "think" not in sent[0]


def test_ollama_leaves_think_out_when_not_asked() -> None:
    assert "think" not in OllamaProvider().request_body([Message("user", "hi")])


def test_ollama_lists_the_default_model_first_without_duplicates() -> None:
    from vara.config import Settings
    from vara.providers.ollama import OllamaProvider

    settings = Settings(ollama_model="qwen3:8b", ollama_models=" qwen3:14b, qwen3:8b ,")
    assert settings.ollama_model_list == ["qwen3:8b", "qwen3:14b"]
    provider = OllamaProvider(default_model="qwen3:8b", models=settings.ollama_model_list)
    assert provider.models == ["qwen3:8b", "qwen3:14b"]


def _sse(chunks: list) -> str:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _openai(handler, name: str = "openai", label: str = "OpenAI"):
    from vara.providers.openai_compat import OpenAICompatibleProvider

    client = httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handler))
    return OpenAICompatibleProvider(
        name, "unused", label=label, base_url="http://api", default_model="gpt-5", client=client
    )


def test_openai_compatible_streams_text_and_usage() -> None:
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["path"] = request.url.path
        sent.update(json.loads(request.content))
        return httpx.Response(
            200,
            text=_sse(
                [
                    {"model": "gpt-5-2026", "choices": [{"delta": {"content": "Hi"}}]},
                    {"model": "gpt-5-2026", "choices": [{"delta": {"content": " there"}}]},
                    {"model": "gpt-5-2026", "choices": [{"delta": {}, "finish_reason": "stop"}]},
                    {
                        "model": "gpt-5-2026",
                        "choices": [],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                    },
                ]
            ),
        )

    events = drain(_openai(handler), system="be brief")

    assert events == [TextDelta("Hi"), TextDelta(" there"), Completed("gpt-5-2026", "stop", 5, 2)]
    assert sent["path"] == "/chat/completions"
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    assert sent["stream_options"] == {"include_usage": True}


def test_openai_compatible_assembles_tool_calls_from_pieces() -> None:
    from vara.providers.base import ToolCall, ToolSpec

    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "web_search", "arguments": '{"que'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ry": "rain"}'}}]}}
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, text=_sse(chunks))

    tool = ToolSpec("web_search", "Search the web", {"type": "object"})
    events = drain(_openai(handler, "xai", "xAI"), tools=[tool])

    assert events[0] == ToolCall("call_1", "web_search", {"query": "rain"})
    assert events[1].stop_reason == "tool_calls"
    assert sent["tools"][0]["function"]["name"] == "web_search"


def test_openai_compatible_sends_tool_turns_in_its_format() -> None:
    from vara.providers.base import ToolCall

    provider = _openai(lambda request: httpx.Response(200))
    call = ToolCall("call_1", "web_search", {"query": "rain"})
    body = provider.request_body(
        [
            Message("user", "rain?"),
            Message("assistant", "", tool_calls=(call,)),
            Message("tool", "No rain", tool_call_id="call_1", name="web_search"),
        ]
    )

    assistant, tool_turn = body["messages"][1], body["messages"][2]
    assert assistant["content"] is None
    assert assistant["tool_calls"][0]["function"] == {
        "name": "web_search",
        "arguments": '{"query": "rain"}',
    }
    assert tool_turn == {"role": "tool", "tool_call_id": "call_1", "content": "No rain"}


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "xAI rejected the API key"), (429, "xAI rate limit reached"), (500, "xAI error 500")],
)
def test_openai_compatible_http_errors_raise_provider_error(status: int, message: str) -> None:
    provider = _openai(lambda request: httpx.Response(status, text="nope"), "xai", "xAI")

    with pytest.raises(ProviderError, match=message):
        drain(provider)


def test_openai_compatible_unreachable_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError, match="Could not reach the OpenAI API"):
        drain(_openai(handler))


def test_cloud_providers_register_only_with_a_key(monkeypatch) -> None:
    from vara import main
    from vara.config import Settings

    keys = {"openai": "sk-test", "xai": "xai-test"}
    monkeypatch.setattr(main, "get_api_key", keys.get)

    names = [p.name for p in main.build_router(Settings()).providers]

    assert names == ["ollama", "openai", "xai"]


def test_reasoning_goes_back_with_the_tool_calls() -> None:
    """DeepSeek and Kimi refuse a tool loop whose assistant turn lost its `reasoning_content`."""
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "Need the "}}]},
        {"choices": [{"delta": {"reasoning_content": "forecast."}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "function": {"name": "weather", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    provider = _openai(lambda request: httpx.Response(200, text=_sse(chunks)), "kimi", "Kimi")

    events = drain(provider)
    call = events[0]
    body = provider.request_body(
        [Message("user", "rain?"), Message("assistant", "", tool_calls=(call,))]
    )

    assert not any(isinstance(e, TextDelta) for e in events)  # reasoning isn't shown
    assert body["messages"][1]["reasoning_content"] == "Need the forecast."


def test_extra_tool_call_fields_and_calls_without_index_round_trip() -> None:
    """Gemini sends whole calls without `index`, with its own fields that must be sent back."""
    signature = {"google": {"thought_signature": "abc"}}

    def call(call_id: str, name: str) -> dict:
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
            "extra_content": signature,
        }

    chunks = [
        {"choices": [{"delta": {"tool_calls": [call("a", "time")]}}]},
        {"choices": [{"delta": {"tool_calls": [call("b", "weather")]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    provider = _openai(lambda request: httpx.Response(200, text=_sse(chunks)), "gemini", "Gemini")

    first, second = drain(provider)[:2]
    sent = provider.request_body([Message("assistant", "", tool_calls=(first, second))])

    assert (first.name, second.name) == ("time", "weather")
    assert sent["messages"][0]["tool_calls"][1]["extra_content"] == signature
    assert "reasoning_content" not in sent["messages"][0]


def test_deepseek_thinking_switch_follows_think() -> None:
    from vara.providers.openai_compat import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        "deepseek",
        "k",
        label="DeepSeek",
        base_url="http://api",
        default_model="m",
        thinking_switch=True,
    )
    hi = [Message("user", "hi")]

    assert provider.request_body(hi, think=False)["thinking"] == {"type": "disabled"}
    assert provider.request_body(hi, think=True)["thinking"] == {"type": "enabled"}
    assert "thinking" not in provider.request_body(hi)
    assert "thinking" not in _openai(lambda r: httpx.Response(200)).request_body(hi, think=True)


def test_every_cloud_provider_registers_with_its_key(monkeypatch) -> None:
    from vara import main
    from vara.config import Settings

    names = ["anthropic", "openai", "xai", "gemini", "deepseek", "kimi", "perplexity"]
    monkeypatch.setattr(main, "get_api_key", lambda name: "key" if name in names else None)

    providers = main.build_router(Settings()).providers

    assert [p.name for p in providers] == ["ollama", *names]
    assert all(p.capabilities.tools for p in providers)
