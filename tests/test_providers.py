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
