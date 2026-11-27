"""The weather watch's API (see vara/weather/watch.py). Alerts are also announced on /v1/events."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from vara.db.models import WeatherAlert, utcnow
from vara.weather.openmeteo import WeatherUnavailable
from vara.weather.watch import WeatherWatch, alert_dict

weather = APIRouter(prefix="/v1/weather")


def _watch(request: Request) -> WeatherWatch:
    watch = getattr(request.app.state, "weather", None)
    if watch is None:
        raise HTTPException(
            503, "The weather watch is off: set VARA_WEATHER_LATITUDE and VARA_WEATHER_LONGITUDE"
        )
    return watch


@weather.get("/outlook")
async def outlook(request: Request, refresh: bool = False) -> dict:
    """The risk for today and the next two days, with what sets it off and each day's high, low, and rain
    chance. From the last forecast fetched, or a fresh one with `refresh=true` (or when there is none)."""
    watch = _watch(request)
    latest = None if refresh else watch.latest_outlook()
    if latest is not None:
        return latest
    try:
        return await watch.check()
    except WeatherUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc


@weather.post("/check")
async def check(request: Request) -> dict:
    """Fetch the forecast now and raise an alert if it calls for one."""
    watch = _watch(request)
    try:
        result = await watch.check()
    except WeatherUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    alert = None if result.get("stale") else await watch.evaluate(result)
    return {"outlook": result, "alert": alert_dict(alert) if alert else None}


@weather.get("/alerts")
def alerts(request: Request, limit: int = 20) -> dict:
    with request.app.state.db.session() as session:
        rows = session.scalars(
            select(WeatherAlert).order_by(WeatherAlert.id.desc()).limit(min(max(limit, 1), 200))
        )
        return {"alerts": [alert_dict(a) for a in rows]}


@weather.post("/alerts/test")
async def test_alert(request: Request) -> dict:
    """Send a test alert from the current outlook through every channel (Discord, live events, so the voice
    listener and chat pages react, and the next reply). Marked "(Test)"; it doesn't hold back real ones."""
    watch = _watch(request)
    latest = watch.latest_outlook()
    if latest is None:
        try:
            latest = await watch.check()
        except WeatherUnavailable as exc:
            raise HTTPException(502, str(exc)) from exc
    top = max(latest["days"], key=lambda d: d["score"])
    alert = await watch.raise_alert(latest, top, datetime.now(UTC), test=True)
    return alert_dict(alert)


@weather.post("/alerts/{alert_id}/announced")
def announced(alert_id: int, request: Request) -> dict:
    """The voice listener said this alert out loud; the next reply gives details if asked."""
    with request.app.state.db.session() as session:
        alert = session.get(WeatherAlert, alert_id)
        if alert is None:
            raise HTTPException(404, "Alert not found")
        alert.announced_at = alert.announced_at or utcnow()
        return alert_dict(alert)
