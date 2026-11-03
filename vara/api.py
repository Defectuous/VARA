import json
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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
    messages: list[ChatMessage] = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    system: str | None = None
    confirm_cloud: bool = False

    def to_messages(self) -> list[Message]:
        return [Message(m.role, m.content) for m in self.messages]


class Usage(BaseModel):
    input_tokens: int | None
    output_tokens: int | None


class ChatResponse(BaseModel):
    provider: str
    model: str
    text: str
    stop_reason: str | None
    usage: Usage
    cost_usd: float | None


def _router(request: Request) -> ModelRouter:
    return request.app.state.router


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PrivacyBlocked):
        return HTTPException(403, str(exc))
    if isinstance(exc, ConfirmationRequired):
        return HTTPException(409, f"{exc}; resend with confirm_cloud=true")
    if isinstance(exc, NoProviderAvailable):
        return HTTPException(503, {"message": str(exc), "errors": exc.errors})
    return HTTPException(502, str(exc))


_ROUTING_ERRORS = (PrivacyBlocked, ConfirmationRequired, NoProviderAvailable, ProviderError)


async def _open_stream(request: Request, body: ChatRequest) -> AsyncIterator[RoutedEvent]:
    """Start routing and wait for the first event, so privacy and provider failures become
    ordinary HTTP errors instead of breaking a stream that has already started."""
    events = _router(request).stream(
        body.to_messages(),
        provider=body.provider,
        model=body.model,
        system=body.system,
        cloud_confirmed=body.confirm_cloud,
    )
    try:
        first = await anext(events)
    except _ROUTING_ERRORS as exc:
        raise _http_error(exc) from exc

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
    rows = _router(request).usage.summary()
    return {"since_start": rows, "total_cost_usd": sum(row["cost_usd"] for row in rows)}


@v1.post("/chat/preview")
def chat_preview(body: ChatRequest, request: Request) -> dict:
    """Show exactly what would be sent, and to which providers, without calling any model."""
    router = _router(request)
    try:
        candidates = router.preview(
            body.to_messages(),
            provider=body.provider,
            model=body.model,
            system=body.system,
            cloud_confirmed=body.confirm_cloud,
        )
    except _ROUTING_ERRORS as exc:
        raise _http_error(exc) from exc
    return {"privacy_mode": router.privacy_mode, "candidates": candidates}


@v1.post("/chat")
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    parts: list[str] = []
    summary: dict | None = None
    try:
        async for routed in await _open_stream(request, body):
            if isinstance(routed.event, TextDelta):
                parts.append(routed.event.text)
            else:
                summary = _summary(routed, routed.event)
    except _ROUTING_ERRORS as exc:
        raise _http_error(exc) from exc

    if summary is None:
        raise HTTPException(502, "The provider ended without completing")
    return ChatResponse(text="".join(parts), **summary)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@v1.post("/chat/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    """Server-sent events: `delta` per text chunk, then `done` with usage, or `error`."""
    events = await _open_stream(request, body)

    async def sse() -> AsyncIterator[str]:
        try:
            async for routed in events:
                if isinstance(routed.event, TextDelta):
                    yield _sse("delta", {"provider": routed.provider, "text": routed.event.text})
                else:
                    yield _sse("done", _summary(routed, routed.event))
        except _ROUTING_ERRORS as exc:
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )
