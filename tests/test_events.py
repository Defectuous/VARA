import asyncio

from fastapi.testclient import TestClient

from vara.config import Settings
from vara.events import EventBus
from vara.main import create_app
from vara.providers.fake import FakeProvider
from vara.router import ModelRouter, PrivacyMode


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, event: str, data: dict) -> None:
        self.events.append((event, data))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


def client_with_bus(provider: FakeProvider) -> tuple[TestClient, RecordingBus]:
    app = create_app(Settings(), router=ModelRouter([provider], PrivacyMode.LOCAL_ONLY))
    bus = RecordingBus()
    app.state.events = bus
    return TestClient(app), bus


def ask(text: str, **extra) -> dict:
    return {"messages": [{"role": "user", "content": text}], **extra}


def test_subscribers_receive_published_events_until_they_leave():
    bus = EventBus()

    async def run():
        with bus.subscribe() as queue:
            bus.publish("exchange_started", {"text": "hi"})
            assert await queue.get() == ("exchange_started", {"text": "hi"})
            assert bus.subscribers == 1
        assert bus.subscribers == 0
        bus.publish("exchange_started", {"text": "nobody listening"})  # no error

    asyncio.run(run())


def test_a_voice_exchange_is_announced_from_question_to_stored_reply():
    client, bus = client_with_bus(FakeProvider("local", reply="It is three."))

    response = client.post("/v1/chat/stream", json=ask("what time is it", spoken=True))

    assert response.status_code == 200
    assert bus.names() == [
        "exchange_started",
        "exchange_delta",
        "exchange_delta",
        "exchange_delta",
        "exchange_done",
    ]
    started, done = bus.events[0][1], bus.events[-1][1]
    assert started["text"] == "what time is it"
    assert started["source"] == "voice"
    assert started["conversation_id"] is None
    assert "".join(d["text"] for n, d in bus.events if n == "exchange_delta") == "It is three. "
    assert done["conversation_id"] == 1
    assert done["text"] == "It is three. "
    # Every event of one exchange carries the same id.
    assert len({d["exchange_id"] for _, d in bus.events}) == 1


def test_the_sending_client_id_is_carried_so_a_page_can_skip_its_own():
    client, bus = client_with_bus(FakeProvider("local"))

    client.post("/v1/chat", json=ask("hello", client="page-123"))

    assert bus.names()[0] == "exchange_started"
    assert bus.names()[-1] == "exchange_done"
    assert {d["client"] for _, d in bus.events} == {"page-123"}
    assert bus.events[0][1]["source"] == "chat"


def test_a_failed_reply_is_announced_as_failed():
    client, bus = client_with_bus(FakeProvider("local", fail=True))

    client.post("/v1/chat/stream", json=ask("hello"))

    assert bus.names()[0] == "exchange_started"
    assert bus.names()[-1] == "exchange_failed"
    assert "exchange_done" not in bus.names()
