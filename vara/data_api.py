from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from vara import retention, store, suggest
from vara.api import ROUTING_ERRORS, http_error
from vara.db.database import Database
from vara.db.models import (
    Conversation,
    Memory,
    MemoryCategory,
    MemorySource,
    MemoryStatus,
    StoredMessage,
)
from vara.router import ModelRouter

data = APIRouter(prefix="/v1")


def _db(request: Request) -> Database:
    return request.app.state.db


# Conversations


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@data.get("/conversations")
def list_conversations(request: Request) -> list[dict]:
    with _db(request).session() as session:
        rows = session.scalars(select(Conversation).order_by(Conversation.updated_at.desc()))
        return [store.conversation_dict(c) for c in rows]


@data.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int, request: Request) -> dict:
    with _db(request).session() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        return store.conversation_dict(conversation, with_messages=True)


@data.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: int, body: ConversationUpdate, request: Request) -> dict:
    with _db(request).session() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        conversation.title = body.title
        session.flush()
        return store.conversation_dict(conversation)


@data.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: int, request: Request) -> Response:
    with _db(request).session() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        session.delete(conversation)
    return Response(status_code=204)


# Memories


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    category: MemoryCategory = MemoryCategory.OTHER
    # A memory you add yourself is confirmed. Anything else is proposed and needs approval.
    status: MemoryStatus = MemoryStatus.CONFIRMED
    pinned: bool = False
    source_message_id: int | None = None


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    category: MemoryCategory | None = None
    status: MemoryStatus | None = None
    pinned: bool | None = None


@data.get("/memories")
def list_memories(request: Request, status: MemoryStatus | None = None) -> list[dict]:
    with _db(request).session() as session:
        query = select(Memory).order_by(Memory.id)
        if status is not None:
            query = query.where(Memory.status == status)
        return [store.memory_dict(m) for m in session.scalars(query)]


@data.post("/memories", status_code=201)
def create_memory(body: MemoryCreate, request: Request) -> dict:
    with _db(request).session() as session:
        if body.source_message_id is not None and not session.get(
            StoredMessage, body.source_message_id
        ):
            raise HTTPException(422, "source_message_id does not exist")
        memory = Memory(
            content=body.content.strip(),
            category=body.category,
            status=body.status,
            pinned=body.pinned,
            source=MemorySource.CONVERSATION if body.source_message_id else MemorySource.USER,
            source_message_id=body.source_message_id,
        )
        session.add(memory)
        session.flush()
        return store.memory_dict(memory)


@data.patch("/memories/{memory_id}")
def update_memory(memory_id: int, body: MemoryUpdate, request: Request) -> dict:
    """Correct a memory, approve a suggestion (status=confirmed), or pin it so no retention
    rule ever deletes it."""
    with _db(request).session() as session:
        memory = session.get(Memory, memory_id)
        if memory is None:
            raise HTTPException(404, "Memory not found")
        if body.content is not None:
            memory.content = body.content.strip()
        if body.category is not None:
            memory.category = body.category
        if body.status is not None:
            memory.status = body.status
        if body.pinned is not None:
            memory.pinned = body.pinned
        session.flush()
        return store.memory_dict(memory)


@data.delete("/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: int, request: Request) -> Response:
    with _db(request).session() as session:
        memory = session.get(Memory, memory_id)
        if memory is None:
            raise HTTPException(404, "Memory not found")
        session.delete(memory)
    return Response(status_code=204)


# Search and export


@data.get("/search")
def search(request: Request, q: str = Query(min_length=1), limit: int = 20) -> dict:
    with _db(request).session() as session:
        return store.search(session, q, limit=min(limit, 100))


@data.get("/export")
def export(request: Request) -> dict:
    with _db(request).session() as session:
        return store.export_all(session)


# Suggested memories


class SuggestRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    confirm_cloud: bool = False


@data.post("/conversations/{conversation_id}/suggest-memories")
async def suggest_memories(conversation_id: int, body: SuggestRequest, request: Request) -> dict:
    """Ask a model for facts worth remembering from your messages in this conversation.

    They are saved as `proposed`: never used in requests until you approve them, and deleted by
    the `proposed` retention rule if left unapproved. The privacy mode applies as for chat.
    """
    db = _db(request)
    with db.session() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        user_messages = [m for m in conversation.messages if m.role == "user"]
    if not user_messages:
        return {"proposed": [], "skipped": 0, "rejected": 0, "provider": None, "model": None}

    router: ModelRouter = request.app.state.router
    try:
        raw, provider, model = await suggest.collect_text(
            router.stream(
                suggest.extraction_request(user_messages),
                provider=body.provider,
                model=body.model,
                system=suggest.EXTRACTION_PROMPT,
                cloud_confirmed=body.confirm_cloud,
            )
        )
    except ROUTING_ERRORS as exc:
        raise http_error(exc) from exc
    suggestions, rejected = suggest.parse_suggestions(raw, user_messages)

    proposed, skipped = [], 0
    with db.session() as session:
        known = {c.lower() for c in session.scalars(select(func.lower(Memory.content)))}
        for item in suggestions:
            if item.content.lower() in known:
                skipped += 1
                continue
            known.add(item.content.lower())
            memory = Memory(
                content=item.content,
                category=item.category,
                status=MemoryStatus.PROPOSED,
                source=MemorySource.CONVERSATION,
                source_message_id=item.source_message_id,
            )
            session.add(memory)
            session.flush()
            proposed.append(store.memory_dict(memory))
    return {
        "proposed": proposed,
        "skipped": skipped,
        "rejected": rejected,
        "provider": provider,
        "model": model,
    }


# Retention


class RetentionUpdate(BaseModel):
    keep_days: int | None = Field(ge=1, le=36500)  # null keeps until you delete


@data.get("/retention")
def retention_overview(request: Request) -> list[dict]:
    """One rule per kind of data, ordered by how much of each kind you have saved."""
    with _db(request).session() as session:
        return retention.overview(session)


@data.put("/retention/{key}")
def update_retention(key: str, body: RetentionUpdate, request: Request) -> list[dict]:
    with _db(request).session() as session:
        try:
            retention.set_rule(session, key, body.keep_days)
        except KeyError:
            raise HTTPException(404, f"No retention rule named '{key}'") from None
        session.flush()
        return retention.overview(session)


@data.get("/retention/due")
def retention_due(request: Request) -> dict:
    """What the current rules would delete. Pin a memory to keep it."""
    with _db(request).session() as session:
        found = retention.expired(session)
        return {
            "conversations": [store.conversation_dict(c) for c in found["conversations"]],
            "memories": [store.memory_dict(m) for m in found["memories"]],
        }


@data.post("/retention/apply")
def retention_apply(request: Request) -> dict:
    with _db(request).session() as session:
        return {"deleted": retention.apply(session)}
