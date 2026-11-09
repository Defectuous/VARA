"""Price checks, price watches, and price alerts (see vara/deals.py and vara/price_watch.py)."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from vara import price_watch
from vara.db.models import PriceAlert, PriceWatch, utcnow

deals = APIRouter(prefix="/v1/deals")


def _finder(request: Request):
    finder = getattr(request.app.state, "deals", None)
    if finder is None:
        raise HTTPException(503, "Price checks need web search (VARA_WEB_SEARCH=auto)")
    return finder


@deals.get("")
async def find(q: str, request: Request) -> dict:
    """The best prices for `q` right now, cheapest first, from deal posts and store pages."""
    return (await _finder(request).find(q[:120])).to_dict()


class NewWatch(BaseModel):
    query: str = Field(min_length=2, max_length=120)
    target_price: float | None = Field(default=None, gt=0)


@deals.get("/watches")
def list_watches(request: Request) -> list[dict]:
    with request.app.state.db.session() as session:
        return [price_watch.watch_dict(w) for w in price_watch.active_watches(session)]


@deals.post("/watches", status_code=201)
async def add_watch(body: NewWatch, request: Request) -> dict:
    """Start watching; today's prices are checked straight away to set the baseline."""
    report = await _finder(request).find(body.query)
    with request.app.state.db.session() as session:
        watch = price_watch.add_watch(session, body.query, body.target_price)
        price_watch.record_check(session, watch, report, alert=False)
        return {**price_watch.watch_dict(watch), "offers": report.to_dict()["offers"]}


@deals.delete("/watches/{watch_id}", status_code=204)
def stop_watch(watch_id: int, request: Request) -> None:
    with request.app.state.db.session() as session:
        watch = session.get(PriceWatch, watch_id)
        if watch is None:
            raise HTTPException(404, "Price watch not found")
        watch.active = False


@deals.get("/alerts")
def list_alerts(request: Request, unseen: bool = False) -> list[dict]:
    with request.app.state.db.session() as session:
        if unseen:
            alerts = price_watch.unseen_alerts(session)
        else:
            alerts = session.query(PriceAlert).order_by(PriceAlert.id.desc()).limit(50).all()
        return [price_watch.alert_dict(a) for a in alerts]


@deals.post("/alerts/{alert_id}/seen", status_code=204)
def mark_seen(alert_id: int, request: Request) -> None:
    with request.app.state.db.session() as session:
        alert = session.get(PriceAlert, alert_id)
        if alert is None:
            raise HTTPException(404, "Price alert not found")
        alert.seen_at = alert.seen_at or utcnow()
