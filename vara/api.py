import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from vara import store
from vara.db.database import Database
from vara.db.models import Conversation
from vara.providers.base import Completed, Message, ProviderError, TextDelta
from vara.router import (
    ConfirmationRequired,
    ModelRouter,
    NoProviderAvailable,
    PrivacyBlocked,
    RoutedEvent,
)

v1 = APIRouter(prefix="/v1")


@v1.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    # New messages only. With conversation_id they are appended to that conversation's
    # stored history; without it a new conversation is started.
    messages: list[ChatMessage] = Field(min_length=1)
    conversation_id: int | None = None
    provider: str | None = None
    model: str | None = None
    system: str | None = None
    confirm_cloud: bool = False
    use_memory: bool = True

    @field_validator("messages")
    @classmethod
    def _ends_with_user(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        if messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return messages


class Usage(BaseModel):
    input_tokens: int | None
    output_tokens: int | None


class MemoryUsed(BaseModel):
    id: int
    content: str


class ChatResponse(BaseModel):
    conversation_id: int
    message_id: int
    provider: str
    model: str
    text: str
    stop_reason: str | None
    usage: Usage
    cost_usd: float | None
    memories_used: list[MemoryUsed]


def _router(request: Request) -> ModelRouter:
    return request.app.state.router


def _db(request: Request) -> Database:
    return request.app.state.db


def http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PrivacyBlocked):
        return HTTPException(403, str(exc))
    if isinstance(exc, ConfirmationRequired):
        return HTTPException(409, f"{exc}; resend with confirm_cloud=true")
    if isinstance(exc, NoProviderAvailable):
        return HTTPException(503, {"message": str(exc), "errors": exc.errors})
    return HTTPException(502, str(exc))


ROUTING_ERRORS = (PrivacyBlocked, ConfirmationRequired, NoProviderAvailable, ProviderError)


@dataclass
class _Prepared:
    body: ChatRequest
    new_messages: list[Message]
    messages: list[Message]  # stored history followed by the new messages
    system: str | None
    memories_used: list[dict]


def _prepare(request: Request, body: ChatRequest) -> _Prepared:
    new_messages = [Message(m.role, m.content) for m in body.messages]
    with _db(request).session() as session:
        history: list[Message] = []
        if body.conversation_id is not None:
            conversation = session.get(Conversation, body.conversation_id)
            if conversation is None:
                raise HTTPException(404, "Conversation not found")
            history = store.history(conversation)
        memories = (
            store.retrieve_memories(session, new_messages[-1].content) if body.use_memory else []
        )
        return _Prepared(
            body=body,
            new_messages=new_messages,
            messages=history + new_messages,
            system=store.memory_prompt(body.system, memories),
            memories_used=[{"id": m.id, "content": m.content} for m in memories],
        )


def _persist(request: Request, prepared: _Prepared, reply: str, summary: dict) -> dict:
    with _db(request).session() as session:
        if prepared.body.conversation_id is None:
            conversation = Conversation(title=store.title_from(prepared.new_messages[0].content))
        else:
            conversation = session.get(Conversation, prepared.body.conversation_id)
            if conversation is None:  # deleted while the reply was being generated
                raise HTTPException(409, "Conversation was deleted")
        stored = store.save_exchange(session, conversation, prepared.new_messages, reply, summary)
        return {
            "conversation_id": conversation.id,
            "message_id": stored.id,
            **summary,
            "memories_used": prepared.memories_used,
        }


async def _open_stream(request: Request, prepared: _Prepared) -> AsyncIterator[RoutedEvent]:
    """Start routing and wait for the first event, so privacy and provider failures become
    ordinary HTTP errors instead of breaking a stream that has already started."""
    events = _router(request).stream(
        prepared.messages,
        provider=prepared.body.provider,
        model=prepared.body.model,
        system=prepared.system,
        cloud_confirmed=prepared.body.confirm_cloud,
    )
    try:
        first = await anext(events)
    except ROUTING_ERRORS as exc:
        raise http_error(exc) from exc

    async def chained() -> AsyncIterator[RoutedEvent]:
        yield first
        async for routed in events:
            yield routed

    return chained()


def _summary(routed: RoutedEvent, done: Completed) -> dict:
    return {
        "provider": routed.provider,
        "model": done.model,
        "stop_reason": done.stop_reason,
        "usage": {"input_tokens": done.input_tokens, "output_tokens": done.output_tokens},
        "cost_usd": routed.cost_usd,
    }


@v1.get("/providers")
def list_providers(request: Request) -> dict:
    router = _router(request)
    return {
        "privacy_mode": router.privacy_mode,
        "providers": [
            {"name": p.name, "default_model": p.default_model, "local": p.capabilities.local}
            for p in router.providers
        ],
    }


@v1.get("/usage")
def usage(request: Request) -> dict:
    with _db(request).session() as session:
        rows = store.usage_summary(session)
    return {"totals": rows, "total_cost_usd": sum(row["cost_usd"] for row in rows)}


@v1.post("/chat/preview")
def chat_preview(body: ChatRequest, request: Request) -> dict:
    """Show exactly what would be sent, and to which providers, without calling any model."""
    prepared = _prepare(request, body)
    router = _router(request)
    try:
        candidates = router.preview(
            prepared.messages,
            provider=body.provider,
            model=body.model,
            system=prepared.system,
            cloud_confirmed=body.confirm_cloud,
        )
    except ROUTING_ERRORS as exc:
        raise http_error(exc) from exc
    return {
        "privacy_mode": router.privacy_mode,
        "memories_used": prepared.memories_used,
        "candidates": candidates,
    }


@v1.post("/chat")
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    prepared = _prepare(request, body)
    parts: list[str] = []
    summary: dict | None = None
    try:
        async for routed in await _open_stream(request, prepared):
            if isinstance(routed.event, TextDelta):
                parts.append(routed.event.text)
            else:
                summary = _summary(routed, routed.event)
    except ROUTING_ERRORS as exc:
        raise http_error(exc) from exc

    if summary is None:
        raise HTTPException(502, "The provider ended without completing")
    text = "".join(parts)
    return ChatResponse(text=text, **_persist(request, prepared, text, summary))


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@v1.post("/chat/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    """Server-sent events: `delta` per text chunk, then `done` with usage, or `error`.

    The exchange is stored only once the reply completes; a failed stream stores nothing.
    """
    prepared = _prepare(request, body)
    events = await _open_stream(request, prepared)

    async def sse() -> AsyncIterator[str]:
        parts: list[str] = []
        try:
            async for routed in events:
                if isinstance(routed.event, TextDelta):
                    parts.append(routed.event.text)
                    yield _sse("delta", {"provider": routed.provider, "text": routed.event.text})
                else:
                    saved = _persist(
                        request, prepared, "".join(parts), _summary(routed, routed.event)
                    )
                    yield _sse("done", saved)
        except ROUTING_ERRORS as exc:
            yield _sse("error", {"message": str(exc)})
        except HTTPException as exc:
            yield _sse("error", {"message": str(exc.detail)})

    return StreamingResponse(
        sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )
