"""Chat through the agent loop (VARA_CHAT_TOOLS, on by default): the model calls tools itself, each call is
in the audit trail, changes wait for approval, and the reply keeps every chat feature (streaming, saving,
the web_search/market_data/deals fields)."""

import json
import threading

from fastapi.testclient import TestClient

from tests.test_agent import call, pending, wait_for
from tests.test_search import FakeWeb
from vara.api import TOOLS_ABILITY
from vara.config import Settings
from vara.main import create_app
from vara.providers.fake import FakeProvider
from vara.router import ModelRouter, PrivacyMode


def tools_client(model: FakeProvider, *, web=None, **settings) -> TestClient:
    if web is not None:
        settings.setdefault("web_search", "auto")
    app = create_app(
        Settings(**settings), router=ModelRouter([model], PrivacyMode.LOCAL_ONLY), web_search=web
    )
    return TestClient(app)


def ask(client: TestClient, text: str, **extra) -> dict:
    response = client.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": text}], **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_chat_runs_tools_and_answers_from_their_results() -> None:
    model = FakeProvider("local", script=[[call("current_time")], "It's teatime."])
    with tools_client(model) as client:
        reply = ask(client, "what time is it?", client_time="2026-11-14T16:05:00-05:00")
        conversation = client.get(f"/v1/conversations/{reply['conversation_id']}").json()
        run = client.get(f"/v1/agent/runs/{reply['agent_run_id']}").json()

    assert reply["text"].strip() == "It's teatime."
    assert [(a["action"], a["status"]) for a in reply["actions"]] == [("current_time", "succeeded")]
    # The model saw the tool's result before answering.
    assert "4:05 PM" in model.calls[1][-1].content
    # Saved once, as an ordinary chat exchange, and the run points at it.
    assert [m["role"] for m in conversation["messages"]] == ["user", "assistant"]
    assert conversation["messages"][1]["content"].strip() == "It's teatime."
    assert run["conversation_id"] == reply["conversation_id"]
    assert run["status"] == "completed"


def test_web_search_results_fill_the_reply_like_the_fixed_steps_did() -> None:
    web = FakeWeb()
    model = FakeProvider("local", script=[[call("web_search", query="us president")], "Trump."])
    with tools_client(model, web=web) as client:
        reply = ask(client, "who is the president?")

    assert web.queries == [("us president", "general")]
    assert reply["web_search"]["query"] == "us president"
    assert reply["web_search"]["results"][0]["url"] == "https://wh.example"
    # The model is told it can look things up, so it never claims it can't.
    assert TOOLS_ABILITY in model.systems[0]
    assert "web_search" in model.tools_offered[0]


def test_web_search_off_or_memory_off_removes_those_tools() -> None:
    model = FakeProvider("local", script=["Hi."])
    with tools_client(model, web=FakeWeb()) as client:
        ask(client, "hello", web_search=False, use_memory=False)

    offered = set(model.tools_offered[0])
    assert not offered & {"web_search", "market_quotes", "find_prices", "search_memory"}
    assert "current_time" in offered
    assert TOOLS_ABILITY not in (model.systems[0] or "")


def test_tools_false_answers_without_the_agent() -> None:
    model = FakeProvider("local")
    with tools_client(model) as client:
        reply = ask(client, "hello", tools=False)

    assert reply["agent_run_id"] is None
    assert model.tools_offered == [[]] or model.tools_offered == []


def test_a_paused_agent_still_answers_without_tools() -> None:
    model = FakeProvider("local")
    with tools_client(model) as client:
        client.post("/v1/agent/pause")
        reply = ask(client, "hello")

    assert reply["text"] == "echo: hello "
    assert reply["agent_run_id"] is None


def test_a_model_that_cannot_use_tools_still_answers() -> None:
    model = FakeProvider("local", tools=False)
    with tools_client(model) as client:
        reply = ask(client, "hello")
        runs = client.get("/v1/agent/runs").json()["runs"]

    assert reply["text"] == "echo: hello "
    assert reply["agent_run_id"] is None
    assert runs[0]["status"] == "failed"  # the attempt is still in the audit trail


def test_a_change_asked_for_in_chat_waits_for_approval() -> None:
    model = FakeProvider(
        "local", script=[[call("watch_price", product="Steam Deck OLED")], "Watching it."]
    )
    with tools_client(model) as client:
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(ask(client, "watch the steam deck price"))
        )
        thread.start()
        [approval] = wait_for(lambda: pending(client))
        assert approval["action"] == "watch_price"
        client.post(f"/v1/agent/approvals/{approval['id']}", json={"decision": "approve"})
        thread.join(5)
        watches = client.get("/v1/deals/watches").json()

    assert result["text"].strip() == "Watching it."
    assert result["actions"][0]["status"] == "succeeded"
    assert [w["query"] for w in watches] == ["Steam Deck OLED"]


def test_streamed_chat_sends_every_step_text_and_one_done() -> None:
    model = FakeProvider("local", script=[[call("current_time")], "It's teatime."])
    with tools_client(model) as client:
        response = client.post(
            "/v1/chat/stream", json={"messages": [{"role": "user", "content": "time?"}]}
        )

    events = [
        (block.split("\n")[0].removeprefix("event: "), json.loads(block.split("data: ", 1)[1]))
        for block in response.text.strip().split("\n\n")
        if block.startswith("event:")
    ]
    names = [name for name, _ in events]
    assert names.count("done") == 1 and names[-1] == "done"
    assert "".join(d["text"] for name, d in events if name == "delta").strip() == "It's teatime."
    assert events[-1][1]["actions"][0]["action"] == "current_time"
