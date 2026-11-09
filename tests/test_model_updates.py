import asyncio
import json

import httpx

from vara.model_updates import maintain_models, update_models


def fake_ollama(digests: dict[str, str], new_digests: dict[str, str], fail: set[str] = frozenset()):
    """An Ollama that reports `digests`, and after a pull reports `new_digests` for those models."""
    pulled: list[str] = []
    current = dict(digests)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            models = [{"name": n, "digest": d} for n, d in current.items()]
            return httpx.Response(200, json={"models": models})
        if request.url.path == "/api/pull":
            name = json.loads(request.content)["model"]
            pulled.append(name)
            if name in fail:
                return httpx.Response(500, json={"error": "registry unreachable"})
            current[name] = new_digests.get(name, current[name])
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404)

    client = httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler))
    return client, pulled


def test_every_installed_model_is_pulled_and_changes_reported():
    client, pulled = fake_ollama(
        {"qwen3:8b": "aaa", "qwen3:14b": "bbb"}, new_digests={"qwen3:14b": "ccc"}
    )
    results = asyncio.run(update_models(client))
    assert pulled == ["qwen3:8b", "qwen3:14b"]
    assert results == {"qwen3:8b": "up to date", "qwen3:14b": "updated"}


def test_one_failed_pull_does_not_stop_the_others():
    client, pulled = fake_ollama({"a": "1", "b": "2"}, new_digests={}, fail={"a"})
    results = asyncio.run(update_models(client))
    assert pulled == ["a", "b"]
    assert results["a"].startswith("failed")
    assert results["b"] == "up to date"


def test_maintain_runs_at_startup_and_records_the_result(tmp_path):
    client, pulled = fake_ollama({"qwen3:8b": "aaa"}, new_digests={})
    status = tmp_path / "ollama-updates.json"

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(maintain_models("http://ollama", status, stop, client=client))
        while not status.exists():
            await asyncio.sleep(0.01)
        stop.set()
        await task

    asyncio.run(run())
    assert pulled == ["qwen3:8b"]
    assert json.loads(status.read_text())["models"] == {"qwen3:8b": "up to date"}


def test_unreachable_ollama_is_logged_not_raised(tmp_path):
    def handler(request):
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler))

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(
            maintain_models("http://ollama", tmp_path / "s.json", stop, client=client)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(run())
