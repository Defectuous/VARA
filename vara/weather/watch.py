"""The weather watch: fetches the forecast every few hours, scores the next three days, and raises an alert
before a shift.

Every `weather_check_hours` the forecast is fetched from Open-Meteo and stored as a snapshot (the history
is kept, not just the latest), and the 3-day outlook is computed (vara/weather/detect.py).

An alert is raised when a day's risk reaches `weather_alert_score`. After that, the next one waits for a
new day, or for the risk to climb `weather_alert_jump` points above the last alert. The same day is never
warned about twice unless its risk jumps like that. Each alert:

- is announced on /v1/events (`weather_alert`) with `active` saying whether you've talked to V.A.R.A.
  in the last `weather_active_minutes`. The voice listener speaks it when you're active and asks if you
  want the details, and shows a silent notification otherwise. Chat pages show it.
- goes to Discord when a webhook is set (`python -m vara weather discord-webhook`), but not during quiet
  hours: it waits for them to end.
- is mentioned in V.A.R.A.'s next reply, once.

A failed fetch is stored as a snapshot with its error; the outlook then comes from the last good snapshot
and says how old it is. Nothing here is medical advice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, func, select

from vara.credentials import get_api_key
from vara.db.models import StoredMessage, WeatherAlert, WeatherSnapshot, utcnow
from vara.logging import redact
from vara.weather import detect
from vara.weather.openmeteo import Hour, OpenMeteo, WeatherUnavailable

log = logging.getLogger("vara.weather")

LOOK_EVERY = 10 * 60  # seconds between looking for due checks and held Discord messages
KEEP_HOURS = (-24, 7 * 24)  # the part of each forecast stored: the past day and the week ahead
DISCORD_SECRET = "discord-webhook"
DISCORD_LIMIT = 2000  # characters in one Discord message


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None)


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC)


def parse_quiet_hours(text: str) -> tuple[time, time] | None:
    """ "05:30-12:30" as (start, end); empty means none."""
    if not text.strip():
        return None
    start, end = (time.fromisoformat(part.strip()) for part in text.split("-", 1))
    return start, end


def in_quiet_hours(local: datetime, quiet: tuple[time, time] | None) -> bool:
    if quiet is None:
        return False
    start, end = quiet
    now = local.time()
    return start <= now < end if start <= end else now >= start or now < end


async def send_discord(url: str, text: str, client: httpx.AsyncClient | None = None) -> None:
    """Post to a Discord webhook, with retries. Raises httpx.HTTPError when it still fails."""
    body = {"content": text[:DISCORD_LIMIT], "allowed_mentions": {"parse": []}}
    last: Exception | None = None
    for attempt in range(3):
        try:
            if client is not None:
                response = await client.post(url, json=body, timeout=15.0)
            else:
                async with httpx.AsyncClient(timeout=15.0) as fresh:
                    response = await fresh.post(url, json=body)
            response.raise_for_status()
            return
        except httpx.HTTPError as exc:
            last = exc
            if attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))
    assert last is not None
    raise last


class WeatherWatch:
    def __init__(
        self,
        settings,
        db,
        bus=None,
        *,
        source: OpenMeteo | None = None,
        discord=send_discord,
        webhook: str | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.bus = bus
        self.source = source or OpenMeteo(settings.weather_latitude, settings.weather_longitude)
        self._discord = discord
        self._webhook = webhook  # tests pass one; otherwise it's read from the credential store
        self.zone: tzinfo = (
            ZoneInfo(settings.weather_timezone)
            if settings.weather_timezone
            else datetime.now().astimezone().tzinfo
        )
        self.thresholds = detect.Thresholds(
            pressure_drop_hpa=settings.weather_pressure_drop_hpa,
            pressure_rise_hpa=settings.weather_pressure_rise_hpa,
            temp_drop_f=settings.weather_temp_drop_f,
            dewpoint_drop_f=settings.weather_dewpoint_drop_f,
            rain_chance=settings.weather_rain_chance,
            swing_rise_hpa=settings.weather_swing_rise_hpa,
        )
        self.quiet = parse_quiet_hours(settings.weather_quiet_hours)
        self._lock = asyncio.Lock()

    @property
    def place(self) -> str:
        return self.settings.weather_place or "your area"

    @property
    def webhook(self) -> str | None:
        return self._webhook if self._webhook is not None else get_api_key(DISCORD_SECRET)

    # ---- the forecast ----

    def build_outlook(self, hours: list[Hour], now: datetime, fetched_at: datetime) -> dict:
        days = detect.outlook(hours, now, self.zone, self.thresholds, self.settings.weather_weights)
        result = []
        for risk in days:
            start = datetime.combine(risk.day, time.min, self.zone)
            day_hours = [h for h in hours if start <= h.time < start + timedelta(days=1)]
            temps = [h.temp_f for h in day_hours if h.temp_f is not None]
            rain = [h.rain_chance for h in day_hours if h.rain_chance is not None]
            result.append(
                {
                    **risk.to_dict(),
                    "high_f": round(max(temps)) if temps else None,
                    "low_f": round(min(temps)) if temps else None,
                    "rain_chance": round(max(rain)) if rain else None,
                }
            )
        return {
            "place": self.place,
            "fetched_at": fetched_at.isoformat(),
            "computed_at": now.isoformat(),
            "timezone": str(self.zone),
            "days": result,
        }

    async def check(self, now: datetime | None = None) -> dict:
        """Fetch the forecast, store it, and return the outlook. On failure, the last good outlook comes
        back with `stale` and the error."""
        now = now or datetime.now(UTC)
        async with self._lock:
            try:
                hours = await self.source.forecast()
            except WeatherUnavailable as exc:
                log.warning("Weather check failed: %s", exc)
                with self.db.session() as session:
                    session.add(
                        WeatherSnapshot(
                            fetched_at=_naive(now),
                            latitude=self.settings.weather_latitude,
                            longitude=self.settings.weather_longitude,
                            error=str(exc),
                        )
                    )
                latest = self.latest_outlook(now)
                if latest is None:
                    raise
                return {**latest, "stale": True, "error": str(exc)}
            keep = [
                h
                for h in hours
                if now + timedelta(hours=KEEP_HOURS[0])
                <= h.time
                <= now + timedelta(hours=KEEP_HOURS[1])
            ]
            outlook = self.build_outlook(hours, now, now)
            with self.db.session() as session:
                session.add(
                    WeatherSnapshot(
                        fetched_at=_naive(now),
                        latitude=self.settings.weather_latitude,
                        longitude=self.settings.weather_longitude,
                        hours=json.dumps([h.to_dict() for h in keep]),
                        outlook=json.dumps(outlook),
                    )
                )
                session.execute(
                    delete(WeatherSnapshot).where(
                        WeatherSnapshot.fetched_at
                        < _naive(now) - timedelta(days=self.settings.weather_keep_days)
                    )
                )
            top = max(outlook["days"], key=lambda d: d["score"])
            log.info("Weather checked: riskiest day %s at %d/100", top["date"], top["score"])
            return outlook

    def _last_good(self) -> WeatherSnapshot | None:
        with self.db.session() as session:
            snapshot = session.scalars(
                select(WeatherSnapshot)
                .where(WeatherSnapshot.hours.is_not(None))
                .order_by(WeatherSnapshot.fetched_at.desc())
                .limit(1)
            ).first()
            if snapshot is not None:
                session.expunge(snapshot)
            return snapshot

    def latest_outlook(self, now: datetime | None = None) -> dict | None:
        """The outlook from the last good forecast, recomputed for `now` (so "today" stays today)."""
        snapshot = self._last_good()
        if snapshot is None:
            return None
        hours = [Hour.from_dict(h) for h in json.loads(snapshot.hours)]
        return self.build_outlook(hours, now or datetime.now(UTC), _aware(snapshot.fetched_at))

    def due(self, now: datetime) -> bool:
        with self.db.session() as session:
            last = session.scalar(select(func.max(WeatherSnapshot.fetched_at)))
        return last is None or _naive(now) - last >= timedelta(
            hours=self.settings.weather_check_hours
        )

    # ---- alerts ----

    def user_active(self, now: datetime) -> bool:
        """Whether you've talked to V.A.R.A. (any client) in the last `weather_active_minutes`."""
        with self.db.session() as session:
            last = session.scalar(select(func.max(StoredMessage.created_at)))
        return last is not None and _naive(now) - last <= timedelta(
            minutes=self.settings.weather_active_minutes
        )

    def _alert_text(self, outlook: dict, top: dict, test: bool) -> tuple[str, str]:
        days = []
        for day in outlook["days"]:
            risk = detect.DayRisk(
                date.fromisoformat(day["date"]),
                day["score"],
                [_trigger(t) for t in day["triggers"]],
            )
            days.append(risk)
        top_risk = next(d for d in days if d.day.isoformat() == top["date"])
        lines = [f"{'(Test) ' if test else ''}Weather shift ahead in {self.place}."]
        lines += [detect.summarize(d, f"{d.day:%A}", self.zone) for d in days if d.score > 0]
        lines.append("Be ready: " + "; ".join(self.settings.weather_checklist) + ".")
        fetched = datetime.fromisoformat(outlook["fetched_at"]).astimezone(self.zone)
        lines.append(f"(Open-Meteo forecast from {fetched:%a %I:%M %p}.)")
        return detect.spoken(top_risk, self.zone), "\n".join(lines)

    def _should_alert(self, top: dict, now: datetime) -> bool:
        if top["score"] < self.settings.weather_alert_score:
            return False
        with self.db.session() as session:
            last = session.scalars(
                select(WeatherAlert)
                .where(WeatherAlert.test.is_(False))
                .order_by(WeatherAlert.id.desc())
                .limit(1)
            ).first()
            if last is None:
                return True
            jumped = top["score"] >= last.score + self.settings.weather_alert_jump
            same_day = _aware(last.created_at).astimezone(self.zone).date() == (
                now.astimezone(self.zone).date()
            )
            return jumped or (not same_day and last.target_date != top["date"])

    async def evaluate(self, outlook: dict, now: datetime | None = None) -> WeatherAlert | None:
        """Raise an alert if the outlook calls for one, and send it."""
        now = now or datetime.now(UTC)
        top = max(outlook["days"], key=lambda d: d["score"])
        if not self._should_alert(top, now):
            return None
        return await self.raise_alert(outlook, top, now)

    async def raise_alert(
        self, outlook: dict, top: dict, now: datetime, *, test: bool = False
    ) -> WeatherAlert:
        spoken, text = self._alert_text(outlook, top, test)
        with self.db.session() as session:
            alert = WeatherAlert(
                created_at=_naive(now),
                target_date=top["date"],
                score=top["score"],
                spoken=spoken,
                text=text,
                outlook=json.dumps(outlook),
                test=test,
            )
            session.add(alert)
            session.flush()
            session.expunge(alert)
        log.info("Weather alert %d: %s at %d/100", alert.id, top["date"], top["score"])
        if self.bus is not None:
            self.bus.publish(
                "weather_alert", {**alert_dict(alert), "active": self.user_active(now)}
            )
        await self.send_held(now)
        return alert

    async def send_held(self, now: datetime | None = None) -> None:
        """Send alerts to Discord that haven't gone yet, unless it's quiet hours. Alerts more than a day
        old are dropped (the forecast has moved on)."""
        now = now or datetime.now(UTC)
        url = self.webhook
        if not url or in_quiet_hours(now.astimezone(self.zone), self.quiet):
            return
        with self.db.session() as session:
            waiting = list(
                session.scalars(
                    select(WeatherAlert).where(
                        WeatherAlert.discord_sent_at.is_(None),
                        WeatherAlert.created_at >= _naive(now) - timedelta(days=1),
                    )
                )
            )
            for alert in waiting:
                try:
                    await self._discord(url, alert.text)
                except httpx.HTTPError as exc:
                    alert.discord_error = redact(str(exc)).replace(url, "[webhook]")
                    log.warning("Couldn't send weather alert %d to Discord", alert.id)
                else:
                    alert.discord_sent_at = _naive(now)
                    alert.discord_error = None
                    log.info("Weather alert %d sent to Discord", alert.id)

    # ---- the background task ----

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                now = datetime.now(UTC)
                if self.due(now):
                    outlook = await self.check(now)
                    if not outlook.get("stale"):
                        await self.evaluate(outlook, now)
                await self.send_held(now)
            except Exception as exc:  # noqa: BLE001 - one bad check must not end the watching
                log.warning("Weather watch: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=LOOK_EVERY)
            except TimeoutError:
                pass


def _trigger(data: dict) -> detect.Trigger:
    return detect.Trigger(
        data["kind"],
        data["amount"],
        datetime.fromisoformat(data["start"]),
        datetime.fromisoformat(data["end"]),
        data.get("before"),
        data.get("after"),
    )


def alert_dict(alert: WeatherAlert) -> dict:
    def iso(moment: datetime | None) -> str | None:
        return _aware(moment).isoformat() if moment else None

    return {
        "id": alert.id,
        "created_at": iso(alert.created_at),
        "date": alert.target_date,
        "score": alert.score,
        "spoken": alert.spoken,
        "text": alert.text,
        "test": alert.test,
        "discord_sent_at": iso(alert.discord_sent_at),
        "discord_error": alert.discord_error,
        "announced_at": iso(alert.announced_at),
        "seen_at": iso(alert.seen_at),
    }


def unseen_alerts(session, now: datetime | None = None) -> list[WeatherAlert]:
    """Alerts from the last day not yet mentioned in a reply."""
    now = now or datetime.now(UTC)
    return list(
        session.scalars(
            select(WeatherAlert)
            .where(
                WeatherAlert.seen_at.is_(None),
                WeatherAlert.created_at >= _naive(now) - timedelta(days=1),
            )
            .order_by(WeatherAlert.id)
        )
    )


def alerts_context(alerts: list[WeatherAlert]) -> str:
    """What the model is told about alerts it hasn't mentioned yet."""
    parts = []
    for alert in alerts:
        if alert.announced_at is not None:
            parts.append(
                "V.A.R.A. just told the user this weather alert out loud and asked if they want the "
                "details. If they say yes or ask about it, give the details below, briefly; otherwise "
                f"don't repeat it.\n{alert.text}"
            )
        else:
            parts.append(
                "V.A.R.A. raised this weather alert for the user and hasn't told them yet. Mention it "
                f"briefly at the start of your reply, then answer their message.\n{alert.text}"
            )
    return "\n\n".join(parts)


def mark_seen(alerts: list[WeatherAlert]) -> None:
    for alert in alerts:
        alert.seen_at = utcnow()
