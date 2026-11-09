import json
import random

from fastapi.testclient import TestClient

from vara.config import Settings
from vara.holding import FIRST_PLAIN, FIRST_SNARKY, STILL_PLAIN, STILL_SNARKY, holding_line
from vara.main import create_app
from vara.providers.fake import FakeProvider
from vara.router import ModelRouter, PrivacyMode

HELLO = {"messages": [{"role": "user", "content": "hello"}]}


def client_for(provider: FakeProvider, mode=PrivacyMode.LOCAL_ONLY, **settings) -> TestClient:
    return TestClient(create_app(Settings(**settings), router=ModelRouter([provider], mode)))


def events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        name, data = block.split("\n")
        out.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def test_first_line_says_it_will_take_a_minute_and_later_ones_say_still_working() -> None:
    rng = random.Random(1)
    assert holding_line(0, personality=True, rng=rng) in FIRST_SNARKY
    assert holding_line(1, personality=True, rng=rng) in STILL_SNARKY
    assert holding_line(0, personality=False, rng=rng) in FIRST_PLAIN
    assert holding_line(3, personality=False, rng=rng) in STILL_PLAIN


def test_a_fast_reply_has_no_notice() -> None:
    client = client_for(FakeProvider("local"), hold_notice_seconds=5)

    names = [name for name, _ in events(client.post("/v1/chat/stream", json=HELLO).text)]

    assert "notice" not in names and names[-1] == "done"


def test_a_slow_reply_says_so_then_keeps_updating_then_answers() -> None:
    client = client_for(
        FakeProvider("local", delay=0.5), hold_notice_seconds=0.05, hold_repeat_seconds=0.15
    )

    stream = events(client.post("/v1/chat/stream", json=HELLO).text)
    names = [name for name, _ in stream]

    assert names[0] == "notice" and names.count("notice") >= 2
    assert names.index("delta") > max(i for i, n in enumerate(names) if n == "notice")
    assert names[-1] == "done"
    assert stream[1][1]["text"] in STILL_SNARKY
    saved = client.get(f"/v1/conversations/{stream[-1][1]['conversation_id']}").json()
    assert saved["messages"][-1]["content"].strip() == "echo: hello"


def test_repeated_notices_can_be_turned_off() -> None:
    client = client_for(
        FakeProvider("local", delay=0.4), hold_notice_seconds=0.05, hold_repeat_seconds=0
    )

    names = [name for name, _ in events(client.post("/v1/chat/stream", json=HELLO).text)]

    assert names.count("notice") == 1 and names[-1] == "done"


def test_a_provider_failure_after_a_notice_is_an_error_event() -> None:
    client = client_for(FakeProvider("local", delay=0.3, fail=True), hold_notice_seconds=0.05)

    response = client.post("/v1/chat/stream", json=HELLO)

    assert response.status_code == 200
    names = [name for name, _ in events(response.text)]
    assert names[0] == "notice" and names[-1] == "error"


def test_privacy_refusals_are_still_immediate_http_errors() -> None:
    client = client_for(FakeProvider("cloud", local=False, delay=0.3), hold_notice_seconds=0.05)

    assert client.post("/v1/chat/stream", json=HELLO).status_code == 503  # not a 200 stream
