import json

from fastapi.testclient import TestClient

from vara.config import Settings
from vara.main import create_app
from vara.providers.fake import FakeProvider
from vara.router import ModelRouter, PrivacyMode

HELLO = {"messages": [{"role": "user", "content": "hello"}]}


def client_for(*providers: FakeProvider, mode: PrivacyMode) -> TestClient:
    return TestClient(create_app(Settings(), router=ModelRouter(providers, mode)))


def test_chat_returns_answer_and_which_model_answered() -> None:
    response = client_for(FakeProvider("local"), mode=PrivacyMode.LOCAL_ONLY).post(
        "/v1/chat", json=HELLO
    )

    assert response.status_code == 200
    assert response.json() == {
        "conversation_id": 1,
        "message_id": 2,
        "provider": "local",
        "model": "local-model",
        "text": "echo: hello ",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": None, "output_tokens": None},
        "cost_usd": 0.0,
        "memories_used": [],
    }


def test_chat_blocked_by_local_only_returns_403() -> None:
    client = client_for(FakeProvider("cloud", local=False), mode=PrivacyMode.LOCAL_ONLY)

    response = client.post("/v1/chat", json={**HELLO, "provider": "cloud"})

    assert response.status_code == 403


def test_chat_in_ask_mode_needs_confirm_cloud() -> None:
    client = client_for(FakeProvider("cloud", local=False), mode=PrivacyMode.ASK)

    assert client.post("/v1/chat", json=HELLO).status_code == 409
    assert client.post("/v1/chat", json={**HELLO, "confirm_cloud": True}).status_code == 200


def test_chat_reports_provider_failures() -> None:
    client = client_for(FakeProvider("local", fail=True), mode=PrivacyMode.LOCAL_ONLY)

    response = client.post("/v1/chat", json=HELLO)

    assert response.status_code == 503
    assert response.json()["detail"]["errors"] == {"local": "local is unavailable"}


def test_providers_endpoint_lists_mode_and_providers() -> None:
    client = client_for(
        FakeProvider("local"), FakeProvider("cloud", local=False), mode=PrivacyMode.ASK
    )

    body = client.get("/v1/providers").json()

    assert body["privacy_mode"] == "ask"
    assert [p["name"] for p in body["providers"]] == ["local", "cloud"]


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        name, data = block.split("\n")
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def test_stream_sends_deltas_then_done() -> None:
    client = client_for(FakeProvider("local", reply="hi there"), mode=PrivacyMode.LOCAL_ONLY)

    response = client.post("/v1/chat/stream", json=HELLO)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert [name for name, _ in events] == ["delta", "delta", "done"]
    assert "".join(data["text"] for name, data in events if name == "delta") == "hi there "
    assert events[-1][1]["provider"] == "local"
    assert events[-1][1]["cost_usd"] == 0.0


def test_stream_privacy_block_is_an_http_error_not_a_stream() -> None:
    client = client_for(FakeProvider("cloud", local=False), mode=PrivacyMode.LOCAL_ONLY)

    response = client.post("/v1/chat/stream", json={**HELLO, "provider": "cloud"})

    assert response.status_code == 403


def test_preview_shows_exact_request_without_calling_the_model() -> None:
    local = FakeProvider("local")
    cloud = FakeProvider("cloud", local=False)
    client = client_for(local, cloud, mode=PrivacyMode.ALLOWED)

    response = client.post("/v1/chat/preview", json={**HELLO, "system": "be brief"})

    assert response.status_code == 200
    candidates = response.json()["candidates"]
    assert [(c["provider"], c["local"]) for c in candidates] == [("local", True), ("cloud", False)]
    assert candidates[0]["request"] == {
        "model": "local-model",
        "system": "be brief",
        "messages": [{"role": "user", "content": "hello"}],
    }
    assert local.calls == [] and cloud.calls == []


def test_preview_respects_privacy_mode() -> None:
    client = client_for(
        FakeProvider("local"), FakeProvider("cloud", local=False), mode=PrivacyMode.LOCAL_ONLY
    )

    candidates = client.post("/v1/chat/preview", json=HELLO).json()["candidates"]

    assert [c["provider"] for c in candidates] == ["local"]


def test_usage_totals_accumulate_cost_for_priced_cloud_models() -> None:
    cloud = FakeProvider("anthropic", local=False, usage=(1_000_000, 100_000))
    cloud.default_model = "claude-opus-5-5"
    client = client_for(cloud, mode=PrivacyMode.ALLOWED)

    first = client.post("/v1/chat", json=HELLO).json()
    client.post("/v1/chat", json=HELLO)
    usage = client.get("/v1/usage").json()

    assert first["cost_usd"] == 6.0  # $4 input + $2 output
    assert usage["total_cost_usd"] == 12.0
    assert usage["totals"] == [
        {
            "provider": "anthropic",
            "model": "claude-opus-5-5",
            "requests": 2,
            "input_tokens": 2_000_000,
            "output_tokens": 200_000,
            "cost_usd": 12.0,
            "unpriced_requests": 0,
        }
    ]


def test_unknown_cloud_model_cost_is_none() -> None:
    client = client_for(
        FakeProvider("cloud", local=False, usage=(10, 10)), mode=PrivacyMode.ALLOWED
    )

    assert client.post("/v1/chat", json=HELLO).json()["cost_usd"] is None
    assert client.get("/v1/usage").json()["totals"][0]["unpriced_requests"] == 1
