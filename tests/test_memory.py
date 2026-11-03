import sqlite3

import pytest
from fastapi.testclient import TestClient

from vara.backup import backup_database, restore_database
from vara.config import Settings
from vara.main import create_app
from vara.providers.fake import FakeProvider
from vara.router import ModelRouter, PrivacyMode
from vara.store import fts_query


def make_client(provider: FakeProvider | None = None) -> TestClient:
    router = ModelRouter([provider or FakeProvider("local")], PrivacyMode.LOCAL_ONLY)
    return TestClient(create_app(Settings(), router=router))


def say(client: TestClient, text: str, **extra) -> dict:
    response = client.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": text}], **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_conversations_survive_a_restart() -> None:
    first = say(make_client(), "Plan my garden layout")

    restarted = make_client()
    conversations = restarted.get("/v1/conversations").json()

    assert [c["title"] for c in conversations] == ["Plan my garden layout"]
    detail = restarted.get(f"/v1/conversations/{first['conversation_id']}").json()
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [
        ("user", "Plan my garden layout"),
        ("assistant", "echo: Plan my garden layout "),
    ]
    assert detail["messages"][1]["provider"] == "local"


def test_continuing_a_conversation_sends_stored_history() -> None:
    provider = FakeProvider("local")
    client = make_client(provider)

    first = say(client, "first question")
    say(client, "second question", conversation_id=first["conversation_id"])

    sent = [(m.role, m.content) for m in provider.calls[-1]]
    assert sent == [
        ("user", "first question"),
        ("assistant", "echo: first question "),
        ("user", "second question"),
    ]


def test_unknown_conversation_is_404() -> None:
    client = make_client()

    response = client.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}], "conversation_id": 99}
    )

    assert response.status_code == 404


def test_last_message_must_be_from_user() -> None:
    response = make_client().post(
        "/v1/chat", json={"messages": [{"role": "assistant", "content": "hi"}]}
    )

    assert response.status_code == 422


def test_relevant_confirmed_memories_are_used_and_reported() -> None:
    client = make_client()
    coffee = client.post("/v1/memories", json={"content": "I drink my coffee black"}).json()
    client.post("/v1/memories", json={"content": "My dog is called Biscuit"})

    preview = client.post(
        "/v1/chat/preview",
        json={"messages": [{"role": "user", "content": "How should I order coffee?"}]},
    ).json()
    answer = say(client, "How should I order coffee?")

    assert preview["memories_used"] == [{"id": coffee["id"], "content": "I drink my coffee black"}]
    assert "I drink my coffee black" in preview["candidates"][0]["request"]["system"]
    assert "Biscuit" not in preview["candidates"][0]["request"]["system"]
    assert answer["memories_used"] == preview["memories_used"]


def test_proposed_memories_are_not_used_until_approved() -> None:
    client = make_client()
    proposed = client.post(
        "/v1/memories", json={"content": "Prefers window seats", "status": "proposed"}
    ).json()

    assert say(client, "Book me seats on the flight")["memories_used"] == []
    assert [m["id"] for m in client.get("/v1/memories?status=proposed").json()] == [proposed["id"]]

    client.patch(f"/v1/memories/{proposed['id']}", json={"status": "confirmed"})

    assert say(client, "Book me seats on the flight")["memories_used"][0]["id"] == proposed["id"]


def test_use_memory_false_sends_no_memories() -> None:
    client = make_client()
    client.post("/v1/memories", json={"content": "I drink my coffee black"})

    assert say(client, "coffee ideas?", use_memory=False)["memories_used"] == []


def test_memory_can_be_corrected_and_deleted() -> None:
    client = make_client()
    memory = client.post("/v1/memories", json={"content": "My sister is Ana"}).json()

    corrected = client.patch(f"/v1/memories/{memory['id']}", json={"content": "My sister is Anna"})
    assert corrected.json()["content"] == "My sister is Anna"
    assert client.get("/v1/search?q=Anna").json()["memories"][0]["id"] == memory["id"]
    assert client.get("/v1/search?q=Ana").json()["memories"] == []

    assert client.delete(f"/v1/memories/{memory['id']}").status_code == 204
    assert client.get("/v1/memories").json() == []
    assert client.get("/v1/search?q=Anna").json()["memories"] == []


def test_memory_records_provenance_from_a_message() -> None:
    client = make_client()
    answer = say(client, "Remember that I am allergic to peanuts")

    memory = client.post(
        "/v1/memories",
        json={
            "content": "Allergic to peanuts",
            "status": "proposed",
            "source_message_id": answer["message_id"],
        },
    ).json()

    assert memory["source"] == "conversation"
    assert memory["source_message_id"] == answer["message_id"]
    bad = client.post("/v1/memories", json={"content": "x", "source_message_id": 999})
    assert bad.status_code == 422


def test_search_finds_messages_and_deleting_a_conversation_removes_them() -> None:
    client = make_client()
    answer = say(client, "Notes about the quarterly budget review")

    hits = client.get("/v1/search?q=budget").json()["messages"]
    assert {h["conversation_id"] for h in hits} == {answer["conversation_id"]}
    assert "[budget]" in hits[0]["snippet"]

    assert client.delete(f"/v1/conversations/{answer['conversation_id']}").status_code == 204
    assert client.get("/v1/search?q=budget").json()["messages"] == []
    assert client.get(f"/v1/conversations/{answer['conversation_id']}").status_code == 404


def test_rename_conversation() -> None:
    client = make_client()
    conversation_id = say(client, "hello there")["conversation_id"]

    renamed = client.patch(f"/v1/conversations/{conversation_id}", json={"title": "Greetings"})

    assert renamed.json()["title"] == "Greetings"


def test_streamed_reply_is_stored() -> None:
    client = make_client(FakeProvider("local", reply="streamed answer"))

    response = client.post(
        "/v1/chat/stream", json={"messages": [{"role": "user", "content": "go"}]}
    )
    assert response.status_code == 200
    assert "event: done" in response.text

    messages = client.get("/v1/conversations/1").json()["messages"]
    assert [m["content"] for m in messages] == ["go", "streamed answer "]


def test_failed_request_stores_nothing() -> None:
    client = make_client(FakeProvider("local", fail=True))

    assert (
        client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}).status_code
        == 503
    )
    assert client.get("/v1/conversations").json() == []


def test_usage_survives_a_restart() -> None:
    cloud = FakeProvider("anthropic", local=False, usage=(1000, 500))
    cloud.default_model = "claude-opus-5-5"
    router = ModelRouter([cloud], PrivacyMode.ALLOWED)
    TestClient(create_app(Settings(), router=router)).post(
        "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}
    )

    usage = make_client().get("/v1/usage").json()

    assert usage["totals"][0]["requests"] == 1
    assert usage["total_cost_usd"] == pytest.approx(0.014)  # 1000 * $4/M + 500 * $20/M


def test_export_contains_conversations_and_memories() -> None:
    client = make_client()
    say(client, "export me")
    client.post("/v1/memories", json={"content": "Lives in Lisbon"})

    exported = client.get("/v1/export").json()

    assert exported["format"] == "vara-export"
    assert exported["conversations"][0]["messages"][0]["content"] == "export me"
    assert exported["memories"][0]["content"] == "Lives in Lisbon"


def test_backup_restores_to_a_working_copy(isolated_data_dir, tmp_path) -> None:
    client = make_client()
    say(client, "keep this safe")
    client.post("/v1/memories", json={"content": "Backup test fact"})
    backup = tmp_path / "backups" / "vara.db"

    backup_database(isolated_data_dir / "vara.db", backup)
    with pytest.raises(FileExistsError):
        backup_database(isolated_data_dir / "vara.db", backup)

    copy_dir = tmp_path / "restored"
    restore_database(backup, copy_dir / "vara.db")
    restored = TestClient(
        create_app(
            Settings(data_dir=copy_dir),
            router=ModelRouter([FakeProvider()], PrivacyMode.LOCAL_ONLY),
        )
    )
    assert restored.get("/v1/conversations").json()[0]["title"] == "keep this safe"
    assert (
        restored.get("/v1/search?q=backup").json()["memories"][0]["content"] == "Backup test fact"
    )


def test_deleted_text_is_not_left_in_the_database_file(isolated_data_dir) -> None:
    client = make_client()
    memory = client.post("/v1/memories", json={"content": "zebracornflake secret"}).json()
    client.delete(f"/v1/memories/{memory['id']}")
    client.app.state.db.dispose()

    with sqlite3.connect(isolated_data_dir / "vara.db") as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert b"zebracornflake" not in (isolated_data_dir / "vara.db").read_bytes()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("How should I order coffee?", '"order" OR "coffee"'),
        ('quote" OR injection', '"quote" OR "injection"'),
        ("the and is", None),
    ],
)
def test_fts_query_is_safe(text: str, expected: str | None) -> None:
    assert fts_query(text) == expected
