"""The agent loop (vara/agent.py), the typed actions, approvals, the audit trail, and the pause switch."""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from vara.actions import Action, ActionRegistry, ActionResult, Risk, default_registry
from vara.actions.base import NoParams
from vara.config import Settings
from vara.db.database import Database
from vara.db.models import AgentRun, RunStatus
from vara.main import create_app
from vara.providers.anthropic import _api_messages
from vara.providers.base import Message, ToolCall, ToolSpec
from vara.providers.fake import FakeProvider
from vara.providers.ollama import OllamaProvider
from vara.router import ModelRouter, PrivacyMode


def call(name: str, i: int = 1, **arguments) -> ToolCall:
    return ToolCall(id=f"call-{i}", name=name, arguments=arguments)


def make_client(*providers, mode=PrivacyMode.LOCAL_ONLY, actions=None, **settings) -> TestClient:
    app = create_app(Settings(**settings), router=ModelRouter(providers, mode), actions=actions)
    return TestClient(app)


def run(client: TestClient, goal: str = "do the thing", **body) -> dict:
    response = client.post("/v1/agent/runs", json={"goal": goal, "wait": True, **body})
    assert response.status_code == 200, response.text
    return response.json()


def start(client: TestClient, goal: str = "do the thing", **body) -> dict:
    response = client.post("/v1/agent/runs", json={"goal": goal, **body})
    assert response.status_code == 200, response.text
    return response.json()


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting")


def pending(client: TestClient) -> list[dict]:
    return client.get("/v1/agent/approvals").json()["approvals"]


def finished(client: TestClient, run_id: int) -> dict:
    def done():
        data = client.get(f"/v1/agent/runs/{run_id}").json()
        return data if data["status"] != "running" else None

    return wait_for(done)


# ---- read-only actions, the loop, and the audit trail ----


def test_a_read_only_action_runs_and_its_result_goes_back_to_the_model() -> None:
    model = FakeProvider("local", script=[[call("current_time")], "It's teatime."])
    with make_client(model) as client:
        data = run(client, "what time is it?", client_time="2026-11-14T16:05:00-05:00")

    assert data["status"] == "completed"
    assert data["final_text"] == "It's teatime."
    assert data["steps"] == 2
    [action] = data["actions"]
    assert action["action"] == "current_time"
    assert action["status"] == "succeeded"
    assert action["permission"] == "read-only"
    assert action["step"] == 1
    assert "4:05 PM" in action["result"]
    # The second model turn saw the tool call and its result.
    second = model.calls[1]
    assert second[-2].tool_calls[0].name == "current_time"
    assert second[-1].role == "tool" and "4:05 PM" in second[-1].content


def test_the_model_is_offered_only_the_tools_that_can_work() -> None:
    model = FakeProvider("local", script=["Hello."])
    with make_client(model) as client:
        run(client)
        tools = {t["name"]: t for t in client.get("/v1/agent/tools").json()["tools"]}

    offered = model.tools_offered[0]
    assert {"current_time", "search_memory", "watch_price"} <= set(offered)
    # Web search is off in tests, so the web-backed tools aren't offered.
    assert "web_search" not in offered and "find_prices" not in offered
    assert tools["web_search"]["available"] is False
    assert tools["watch_price"]["needs_approval"] is True
    assert tools["current_time"]["risk"] == "read_only"


def test_the_goal_and_final_answer_are_saved_to_the_conversation() -> None:
    model = FakeProvider("local", script=["First answer.", "Second answer."])
    with make_client(model) as client:
        first = run(client, "first question")
        second = run(client, "follow-up", conversation_id=first["conversation_id"])
        conversation = client.get(f"/v1/conversations/{first['conversation_id']}").json()

    assert second["conversation_id"] == first["conversation_id"]
    assert [m["content"] for m in conversation["messages"]] == [
        "first question",
        "First answer.",
        "follow-up",
        "Second answer.",
    ]
    # The follow-up was answered with the earlier exchange in view.
    assert [m.content for m in model.calls[1]] == ["first question", "First answer.", "follow-up"]


def test_memory_search_finds_remembered_facts() -> None:
    model = FakeProvider(
        "local", script=[[call("search_memory", query="favourite tea")], "Earl Grey."]
    )
    with make_client(model) as client:
        client.post("/v1/memories", json={"content": "My favourite tea is Earl Grey"})
        data = run(client, "what tea do I like?")

    assert "Earl Grey" in data["actions"][0]["result"]


def test_invalid_arguments_are_refused_and_the_model_is_told_why() -> None:
    model = FakeProvider(
        "local",
        script=[
            [call("watch_price", product="x" * 500)],  # too long
            [call("watch_price", 2, product="AirPods", colour="red")],  # unknown field
            "Sorry, I couldn't.",
        ],
    )
    with make_client(model) as client:
        data = run(client)

    assert [a["status"] for a in data["actions"]] == ["invalid", "invalid"]
    assert "product" in data["actions"][0]["error"]
    assert "colour" in data["actions"][1]["error"]
    assert "weren't valid" in model.calls[1][-1].content
    assert pending(client) == []  # nothing ever reached the approval queue


def test_an_unknown_tool_is_refused() -> None:
    model = FakeProvider("local", script=[[call("delete_everything")], "I can't do that."])
    with make_client(model) as client:
        data = run(client)

    [action] = data["actions"]
    assert action["status"] == "invalid"
    assert "no tool named" in model.calls[1][-1].content
    assert "current_time" in model.calls[1][-1].content  # told what it can use instead


def test_the_same_action_is_not_run_twice_in_a_run() -> None:
    counted = []

    async def count(_ctx, _params):
        counted.append(1)
        return ActionResult("counted")

    registry = ActionRegistry([Action("count", "Count.", NoParams, Risk.READ_ONLY, count)])
    model = FakeProvider("local", script=[[call("count"), call("count", 2)], "Done."])
    with make_client(model, actions=registry) as client:
        data = run(client)

    assert len(counted) == 1
    assert [a["status"] for a in data["actions"]] == ["succeeded", "duplicate"]
    assert "wasn't done again" in model.calls[1][-1].content


# ---- approvals ----


def test_a_change_waits_for_approval_and_runs_once_approved() -> None:
    model = FakeProvider(
        "local",
        script=[[call("watch_price", product="AirPods Pro", target_price=199)], "Watching."],
    )
    with make_client(model) as client:
        started = start(client, "tell me when AirPods Pro drop below $199")
        [waiting] = wait_for(lambda: pending(client))
        assert waiting["run_id"] == started["id"]
        assert waiting["risk"] == "local_change"
        assert waiting["description"] == (
            'Start watching the price of "AirPods Pro" every 6 hours (alert at or below $199.00).'
        )
        assert client.get("/v1/agent/status").json()["pending_approvals"] == 1

        decided = client.post(f"/v1/agent/approvals/{waiting['id']}", json={"decision": "approve"})
        assert decided.status_code == 200
        data = finished(client, started["id"])

        watches = client.get("/v1/deals/watches").json()

    assert data["status"] == "completed"
    [action] = data["actions"]
    assert action["status"] == "succeeded"
    assert action["permission"] == "approved"
    assert action["decided_at"] is not None
    assert [w["query"] for w in watches] == ["AirPods Pro"]


def test_a_rejected_change_is_not_made() -> None:
    model = FakeProvider("local", script=[[call("watch_price", product="AirPods Pro")], "OK."])
    with make_client(model) as client:
        started = start(client)
        [waiting] = wait_for(lambda: pending(client))
        client.post(
            f"/v1/agent/approvals/{waiting['id']}",
            json={"decision": "reject", "note": "not now"},
        )
        data = finished(client, started["id"])
        watches = client.get("/v1/deals/watches").json()

    assert data["actions"][0]["status"] == "rejected"
    assert data["actions"][0]["decision_note"] == "not now"
    assert watches == []
    assert "declined this (not now)" in model.calls[1][-1].content


def test_an_unanswered_approval_expires() -> None:
    model = FakeProvider("local", script=[[call("watch_price", product="AirPods Pro")], "OK."])
    with make_client(model, agent_approval_timeout_seconds=0.1) as client:
        data = run(client)
        late = client.post(
            f"/v1/agent/approvals/{data['actions'][0]['id']}", json={"decision": "approve"}
        )

    assert data["actions"][0]["status"] == "expired"
    assert late.status_code == 409
    assert client.get("/v1/deals/watches").json() == []


def test_deciding_an_action_that_is_not_waiting_is_refused() -> None:
    with make_client(FakeProvider("local")) as client:
        assert (
            client.post("/v1/agent/approvals/99", json={"decision": "approve"}).status_code == 409
        )


def test_a_change_already_made_moments_ago_is_not_repeated_by_another_run() -> None:
    watch = call("watch_price", product="AirPods Pro")
    model = FakeProvider("local", script=[[watch], "Watching.", [watch], "Already watching."])
    with make_client(model) as client:
        first = start(client)
        [waiting] = wait_for(lambda: pending(client))
        client.post(f"/v1/agent/approvals/{waiting['id']}", json={"decision": "approve"})
        finished(client, first["id"])
        second = run(client)  # would hang on an approval if it weren't recognised

    assert second["actions"][0]["status"] == "duplicate"


# ---- limits, cancelling, and the pause switch ----


def test_a_runaway_loop_is_stopped_by_the_step_limit() -> None:
    looping = [[call("current_time", i)] for i in range(10)]
    model = FakeProvider("local", script=[*looping])
    with make_client(model) as client:
        data = run(client, max_steps=3)

    assert data["status"] == "stopped"
    assert data["error"] == "Reached the limit of 3 steps"
    assert data["steps"] == 3
    assert len(model.calls) == 4  # three steps, then one last turn to answer
    assert "used all the steps" in model.systems[-1]


def test_a_slow_action_times_out() -> None:
    async def slow(_ctx, _params):
        await asyncio.sleep(5)
        return ActionResult("never")

    registry = ActionRegistry(
        [Action("slow", "Slow.", NoParams, Risk.READ_ONLY, slow, timeout=0.05)]
    )
    model = FakeProvider("local", script=[[call("slow")], "It was slow."])
    with make_client(model, actions=registry) as client:
        data = run(client)

    assert data["actions"][0]["status"] == "timed_out"
    assert data["status"] == "completed"


def test_a_whole_run_has_a_time_limit() -> None:
    model = FakeProvider("local", delay=2.0)
    with make_client(model, agent_run_timeout_seconds=0.1) as client:
        data = run(client)

    assert data["status"] == "stopped"
    assert "Ran out of time" in data["error"]


def test_a_run_can_be_cancelled_while_it_waits_for_approval() -> None:
    model = FakeProvider("local", script=[[call("watch_price", product="AirPods Pro")]])
    with make_client(model) as client:
        started = start(client)
        wait_for(lambda: pending(client))
        cancelled = client.post(f"/v1/agent/runs/{started['id']}/cancel").json()
        again = client.post(f"/v1/agent/runs/{started['id']}/cancel")

    assert cancelled["status"] == "cancelled"
    assert cancelled["actions"][0]["status"] == "cancelled"
    assert pending(client) == []
    assert again.status_code == 409


def test_pause_stops_everything_and_survives_a_restart(tmp_path) -> None:
    settings = {"data_dir": tmp_path / "data"}
    model = FakeProvider("local", script=[[call("watch_price", product="AirPods Pro")]])
    with make_client(model, **settings) as client:
        started = start(client)
        wait_for(lambda: pending(client))
        paused = client.post("/v1/agent/pause").json()
        refused = client.post("/v1/agent/runs", json={"goal": "anything"})
        data = client.get(f"/v1/agent/runs/{started['id']}").json()

    assert paused == {"paused": True, "stopped_runs": [started["id"]]}
    assert data["status"] == "cancelled"
    assert data["error"].startswith("Paused")
    assert refused.status_code == 409

    with make_client(FakeProvider("local", script=["Back."]), **settings) as client:
        assert client.get("/v1/agent/status").json()["paused"] is True
        assert client.post("/v1/agent/runs", json={"goal": "anything"}).status_code == 409
        client.post("/v1/agent/resume")
        assert run(client)["status"] == "completed"


def test_runs_left_open_by_a_stopped_server_are_closed_at_startup(tmp_path) -> None:
    db = Database(tmp_path / "data" / "vara.db")
    db.upgrade()
    with db.session() as session:
        session.add(AgentRun(goal="interrupted", max_steps=8, status=RunStatus.RUNNING))
    db.dispose()

    with make_client(FakeProvider("local"), data_dir=tmp_path / "data") as client:
        [stale] = client.get("/v1/agent/runs").json()["runs"]

    assert stale["status"] == "interrupted"


def test_too_many_runs_at_once_are_refused() -> None:
    model = FakeProvider("local", delay=1.0)
    with make_client(model, agent_max_running=1) as client:
        start(client)
        assert client.post("/v1/agent/runs", json={"goal": "another"}).status_code == 429


# ---- privacy, providers, and redaction ----


def test_local_only_mode_still_blocks_cloud_models_in_the_agent() -> None:
    cloud = FakeProvider("cloud", local=False, script=["should never answer"])
    with make_client(cloud, mode=PrivacyMode.LOCAL_ONLY) as client:
        data = run(client)

    assert data["status"] == "failed"
    assert cloud.calls == []


def test_providers_that_cannot_use_tools_are_not_used() -> None:
    plain = FakeProvider("plain", tools=False)
    with make_client(plain) as client:
        data = run(client)

    assert data["status"] == "failed"
    assert plain.calls == []


def test_secrets_in_results_are_redacted_before_the_model_or_the_audit_sees_them() -> None:
    async def leaky(_ctx, _params):
        return ActionResult("config: api_key=supersecretvalue123 and more")

    registry = ActionRegistry([Action("leaky", "Leaks.", NoParams, Risk.READ_ONLY, leaky)])
    model = FakeProvider("local", script=[[call("leaky")], "Done."])
    with make_client(model, actions=registry) as client:
        data = run(client)
        audit = client.get("/v1/agent/actions").json()["actions"]

    assert "supersecretvalue123" not in model.calls[1][-1].content
    assert "supersecretvalue123" not in json.dumps(data)
    assert "supersecretvalue123" not in json.dumps(audit)


def test_the_audit_trail_traces_every_action_to_its_run_and_step() -> None:
    model = FakeProvider(
        "local",
        script=[[call("current_time")], [call("list_price_watches", 2)], "All done."],
    )
    with make_client(model) as client:
        data = run(client)
        audit = client.get("/v1/agent/actions", params={"run_id": data["id"]}).json()["actions"]

    assert [(a["action"], a["step"], a["run_id"]) for a in reversed(audit)] == [
        ("current_time", 1, data["id"]),
        ("list_price_watches", 2, data["id"]),
    ]
    assert all(a["permission"] == "read-only" and a["result"] for a in audit)


def test_progress_is_announced_live() -> None:
    model = FakeProvider("local", script=[[call("current_time")], "Done."])
    with make_client(model) as client:
        bus = client.app.state.events
        with bus.subscribe() as queue:
            run(client)
            events = []
            while not queue.empty():
                events.append(queue.get_nowait()[0])

    assert events[0] == "agent_run_started"
    assert "agent_action" in events
    assert "agent_delta" in events
    assert events[-1] == "agent_run_finished"


def test_every_builtin_action_has_a_valid_tool_schema() -> None:
    for action in default_registry().all():
        spec = action.spec()
        assert spec.parameters["type"] == "object"
        assert "properties" in spec.parameters


# ---- provider formats for tool calls ----


def test_ollama_sends_tools_and_tool_turns_in_its_format() -> None:
    provider = OllamaProvider(default_model="qwen3:14b")
    turns = [
        Message("user", "time?"),
        Message("assistant", "", tool_calls=(call("current_time"),)),
        Message("tool", "4 PM", tool_call_id="call-1", name="current_time"),
    ]
    body = provider.request_body(
        turns, tools=[ToolSpec("current_time", "The time.", {"type": "object", "properties": {}})]
    )

    assert body["tools"][0]["function"]["name"] == "current_time"
    assert body["messages"][1]["tool_calls"] == [
        {"function": {"name": "current_time", "arguments": {}}}
    ]
    assert body["messages"][2] == {"role": "tool", "content": "4 PM", "tool_name": "current_time"}


def test_ollama_streams_tool_calls() -> None:
    lines = [
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "web_search", "arguments": {"query": "news"}}}
                ],
            }
        },
        {"message": {"content": ""}, "done": True, "done_reason": "stop", "model": "qwen3:14b"},
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="\n".join(json.dumps(line) for line in lines))

    client = httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler))
    provider = OllamaProvider(client=client)

    async def drain():
        return [e async for e in provider.stream([Message("user", "news?")], tools=[])]

    events = asyncio.run(drain())
    tool_call = events[0]
    assert isinstance(tool_call, ToolCall)
    assert (tool_call.name, tool_call.arguments) == ("web_search", {"query": "news"})
    assert tool_call.id


def test_claude_gets_tool_results_grouped_in_one_user_turn() -> None:
    turns = [
        Message("user", "two things"),
        Message("assistant", "Checking.", tool_calls=(call("a"), call("b", 2))),
        Message("tool", "A", tool_call_id="call-1", name="a"),
        Message("tool", "B", tool_call_id="call-2", name="b"),
    ]
    converted = _api_messages(turns)

    assert converted[1]["content"][0] == {"type": "text", "text": "Checking."}
    assert [b["id"] for b in converted[1]["content"][1:]] == ["call-1", "call-2"]
    assert converted[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call-1", "content": "A"},
            {"type": "tool_result", "tool_use_id": "call-2", "content": "B"},
        ],
    }
    assert len(converted) == 3


@pytest.mark.parametrize("risk", list(Risk))
def test_only_read_only_actions_skip_approval(risk: Risk) -> None:
    assert risk.needs_approval is (risk is not Risk.READ_ONLY)
