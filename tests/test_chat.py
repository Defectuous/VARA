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
        "provider": "local",
        "model": "local-model",
        "text": "echo: hello ",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": None, "output_tokens": None},
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
