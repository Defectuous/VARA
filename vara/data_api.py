from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from vara import store
from vara.db.database import Database
from vara.db.models import Conversation, Memory, MemorySource, MemoryStatus, StoredMessage

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
    # A memory you add yourself is confirmed. Anything else is proposed and needs approval.
    status: MemoryStatus = MemoryStatus.CONFIRMED
    source_message_id: int | None = None


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    status: MemoryStatus | None = None


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
            status=body.status,
            source=MemorySource.CONVERSATION if body.source_message_id else MemorySource.USER,
            source_message_id=body.source_message_id,
        )
        session.add(memory)
        session.flush()
        return store.memory_dict(memory)


@data.patch("/memories/{memory_id}")
def update_memory(memory_id: int, body: MemoryUpdate, request: Request) -> dict:
    """Correct a memory's text, or approve a proposed memory by setting status=confirmed."""
    with _db(request).session() as session:
        memory = session.get(Memory, memory_id)
        if memory is None:
            raise HTTPException(404, "Memory not found")
        if body.content is not None:
            memory.content = body.content.strip()
        if body.status is not None:
            memory.status = body.status
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
