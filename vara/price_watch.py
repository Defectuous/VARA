"""Price watches: V.A.R.A. re-checks prices on a schedule and tells you when something gets cheap.

An alert is raised when a fresh offer is at or below the watch's target price, or, with no target, at
least 5% below the lowest price seen so far. "Fresh" means a store page or store API price, or a deal
posted in the last ALERT_DEAL_AGE (an older post may have expired). Each alert is sent once per price,
announced to open chat pages (`price_alert` on /v1/events), and mentioned in V.A.R.A.'s next reply.
Watching only reads prices; V.A.R.A. never buys anything.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from vara.db.models import PriceAlert, PriceWatch, utcnow
from vara.deals import DealReport, Offer, relevant

log = logging.getLogger("vara.deals")

CHECK_EVERY = timedelta(hours=6)
LOOK_EVERY = 30 * 60  # seconds between looking for watches that are due
ALERT_DEAL_AGE = timedelta(days=7)
DROP_WITHOUT_TARGET = 0.95  # 5% below the lowest seen


def add_watch(session: Session, query: str, target: float | None) -> PriceWatch:
    """Start watching (or change the target of an existing watch for the same thing)."""
    for watch in active_watches(session):
        if watch.query.lower() == query.lower():
            watch.target_price = target
            return watch
    watch = PriceWatch(query=query, target_price=target)
    session.add(watch)
    session.flush()
    return watch


def active_watches(session: Session) -> list[PriceWatch]:
    return list(
        session.scalars(select(PriceWatch).where(PriceWatch.active).order_by(PriceWatch.id))
    )


def stop_watches(session: Session, query: str) -> list[PriceWatch]:
    """Stop the watches that match `query` ("the AirPods" stops "AirPods Pro 3")."""
    stopped = []
    for watch in active_watches(session):
        if relevant(query, watch.query) or query.lower() in watch.query.lower():
            watch.active = False
            stopped.append(watch)
    return stopped


def _fresh(offer: Offer, now: datetime) -> bool:
    if offer.posted is None:  # a store page or API price, read just now
        return offer.in_stock is not False
    posted = datetime.fromisoformat(offer.posted).replace(tzinfo=UTC)
    return now - posted <= ALERT_DEAL_AGE


def record_check(
    session: Session, watch: PriceWatch, report: DealReport, *, alert: bool = True
) -> PriceAlert | None:
    """Store a price check. Returns the new alert, if this check raised one. `alert=False` (the first
    check, made while you're being told the prices anyway) only sets the baseline."""
    now = datetime.now(UTC)
    watch.last_checked_at = utcnow()
    fresh = [o for o in report.offers if _fresh(o, now)]
    best = min(fresh, key=lambda o: o.price, default=None)
    if best is None:
        return None
    previous_best = watch.best_price
    if previous_best is None or best.price < previous_best:
        watch.best_price, watch.best_store = best.price, best.store
        watch.best_title, watch.best_url = best.title, best.url
    if watch.target_price is not None:
        triggered = best.price <= watch.target_price
    else:
        triggered = previous_best is not None and best.price <= previous_best * DROP_WITHOUT_TARGET
    if not triggered or (watch.alerted_price is not None and best.price >= watch.alerted_price):
        return None
    watch.alerted_price = best.price
    if not alert:
        return None
    created = PriceAlert(
        watch_id=watch.id,
        query=watch.query,
        price=best.price,
        store=best.store,
        title=best.title,
        url=best.url,
    )
    session.add(created)
    session.flush()
    return created


def unseen_alerts(session: Session) -> list[PriceAlert]:
    return list(
        session.scalars(
            select(PriceAlert).where(PriceAlert.seen_at.is_(None)).order_by(PriceAlert.id)
        )
    )


def alert_dict(alert: PriceAlert) -> dict:
    return {
        "id": alert.id,
        "watch_id": alert.watch_id,
        "query": alert.query,
        "price": alert.price,
        "store": alert.store,
        "title": alert.title,
        "url": alert.url,
        "created_at": alert.created_at.isoformat() + "Z",
    }


def watch_dict(watch: PriceWatch) -> dict:
    return {
        "id": watch.id,
        "query": watch.query,
        "target_price": watch.target_price,
        "active": watch.active,
        "created_at": watch.created_at.isoformat() + "Z",
        "last_checked_at": watch.last_checked_at.isoformat() + "Z"
        if watch.last_checked_at
        else None,
        "best_price": watch.best_price,
        "best_store": watch.best_store,
        "best_title": watch.best_title,
        "best_url": watch.best_url,
    }


def alerts_context(alerts: list[PriceAlert]) -> str:
    lines = [
        (
            "Price alerts from your watches since you last talked (tell the user about these first, briefly, "
            "with the price and store; the links are on the chat page):"
        )
    ]
    lines += [f"- {a.query}: ${a.price:,.2f} at {a.store} ({a.title}) {a.url}" for a in alerts]
    return "\n".join(lines)


def watches_context(watches: list[PriceWatch]) -> str:
    if not watches:
        return 'Price watches: none. The user can say, e.g., "tell me when X drops below $N".'
    lines = ["Price watches V.A.R.A. checks every 6 hours:"]
    for w in watches:
        target = (
            f"alert at or below ${w.target_price:,.2f}" if w.target_price else "alert on a 5% drop"
        )
        best = f"; lowest seen ${w.best_price:,.2f} at {w.best_store}" if w.best_price else ""
        lines.append(f"- {w.query} ({target}{best})")
    return "\n".join(lines)


async def maintain_watches(db, finder, bus, stop: asyncio.Event) -> None:
    """Background task: re-check each active watch every CHECK_EVERY while V.A.R.A. runs."""
    while not stop.is_set():
        try:
            with db.session() as session:
                due = [
                    (w.id, w.query)
                    for w in active_watches(session)
                    if w.last_checked_at is None
                    or datetime.now(UTC).replace(tzinfo=None) - w.last_checked_at >= CHECK_EVERY
                ]
            for watch_id, query in due:
                report = await finder.find(query)
                with db.session() as session:
                    watch = session.get(PriceWatch, watch_id)
                    if watch is None or not watch.active:
                        continue
                    created = record_check(session, watch, report)
                    if created is not None:
                        log.info(
                            "Price alert: %s at $%.2f (%s)", query, created.price, created.store
                        )
                        if bus is not None:
                            bus.publish("price_alert", alert_dict(created))
        except Exception as exc:  # noqa: BLE001 - one bad check must not end the watching
            log.warning("Price watch check failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=LOOK_EVERY)
        except TimeoutError:
            pass
