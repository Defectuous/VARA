import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from vara import price_watch, store
from vara.db.database import Database
from vara.db.models import Conversation, utcnow
from vara.deals import deal_context
from vara.deals import parse_request as parse_deal_request
from vara.holding import holding_line
from vara.markets import find_symbols, market_context
from vara.providers.base import Completed, Message, ProviderError, TextDelta
from vara.reasoning import ReasoningMode, should_reason
from vara.router import (
    ConfirmationRequired,
    ModelRouter,
    NoProviderAvailable,
    PrivacyBlocked,
    RoutedEvent,
)
from vara.search import planner
from vara.search.client import SearchFailed, search_context

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
    # The reply will be read aloud (voice clients): ask for a short, plain-text answer.
    spoken: bool = False
    # The asking device's clock (ISO 8601 with its UTC offset) and time-zone name. V.A.R.A. tells the
    # model the time where you are, so a phone in another time zone gets its own local time. Without
    # them, this PC's clock is used.
    client_time: datetime | None = None
    client_timezone: str | None = Field(default=None, max_length=64)
    # Let V.A.R.A. search the web when the question needs current information (VARA_WEB_SEARCH=auto).
    web_search: bool = True
    # Whether the model reasons before answering: "auto" decides per message (fast for small talk,
    # reasoning for hard questions), "on"/"off" force it. Unset uses VARA_REASONING.
    reasoning: ReasoningMode | None = None
    # An id the sending client picks for itself. Live events (/v1/events) carry it, so a chat page can
    # skip the exchanges it sent itself and show everyone else's (the voice listener's, other tabs').
    client: str | None = Field(default=None, max_length=64)

    @field_validator("messages")
    @classmethod
    def _ends_with_user(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        if messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return messages

    @field_validator("client_time")
    @classmethod
    def _has_offset(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError(
                "client_time must include its UTC offset, e.g. 2026-11-05T07:04:00-05:00"
            )
        return value


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
    web_search: dict | None = None
    market_data: list[dict] | None = None
    deals: dict | None = None
    price_alerts: list[dict] | None = None
    reasoning: bool | None = None


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
    think: bool = False
    web_search: dict | None = None
    market_data: list[dict] | None = None
    deals: dict | None = None
    price_alerts: list[dict] | None = None


SPOKEN_REPLY = (
    "Your reply will be spoken aloud. Answer in one to three short sentences of plain text, "
    "with no lists, headings, or formatting."
)


def fill_clock(template: str, now: datetime | None = None, timezone_name: str | None = None) -> str:
    """Fill {date}, {time}, and {timezone}: the model has no clock of its own, so without this it
    invents the time. `now` is the asking device's clock when it sent one, otherwise this PC's."""
    if now is None:
        now = datetime.now().astimezone()
        zone = timezone_name or now.tzname() or _utc_offset(now)
    else:
        # A device's clock arrives as a bare offset; without its zone name, say "UTC-05:00".
        zone = timezone_name or _utc_offset(now)
    return (
        template.replace("{date}", now.strftime("%A, %B %d, %Y").replace(" 0", " "))
        .replace("{time}", now.strftime("%I:%M %p").lstrip("0"))
        .replace("{timezone}", zone)
    )


def _utc_offset(now: datetime) -> str:
    minutes = int(now.utcoffset().total_seconds() // 60)  # type: ignore[union-attr]
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _system_prompt(request: Request, body: ChatRequest) -> str | None:
    """The request's own system prompt, or the configured default plus V.A.R.A.'s personality
    (empty means none)."""
    if body.system is not None:
        prompt = body.system
    else:
        settings = request.app.state.settings
        configured = "\n\n".join(p for p in (settings.system_prompt, settings.personality) if p)
        prompt = fill_clock(configured, body.client_time, body.client_timezone)
    if body.spoken:
        prompt = f"{prompt}\n\n{SPOKEN_REPLY}" if prompt else SPOKEN_REPLY
    return prompt or None


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
            system=store.memory_prompt(_system_prompt(request, body), memories),
            memories_used=[{"id": m.id, "content": m.content} for m in memories],
            think=should_reason(
                new_messages[-1].content,
                spoken=body.spoken,
                mode=body.reasoning or request.app.state.settings.reasoning,
            ),
        )


WEB_ABILITY = (
    "You can use public information from the web: when a message needs current information (news, "
    "prices, markets, weather, scores, who holds an office), V.A.R.A. searches the web first and gives "
    "you the results below. Never say you can't access the internet, live data, or public data. If no "
    "results are given, or they don't answer the question, say you didn't find it this time and that "
    'saying "search for ..." makes V.A.R.A. search directly.'
)


def _append_system(prepared: _Prepared, block: str) -> None:
    prepared.system = f"{prepared.system}\n\n{block}" if prepared.system else block


async def _add_deals(request: Request, prepared: _Prepared, text: str, retrieved: str) -> bool:
    """Price alerts waiting to be told, and price checks or watches the message asks for. Returns True
    when the message was a buying request (so no general web search is needed)."""
    finder = getattr(request.app.state, "deals", None)
    if finder is None:
        return False
    with _db(request).session() as session:
        if alerts := price_watch.unseen_alerts(session):
            _append_system(prepared, price_watch.alerts_context(alerts))
            prepared.price_alerts = [price_watch.alert_dict(a) for a in alerts]
            for alert in alerts:
                alert.seen_at = utcnow()
    deal = parse_deal_request(text)
    if deal is None:
        return False
    log = logging.getLogger("vara")
    if deal.kind == "list_watches":
        with _db(request).session() as session:
            _append_system(
                prepared, price_watch.watches_context(price_watch.active_watches(session))
            )
        return True
    if deal.kind == "stop_watch":
        with _db(request).session() as session:
            stopped = price_watch.stop_watches(session, deal.query)
            remaining = price_watch.active_watches(session)
            note = (
                f"V.A.R.A. stopped watching: {', '.join(w.query for w in stopped)}."
                if stopped
                else f'No price watch matched "{deal.query}"; nothing was stopped.'
            )
            _append_system(prepared, f"{note}\n{price_watch.watches_context(remaining)}")
        return True

    report = await finder.find(deal.query)
    context = deal_context(report, retrieved)
    summary: dict = {
        "query": report.query,
        "offers": [asdict(o) for o in report.offers[:8]],
        "checked": report.checked,
        "failed": report.failed,
    }
    if deal.kind == "watch":
        with _db(request).session() as session:
            watch = price_watch.add_watch(session, deal.query, deal.target)
            price_watch.record_check(session, watch, report, alert=False)
            rule = (
                f"an alert when it's ${deal.target:,.2f} or less"
                if deal.target is not None
                else "an alert when the price drops at least 5% below the lowest seen"
            )
            context += (
                f'\n\nV.A.R.A. is now watching "{deal.query}": it re-checks every 6 hours and gives {rule}. '
                "Tell the user that, along with today's best price."
            )
            if deal.target is not None and report.best and report.best.price <= deal.target:
                context += " Today's best price is already at or below the target: say so."
            summary["watch"] = price_watch.watch_dict(watch)
        log.info("Price watch added: %r (target %s)", deal.query, deal.target)
    _append_system(prepared, context)
    prepared.deals = summary
    return True


async def _add_web_search(request: Request, prepared: _Prepared) -> None:
    """Search the web first if the question needs current information, and give the model the results.
    Only the search words leave this PC. A failed search never fails the chat."""
    body = prepared.body
    web = request.app.state.web
    if web is None or not body.web_search:
        return
    text = prepared.new_messages[-1].content
    history = prepared.messages[:-1]
    router = _router(request)
    # Without this the model falls back on "I can't access live data" whenever no search ran.
    prepared.system = f"{prepared.system}\n\n{WEB_ABILITY}" if prepared.system else WEB_ABILITY

    def ask_model(messages: list[Message], system: str):
        return router.stream(
            messages,
            provider=body.provider,
            model=body.model,
            system=system,
            think=False,  # a one-line search query doesn't need reasoning
            cloud_confirmed=body.confirm_cloud,
        )

    retrieved = fill_clock("{date} at {time} ({timezone})", body.client_time, body.client_timezone)
    # Buying things: price checks and price watches (vara/deals.py, vara/price_watch.py).
    if await _add_deals(request, prepared, text, retrieved):
        return
    # Market figures come from a quote source, fetched while the search is being planned: search
    # snippets rarely hold the number, and without it the model invents one.
    quotes_task = None
    markets = getattr(request.app.state, "markets", None)
    if markets is not None:
        previous = next((m.content for m in reversed(history) if m.role == "user"), "")
        symbols = find_symbols(text) or find_symbols(previous)
        if symbols:
            quotes_task = asyncio.create_task(markets.quotes(symbols))
    try:
        today = fill_clock("{date}", body.client_time, body.client_timezone)
        query = await planner.plan_search(text, today, ask_model, history)
        found = None
        if query is not None:
            found = await web.search(query, category=planner.category_for(text))
    except (*ROUTING_ERRORS, SearchFailed) as exc:
        logging.getLogger("vara").warning("Web search skipped: %s", exc)
        found = None
    if quotes_task is not None and (quotes := await quotes_task):
        context = market_context(quotes, retrieved)
        prepared.system = f"{prepared.system}\n\n{context}"
        prepared.market_data = [
            {"symbol": q.symbol, "name": q.name, "price": q.price, "as_of": q.as_of.isoformat()}
            for q in quotes
        ]
        logging.getLogger("vara").info("Market data: %s", ", ".join(q.symbol for q in quotes))
    if found is None:
        return
    context = search_context(found, retrieved)
    prepared.system = f"{prepared.system}\n\n{context}" if prepared.system else context
    prepared.web_search = {
        "query": found.query,
        "results": [{"title": r.title, "url": r.url, "engines": r.engines} for r in found.results],
        "unresponsive_engines": found.unresponsive,
    }
    logging.getLogger("vara").info(
        "Web search: %d results (%s unresponsive)", len(found.results), len(found.unresponsive)
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
            "web_search": prepared.web_search,
            "market_data": prepared.market_data,
            "deals": prepared.deals,
            "price_alerts": prepared.price_alerts,
            "reasoning": prepared.think,
        }


async def _open_stream(request: Request, prepared: _Prepared) -> AsyncIterator[RoutedEvent]:
    """Start routing and wait for the first event, so privacy and provider failures become
    ordinary HTTP errors instead of breaking a stream that has already started."""
    events = _router(request).stream(
        prepared.messages,
        provider=prepared.body.provider,
        model=prepared.body.model,
        system=prepared.system,
        think=prepared.think,
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
            {
                "name": p.name,
                "default_model": p.default_model,
                "models": list(getattr(p, "models", None) or [p.default_model]),
                "local": p.capabilities.local,
            }
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
            think=prepared.think,
            cloud_confirmed=body.confirm_cloud,
        )
    except ROUTING_ERRORS as exc:
        raise http_error(exc) from exc
    return {
        "privacy_mode": router.privacy_mode,
        "reasoning": prepared.think,
        "memories_used": prepared.memories_used,
        "candidates": candidates,
    }


class _Announcer:
    """Announces one exchange on the live-events bus (see vara/events.py)."""

    def __init__(self, request: Request, body: ChatRequest) -> None:
        self.bus = getattr(request.app.state, "events", None)
        self.base = {
            "exchange_id": uuid.uuid4().hex,
            "client": body.client,
            "source": "voice" if body.spoken else "chat",
        }
        self._publish(
            "exchange_started",
            conversation_id=body.conversation_id,
            text=body.messages[-1].content,
        )

    def _publish(self, event: str, **data) -> None:
        if self.bus is not None:
            self.bus.publish(event, {**self.base, **data})

    def delta(self, text: str, provider: str) -> None:
        self._publish("exchange_delta", text=text, provider=provider)

    def done(self, saved: dict, text: str) -> None:
        self._publish("exchange_done", text=text, **saved)

    def notice(self, text: str) -> None:
        self._publish("exchange_notice", text=text)

    def failed(self, message: str) -> None:
        self._publish("exchange_failed", message=message)


@v1.post("/chat")
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    prepared = _prepare(request, body)
    live = _Announcer(request, body)
    await _add_web_search(request, prepared)
    parts: list[str] = []
    summary: dict | None = None
    try:
        async for routed in await _open_stream(request, prepared):
            if isinstance(routed.event, TextDelta):
                parts.append(routed.event.text)
                live.delta(routed.event.text, routed.provider)
            else:
                summary = _summary(routed, routed.event)
    except ROUTING_ERRORS as exc:
        live.failed(str(exc))
        raise http_error(exc) from exc
    except HTTPException as exc:
        live.failed(str(exc.detail))
        raise

    if summary is None:
        live.failed("The provider ended without completing")
        raise HTTPException(502, "The provider ended without completing")
    text = "".join(parts)
    saved = _persist(request, prepared, text, summary)
    live.done(saved, text)
    return ChatResponse(text=text, **saved)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@v1.post("/chat/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    """Server-sent events: `delta` per text chunk, then `done` with usage, or `error`.

    If the reply hasn't started after `hold_notice_seconds`, `notice` events come first: "this is going to
    take a minute", then "still working on it" every `hold_repeat_seconds`. Notices are not part of the
    reply and are not stored. A failure that comes before any notice (privacy, no provider) is an HTTP
    error; after one it is an `error` event.

    The exchange is stored only once the reply completes; a failed stream stores nothing.
    """
    prepared = _prepare(request, body)
    live = _Announcer(request, body)
    settings = request.app.state.settings

    async def begin() -> AsyncIterator[RoutedEvent]:
        await _add_web_search(request, prepared)
        return await _open_stream(request, prepared)

    starting = asyncio.ensure_future(begin())
    await asyncio.wait({starting}, timeout=settings.hold_notice_seconds or None)
    events: AsyncIterator[RoutedEvent] | None = None
    if starting.done():
        try:
            events = starting.result()
        except HTTPException as exc:
            live.failed(str(exc.detail))
            raise

    async def holding() -> AsyncIterator[str]:
        """Notices while the reply hasn't started."""
        round_ = 0
        while not starting.done():
            if round_ == 0 or settings.hold_repeat_seconds:
                text = holding_line(round_, personality=bool(settings.personality))
                live.notice(text)
                yield _sse("notice", {"text": text})
            round_ += 1
            await asyncio.wait({starting}, timeout=settings.hold_repeat_seconds or None)

    async def sse() -> AsyncIterator[str]:
        nonlocal events
        parts: list[str] = []
        finished = False
        try:
            if events is None:
                async for notice in holding():
                    yield notice
                events = starting.result()
            async for routed in events:
                if isinstance(routed.event, TextDelta):
                    parts.append(routed.event.text)
                    live.delta(routed.event.text, routed.provider)
                    yield _sse("delta", {"provider": routed.provider, "text": routed.event.text})
                else:
                    text = "".join(parts)
                    saved = _persist(request, prepared, text, _summary(routed, routed.event))
                    finished = True
                    live.done(saved, text)
                    yield _sse("done", saved)
        except ROUTING_ERRORS as exc:
            finished = True
            live.failed(str(exc))
            yield _sse("error", {"message": str(exc)})
        except HTTPException as exc:
            finished = True
            live.failed(str(exc.detail))
            yield _sse("error", {"message": str(exc.detail)})
        finally:
            starting.cancel()  # no-op once started; stops a search or model call nobody is waiting for
            if not finished:  # the client stopped or disconnected; nothing was stored
                live.failed("Stopped before the reply finished")

    return StreamingResponse(
        sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


HEARTBEAT_SECONDS = 15.0


@v1.get("/events")
async def live_events(request: Request) -> StreamingResponse:
    """Server-sent events for open clients: every chat exchange as it happens, from any client
    (`exchange_started`, `exchange_delta`, `exchange_done`, `exchange_failed`; see vara/events.py).
    A comment line is sent every 15 seconds so idle connections stay open."""
    bus = request.app.state.events

    async def stream() -> AsyncIterator[str]:
        with bus.subscribe() as queue:
            yield ": connected\n\n"
            while not await request.is_disconnected():
                try:
                    event, data = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event, data)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
