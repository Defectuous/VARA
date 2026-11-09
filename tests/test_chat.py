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
        "web_search": None,
        "market_data": None,
        "deals": None,
        "price_alerts": None,
        "reasoning": False,
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


def _sent_system(client: TestClient, **extra) -> str | None:
    response = client.post("/v1/chat/preview", json={**HELLO, **extra})
    return response.json()["candidates"][0]["request"]["system"]


def test_default_system_prompt_names_vara_and_today() -> None:
    from datetime import datetime

    system = _sent_system(client_for(FakeProvider("local"), mode=PrivacyMode.LOCAL_ONLY))

    assert "V.A.R.A." in system
    assert str(datetime.now().astimezone().year) in system and "{date}" not in system


def test_request_system_prompt_replaces_the_default() -> None:
    client = client_for(FakeProvider("local"), mode=PrivacyMode.LOCAL_ONLY)

    assert _sent_system(client, system="be brief") == "be brief"
    assert _sent_system(client, system="") is None


def test_configured_system_prompt_is_used() -> None:
    app = create_app(
        Settings(system_prompt="You are terse.", personality=""),
        router=ModelRouter([FakeProvider("local")], PrivacyMode.LOCAL_ONLY),
    )

    assert _sent_system(TestClient(app)) == "You are terse."


def test_personality_follows_the_configured_prompt_but_not_a_request_prompt() -> None:
    app = create_app(
        Settings(system_prompt="You are terse.", personality="Be cheeky."),
        router=ModelRouter([FakeProvider("local")], PrivacyMode.LOCAL_ONLY),
    )
    client = TestClient(app)

    assert _sent_system(client) == "You are terse.\n\nBe cheeky."
    assert _sent_system(client, system="be brief") == "be brief"


def test_spoken_requests_ask_for_a_short_plain_reply() -> None:
    client = client_for(FakeProvider("local"), mode=PrivacyMode.LOCAL_ONLY)

    spoken = _sent_system(client, spoken=True)

    assert "V.A.R.A." in spoken and "spoken aloud" in spoken
    assert "spoken aloud" not in _sent_system(client)
    assert _sent_system(client, system="", spoken=True).startswith(
        "Your reply will be spoken aloud"
    )


def test_the_prompt_carries_the_current_date_and_time() -> None:
    from datetime import datetime, timedelta, timezone

    from vara.api import fill_clock

    now = datetime(2026, 11, 5, 7, 4, tzinfo=timezone(timedelta(hours=-5), "EST"))

    filled = fill_clock("Today is {date}, and the local time is {time} ({timezone}).", now, "EST")

    assert filled == "Today is Thursday, November 5, 2026, and the local time is 7:04 AM (EST)."


def test_default_prompt_has_no_unfilled_placeholders() -> None:
    system = _sent_system(client_for(FakeProvider("local"), mode=PrivacyMode.LOCAL_ONLY))

    assert "{" not in system and "local time is" in system


def test_the_asking_devices_clock_is_used_when_it_sends_one() -> None:
    client = client_for(FakeProvider("local"), mode=PrivacyMode.LOCAL_ONLY)

    system = _sent_system(
        client, client_time="2026-11-05T21:30:00+09:00", client_timezone="Asia/Tokyo"
    )

    assert "Thursday, November 5, 2026" in system
    assert "9:30 PM (Asia/Tokyo)" in system


def test_a_device_clock_without_a_zone_name_shows_its_offset() -> None:
    client = client_for(FakeProvider("local"), mode=PrivacyMode.LOCAL_ONLY)

    system = _sent_system(client, client_time="2026-11-05T07:04:00-05:00")

    assert "7:04 AM (UTC-05:00)" in system


def test_a_device_clock_must_include_its_offset() -> None:
    client = client_for(FakeProvider("local"), mode=PrivacyMode.LOCAL_ONLY)

    response = client.post("/v1/chat/preview", json={**HELLO, "client_time": "2026-11-05T07:04:00"})

    assert response.status_code == 422


def test_chat_picks_reasoning_per_message_and_reports_it() -> None:
    local = FakeProvider("local")
    client = client_for(local, mode=PrivacyMode.LOCAL_ONLY)

    quick = client.post("/v1/chat", json=HELLO).json()
    hard = client.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": "Why is the sky blue?"}]}
    ).json()
    forced = client.post("/v1/chat", json={**HELLO, "reasoning": "on"}).json()

    assert (quick["reasoning"], hard["reasoning"], forced["reasoning"]) == (False, True, True)
    assert local.thinks == [False, True, True]


def test_providers_endpoint_lists_each_providers_models() -> None:
    client = client_for(FakeProvider("local"), mode=PrivacyMode.LOCAL_ONLY)
    client.app.state.router.providers[0].models = ["local-model", "bigger-model"]

    body = client.get("/v1/providers").json()

    assert body["providers"][0]["models"] == ["local-model", "bigger-model"]


def test_providers_endpoint_defaults_models_to_the_default_model() -> None:
    client = client_for(FakeProvider("local"), mode=PrivacyMode.LOCAL_ONLY)

    body = client.get("/v1/providers").json()

    assert body["providers"][0]["models"] == ["local-model"]
