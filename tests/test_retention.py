import json
from datetime import timedelta

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text, update

from vara.config import Settings
from vara.db.database import Database, _alembic_config
from vara.db.models import Conversation, Memory, utcnow
from vara.main import create_app
from vara.providers.fake import FakeProvider
from vara.router import ModelRouter, PrivacyMode
from vara.suggest import parse_suggestions


def make_client(provider: FakeProvider | None = None, mode=PrivacyMode.LOCAL_ONLY) -> TestClient:
    router = ModelRouter([provider or FakeProvider("local")], mode)
    return TestClient(create_app(Settings(), router=router))


def say(client: TestClient, text_: str) -> dict:
    response = client.post("/v1/chat", json={"messages": [{"role": "user", "content": text_}]})
    assert response.status_code == 200, response.text
    return response.json()


def age(client: TestClient, model, days: int) -> None:
    """Make every row of `model` look `days` old."""
    old = utcnow() - timedelta(days=days)
    with client.app.state.db.session() as session:
        values = {"updated_at": old}
        if model is Memory:
            values["created_at"] = old
        session.execute(update(model).values(**values))


EXTRACTED = json.dumps(
    {
        "facts": [
            {"content": "Prefers tea to coffee", "category": "preference", "message": 1},
            {"content": "Dentist appointment on 14 March", "category": "commitment", "message": 2},
            {"content": "Has a cat", "category": "made-up-category", "message": 9},
            {"content": "Plays the violin", "category": "other", "message": 1},  # invented
            {"content": "API key is sk-ant-api03-" + "x" * 30, "category": "other", "message": 1},
            {"content": "", "category": "other"},
        ]
    }
)


def test_suggestions_are_saved_as_proposed_with_category_and_provenance() -> None:
    extractor = FakeProvider("local", reply=EXTRACTED)
    client = make_client(extractor)
    conversation_id = say(client, "I like tea more than coffee, and I have a cat")[
        "conversation_id"
    ]
    client.post(
        "/v1/chat",
        json={
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": "My dentist appointment is on 14 March"}],
        },
    )
    with client.app.state.db.session() as session:
        conversation = session.get(Conversation, conversation_id)
        user_ids = [m.id for m in conversation.messages if m.role == "user"]

    result = client.post(f"/v1/conversations/{conversation_id}/suggest-memories", json={})

    assert result.status_code == 200
    body = result.json()
    assert [(m["content"], m["category"], m["status"]) for m in body["proposed"]] == [
        ("Prefers tea to coffee", "preference", "proposed"),
        ("Dentist appointment on 14 March", "commitment", "proposed"),
        ("Has a cat", "other", "proposed"),
    ]
    assert [m["source_message_id"] for m in body["proposed"]] == [user_ids[0], user_ids[1], None]
    assert body["rejected"] == 3  # the invented fact, the credential, and the empty one
    # Only the user's messages are sent for extraction, not the assistant's replies.
    assert '"facts"' not in extractor.calls[-1][0].content


def test_suggestions_skip_duplicates_of_existing_memories() -> None:
    client = make_client(FakeProvider("local", reply=EXTRACTED))
    client.post("/v1/memories", json={"content": "prefers tea to coffee"})
    conversation_id = say(client, "I like tea more than coffee")["conversation_id"]

    body = client.post(f"/v1/conversations/{conversation_id}/suggest-memories", json={}).json()

    assert body["skipped"] == 1
    assert "Prefers tea to coffee" not in [m["content"] for m in body["proposed"]]


def test_suggestions_respect_privacy_mode() -> None:
    cloud = FakeProvider("cloud", local=False, reply=EXTRACTED)
    client = make_client(cloud, mode=PrivacyMode.ASK)
    with client.app.state.db.session() as session:
        session.add(Conversation(title="t"))
    # Seed a user message directly; chatting would itself need confirmation.
    with client.app.state.db.session() as session:
        session.execute(
            text(
                "INSERT INTO messages (conversation_id, role, content, created_at) "
                "VALUES (1, 'user', 'I like tea', CURRENT_TIMESTAMP)"
            )
        )

    blocked = client.post("/v1/conversations/1/suggest-memories", json={})
    allowed = client.post("/v1/conversations/1/suggest-memories", json={"confirm_cloud": True})

    assert blocked.status_code == 409
    assert allowed.status_code == 200


def test_unparseable_model_output_proposes_nothing() -> None:
    assert parse_suggestions("Sorry, I can't help with that.", []) == ([], 0)
    assert parse_suggestions('{"facts": "nope"}', []) == ([], 0)


def test_retention_list_puts_the_categories_you_use_first() -> None:
    client = make_client()
    for content, category in [
        ("Works as a nurse", "work"),
        ("Works night shifts", "work"),
        ("Allergic to peanuts", "health"),
    ]:
        client.post("/v1/memories", json={"content": content, "category": category})

    rows = client.get("/v1/retention").json()

    assert [r["key"] for r in rows[:4]] == ["conversations", "proposed", "work", "health"]
    work = rows[2]
    assert (work["confirmed"], work["keep_days"], work["sensitive"]) == (2, None, False)
    assert rows[3]["sensitive"] is True


def test_defaults_keep_everything_except_unapproved_suggestions() -> None:
    client = make_client()
    say(client, "old conversation")
    client.post("/v1/memories", json={"content": "Call mum on Sunday", "category": "commitment"})
    client.post("/v1/memories", json={"content": "Maybe likes jazz", "status": "proposed"})
    age(client, Memory, 400)
    age(client, Conversation, 400)

    due = client.get("/v1/retention/due").json()

    assert due["conversations"] == []
    assert [m["content"] for m in due["memories"]] == ["Maybe likes jazz"]
    commitment = next(r for r in client.get("/v1/retention").json() if r["key"] == "commitment")
    assert commitment["keep_days"] is None


def test_pinned_memories_outlive_a_time_limit() -> None:
    client = make_client()
    keep = client.post(
        "/v1/memories", json={"content": "Passport renewal due 2031", "category": "commitment"}
    ).json()
    client.post(
        "/v1/memories", json={"content": "Dinner with Jo on Friday", "category": "commitment"}
    )
    client.put("/v1/retention/commitment", json={"keep_days": 90})
    age(client, Memory, 120)

    due = [m["content"] for m in client.get("/v1/retention/due").json()["memories"]]
    assert sorted(due) == ["Dinner with Jo on Friday", "Passport renewal due 2031"]

    client.patch(f"/v1/memories/{keep['id']}", json={"pinned": True})
    age(client, Memory, 120)  # pinning edits the memory, so age it again

    assert client.post("/v1/retention/apply").json() == {
        "deleted": {"conversations": 0, "memories": 1}
    }
    remaining = client.get("/v1/memories").json()
    assert [(m["content"], m["pinned"]) for m in remaining] == [("Passport renewal due 2031", True)]


def test_conversation_rule_deletes_old_conversations() -> None:
    client = make_client()
    say(client, "ancient history")
    client.put("/v1/retention/conversations", json={"keep_days": 30})
    age(client, Conversation, 31)

    assert client.post("/v1/retention/apply").json()["deleted"]["conversations"] == 1
    assert client.get("/v1/conversations").json() == []
    assert client.get("/v1/search?q=ancient").json()["messages"] == []


def test_rules_are_applied_on_startup() -> None:
    client = make_client()
    client.post("/v1/memories", json={"content": "Maybe likes jazz", "status": "proposed"})
    age(client, Memory, 31)

    assert make_client().get("/v1/memories").json() == []


def test_rule_back_to_keep_forever_and_unknown_rule() -> None:
    client = make_client()

    client.put("/v1/retention/proposed", json={"keep_days": None})
    proposed = next(r for r in client.get("/v1/retention").json() if r["key"] == "proposed")

    assert proposed["keep_days"] is None
    assert client.put("/v1/retention/nope", json={"keep_days": 5}).status_code == 404
    assert client.put("/v1/retention/work", json={"keep_days": 0}).status_code == 422


def test_upgrade_from_first_schema_keeps_data_and_search(isolated_data_dir) -> None:
    db = Database(isolated_data_dir / "vara.db")
    config = _alembic_config(db.url)
    with db.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO memories (content, status, source, created_at, updated_at) "
                "VALUES ('Lives near the harbour', 'confirmed', 'user', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    db.dispose()

    client = make_client()  # applies the remaining migrations

    memory = client.get("/v1/memories").json()[0]
    assert (memory["content"], memory["category"], memory["pinned"]) == (
        "Lives near the harbour",
        "other",
        False,
    )
    assert client.get("/v1/search?q=harbour").json()["memories"][0]["id"] == memory["id"]
    client.patch(f"/v1/memories/{memory['id']}", json={"content": "Lives near the lighthouse"})
    assert client.get("/v1/search?q=lighthouse").json()["memories"][0]["id"] == memory["id"]
    assert client.get("/v1/search?q=harbour").json()["memories"] == []
