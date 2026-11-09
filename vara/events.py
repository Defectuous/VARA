"""Live events for open clients: the chat page shows a voice conversation as it happens.

Every chat exchange, from any client (the voice listener, a browser tab, the API), is announced here:
`exchange_started` with the question, `exchange_notice` while a slow reply hasn't started ("this is going to
take a minute"), `exchange_delta` for each piece of the reply, then `exchange_done`
(stored; carries the conversation id) or `exchange_failed`. Clients subscribe through `GET /v1/events`.

Nothing is stored; a client that isn't connected simply misses the events and reads the history instead.
V.A.R.A. runs as a single process, so an in-memory list of subscribers is enough.
"""

import asyncio
import contextlib
import logging
from collections.abc import Iterator

log = logging.getLogger("vara.events")

QUEUE_SIZE = 1000  # a client that falls this far behind loses events rather than slowing chat


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def publish(self, event: str, data: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                log.warning("A live-events client is too far behind; dropping an event for it")

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)
