"""The weather watch: the front detector, the risk score, alert rules, quiet hours, and how alerts reach the
user (Discord, live events for the voice listener, the next reply)."""

import asyncio
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from vara.config import Settings
from vara.db.database import Database
from vara.db.models import WeatherAlert, WeatherSnapshot
from vara.events import EventBus
from vara.main import create_app
from vara.providers.fake import FakeProvider
from vara.router import ModelRouter, PrivacyMode
from vara.weather import detect
from vara.weather.openmeteo import Hour, WeatherUnavailable, parse_hourly
from vara.weather.watch import WeatherWatch, in_quiet_hours, parse_quiet_hours

ZONE = ZoneInfo("America/New_York")
FIXTURE = Path(__file__).parent / "fixtures" / "columbus-2025-11-26-archive.json"
# Noon in Columbus on Friday 2025-11-28, before the real swing that weekend.
FRIDAY_NOON = datetime(2025, 11, 28, 17, tzinfo=UTC)


def real_week() -> list[Hour]:
    return parse_hourly(json.loads(FIXTURE.read_text(encoding="utf-8")))


def shaped_week(start: datetime) -> list[Hour]:
    """The owner's numbers: 1011 → 1035 hPa in 40 hours, then down 28 hPa in one day, with the cold
    air and rain that usually come with the drop."""
    hours = []
    for h in range(7 * 24):
        if h <= 40:
            pressure = 1011 + 24 * h / 40
        elif h <= 64:
            pressure = 1035 - 28 * (h - 40) / 24
        else:
            pressure = 1007.0
        temp = 50.0 if h < 52 else max(30.0, 50 - (h - 52) * 1.2)
        dew = 40.0 if h < 52 else max(20.0, 40 - (h - 52) * 1.2)
        rain = 80.0 if 56 <= h <= 62 else 5.0
        hours.append(
            Hour(start + timedelta(hours=h), pressure, temp, 70.0, dew, rain, 0.0, 10.0, 270.0)
        )
    return hours


# ---- the detector and the score ----


def test_the_real_swing_week_is_flagged_on_the_right_day() -> None:
    days = detect.outlook(real_week(), FRIDAY_NOON, ZONE, days=5)
    sunday = days[2]

    assert sunday.day.isoformat() == "2025-11-30"
    assert {"pressure_drop", "dewpoint_drop", "pressure_rise"} <= {t.kind for t in sunday.triggers}
    drop = next(t for t in sunday.triggers if t.kind == "pressure_drop")
    assert drop.amount == pytest.approx(19.8, abs=0.5)
    assert sunday.score >= 60
    # Friday and Saturday are calm: the high builds slowly.
    assert days[0].score == 0 and days[1].score == 0
    assert sunday.summary.startswith("Sunday: ")


def test_the_owners_swing_shape_is_caught_as_a_swing_then_a_front() -> None:
    start = datetime(2026, 1, 5, 5, tzinfo=UTC)  # midnight in Columbus
    days = detect.outlook(shaped_week(start), start, ZONE)
    kinds = {t.kind for d in days for t in d.triggers}

    assert {"swing", "pressure_drop", "temp_drop", "rain"} <= kinds
    swing = next(t for d in days for t in d.triggers if t.kind == "swing")
    assert swing.amount == pytest.approx(24, abs=0.5)
    assert swing.end == start + timedelta(hours=40)  # the peak, when the last illness started
    drop = next(t for d in days for t in d.triggers if t.kind == "pressure_drop")
    assert drop.amount == pytest.approx(28, abs=0.5)
    assert max(d.score for d in days) >= 60


def test_changes_that_already_happened_are_not_a_warning() -> None:
    start = datetime(2026, 1, 5, 5, tzinfo=UTC)
    hours = shaped_week(start)
    # After the drop is over, nothing is left to warn about.
    days = detect.outlook(hours, start + timedelta(hours=66), ZONE)
    assert not any(t.kind == "pressure_drop" for d in days for t in d.triggers)


def test_small_changes_score_zero_and_big_ones_cap_at_100() -> None:
    calm = [
        Hour(FRIDAY_NOON + timedelta(hours=h), 1015 + h * 0.1, 50, 60, 40, 10, 0, 5, 180)
        for h in range(72)
    ]
    assert all(d.score == 0 for d in detect.outlook(calm, FRIDAY_NOON, ZONE))
    huge = [
        detect.Trigger(kind, 100.0, FRIDAY_NOON, FRIDAY_NOON) for kind in detect.DEFAULT_WEIGHTS
    ]
    assert detect.score(huge, detect.Thresholds()) == 100


def test_weights_can_be_changed() -> None:
    swing = [detect.Trigger("swing", 20.0, FRIDAY_NOON, FRIDAY_NOON)]
    assert detect.score(swing, detect.Thresholds()) == 35
    assert detect.score(swing, detect.Thresholds(), {"swing": 70}) == 70


def test_quiet_hours_including_ones_that_wrap_midnight() -> None:
    day = parse_quiet_hours("05:30-12:30")
    assert in_quiet_hours(datetime(2026, 1, 5, 9, 0), day)
    assert not in_quiet_hours(datetime(2026, 1, 5, 12, 30), day)
    night = parse_quiet_hours("22:00-06:00")
    assert night == (time(22), time(6))
    assert in_quiet_hours(datetime(2026, 1, 5, 23, 0), night)
    assert in_quiet_hours(datetime(2026, 1, 5, 3, 0), night)
    assert not in_quiet_hours(datetime(2026, 1, 5, 7, 0), night)
    assert parse_quiet_hours("") is None


# ---- the watch ----


class FakeSource:
    def __init__(self, hours: list[Hour] | None = None, fail: bool = False) -> None:
        self.hours = hours if hours is not None else real_week()
        self.fail = fail
        self.calls = 0

    async def forecast(self) -> list[Hour]:
        self.calls += 1
        if self.fail:
            raise WeatherUnavailable("Open-Meteo didn't answer")
        return self.hours


def settings(tmp_path, **extra) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        weather_latitude=39.96,
        weather_longitude=-83.0,
        weather_place="Columbus",
        weather_timezone="America/New_York",
        **extra,
    )


def make_watch(tmp_path, source=None, webhook="https://discord.example/hook", **extra):
    config = settings(tmp_path, **extra)
    db = Database(config.database_path)
    db.upgrade()
    sent: list[str] = []

    async def discord(url, text):
        sent.append(text)

    bus = EventBus()
    watch = WeatherWatch(
        config, db, bus, source=source or FakeSource(), discord=discord, webhook=webhook
    )
    return watch, db, bus, sent


def test_a_check_stores_the_forecast_and_its_outlook(tmp_path) -> None:
    watch, db, _bus, _sent = make_watch(tmp_path)
    outlook = asyncio.run(watch.check(FRIDAY_NOON))

    assert outlook["place"] == "Columbus"
    assert [d["date"] for d in outlook["days"]] == ["2025-11-28", "2025-11-29", "2025-11-30"]
    assert outlook["days"][2]["score"] >= 60
    assert outlook["days"][2]["high_f"] is not None
    with db.session() as session:
        [snapshot] = session.query(WeatherSnapshot).all()
        assert json.loads(snapshot.outlook)["days"][2]["score"] == outlook["days"][2]["score"]
        assert len(json.loads(snapshot.hours)) > 100  # the past day and the week ahead
    assert not watch.due(FRIDAY_NOON + timedelta(hours=1))
    assert watch.due(FRIDAY_NOON + timedelta(hours=3))


def test_a_failed_fetch_falls_back_to_the_last_good_forecast(tmp_path) -> None:
    source = FakeSource()
    watch, db, _bus, _sent = make_watch(tmp_path, source)
    asyncio.run(watch.check(FRIDAY_NOON))
    source.fail = True
    outlook = asyncio.run(watch.check(FRIDAY_NOON + timedelta(hours=3)))

    assert outlook["stale"] is True and "didn't answer" in outlook["error"]
    assert outlook["days"][2]["score"] >= 60
    with db.session() as session:
        assert (
            session.query(WeatherSnapshot).filter(WeatherSnapshot.error.is_not(None)).count() == 1
        )


def test_an_alert_waits_out_quiet_hours_then_goes_to_discord(tmp_path) -> None:
    watch, db, bus, sent = make_watch(tmp_path)
    with bus.subscribe() as events:
        outlook = asyncio.run(watch.check(FRIDAY_NOON))  # noon: still quiet until 12:30
        alert = asyncio.run(watch.evaluate(outlook, FRIDAY_NOON))
        event, data = events.get_nowait()

    assert alert is not None and alert.target_date == "2025-11-30"
    assert event == "weather_alert" and data["active"] is False
    assert data["spoken"].startswith("Sunday scores")
    assert "→" not in data["spoken"]  # spoken text has no symbols
    assert sent == []  # quiet hours
    asyncio.run(watch.send_held(FRIDAY_NOON + timedelta(minutes=40)))
    assert len(sent) == 1
    assert sent[0].startswith("Weather shift ahead in Columbus.")
    assert "Be ready: saline rinse; run the humidifier" in sent[0]
    with db.session() as session:
        assert session.get(WeatherAlert, alert.id).discord_sent_at is not None


def test_the_same_day_is_not_warned_about_twice_unless_it_jumps(tmp_path) -> None:
    watch, _db, _bus, _sent = make_watch(tmp_path)
    outlook = asyncio.run(watch.check(FRIDAY_NOON))
    assert asyncio.run(watch.evaluate(outlook, FRIDAY_NOON)) is not None
    later = FRIDAY_NOON + timedelta(hours=3)
    assert asyncio.run(watch.evaluate(outlook, later)) is None
    # Next day, same target day, same score: still not again.
    assert asyncio.run(watch.evaluate(outlook, FRIDAY_NOON + timedelta(days=1))) is None
    # A jump of 20+ points alerts again.
    jumped = json.loads(json.dumps(outlook))
    jumped["days"][2]["score"] = min(100, outlook["days"][2]["score"] + 20)
    if jumped["days"][2]["score"] - outlook["days"][2]["score"] >= 20:
        assert asyncio.run(watch.evaluate(jumped, later)) is not None


def test_a_calm_outlook_raises_nothing(tmp_path) -> None:
    calm = [
        Hour(FRIDAY_NOON + timedelta(hours=h - 24), 1015.0, 50, 60, 40, 10, 0, 5, 180)
        for h in range(7 * 24)
    ]
    watch, _db, _bus, sent = make_watch(tmp_path, FakeSource(calm))
    outlook = asyncio.run(watch.check(FRIDAY_NOON))
    assert asyncio.run(watch.evaluate(outlook, FRIDAY_NOON)) is None
    assert sent == []


def test_without_a_webhook_alerts_still_reach_the_listener(tmp_path) -> None:
    watch, _db, bus, sent = make_watch(tmp_path, webhook="")  # "" means none set
    after_quiet = FRIDAY_NOON + timedelta(hours=1)
    with bus.subscribe() as events:
        outlook = asyncio.run(watch.check(after_quiet))
        asyncio.run(watch.evaluate(outlook, after_quiet))
        assert events.get_nowait()[0] == "weather_alert"
    assert sent == []


def test_a_discord_failure_is_recorded_without_the_url(tmp_path) -> None:
    watch, db, _bus, _sent = make_watch(tmp_path)

    async def broken(url, text):
        raise httpx.ConnectError(f"can't reach {url}")

    watch._discord = broken
    after_quiet = FRIDAY_NOON + timedelta(hours=1)
    outlook = asyncio.run(watch.check(after_quiet))
    alert = asyncio.run(watch.evaluate(outlook, after_quiet))
    with db.session() as session:
        error = session.get(WeatherAlert, alert.id).discord_error
    assert "discord.example" not in error and "[webhook]" in error


# ---- through the app: live events, the next reply, the API, and the tool ----


def app_client(tmp_path, model: FakeProvider, **extra) -> tuple[TestClient, WeatherWatch]:
    config = settings(tmp_path, **extra)
    app = create_app(config, router=ModelRouter([model], PrivacyMode.LOCAL_ONLY), weather=False)
    watch = WeatherWatch(config, app.state.db, app.state.events, source=FakeSource(), webhook="")
    app.state.weather = watch
    return TestClient(app), watch


def ask(client: TestClient, text: str, **extra) -> dict:
    response = client.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": text}], **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_next_reply_mentions_an_alert_once(tmp_path) -> None:
    model = FakeProvider("local")
    client, watch = app_client(tmp_path, model)
    with client:
        now = datetime.now(UTC)
        outlook = asyncio.run(watch.check(now))
        top = max(outlook["days"], key=lambda d: d["score"])
        alert = asyncio.run(watch.raise_alert(outlook, top, now, test=True))
        first = ask(client, "hello")
        second = ask(client, "hello again")

    assert [a["id"] for a in first["weather_alerts"]] == [alert.id]
    assert "hasn't told them yet" in model.systems[0]
    assert second["weather_alerts"] is None
    assert "weather alert" not in (model.systems[1] or "")


def test_after_the_listener_says_it_the_reply_gives_details_if_asked(tmp_path) -> None:
    model = FakeProvider("local")
    client, watch = app_client(tmp_path, model)
    with client:
        now = datetime.now(UTC)
        outlook = asyncio.run(watch.check(now))
        top = max(outlook["days"], key=lambda d: d["score"])
        alert = asyncio.run(watch.raise_alert(outlook, top, now, test=True))
        marked = client.post(f"/v1/weather/alerts/{alert.id}/announced").json()
        ask(client, "yes, tell me more")

    assert marked["announced_at"] is not None
    assert "just told the user this weather alert out loud" in model.systems[0]


def test_the_listener_hears_whether_the_user_is_active(tmp_path) -> None:
    client, watch = app_client(tmp_path, FakeProvider("local"))
    with client:
        ask(client, "hello")  # an exchange just now: active
        now = datetime.now(UTC)
        outlook = asyncio.run(watch.check(now))
        top = max(outlook["days"], key=lambda d: d["score"])
        with client.app.state.events.subscribe() as events:
            asyncio.run(watch.raise_alert(outlook, top, now, test=True))
            event, data = events.get_nowait()
    assert event == "weather_alert" and data["active"] is True
    assert watch.user_active(now) and not watch.user_active(now + timedelta(minutes=11))


def test_the_test_alert_endpoint_and_alert_list(tmp_path) -> None:
    client, _watch = app_client(tmp_path, FakeProvider("local"))
    with client:
        alert = client.post("/v1/weather/alerts/test").json()
        listed = client.get("/v1/weather/alerts").json()["alerts"]
        outlook = client.get("/v1/weather/outlook").json()

    assert alert["test"] is True and alert["text"].startswith("(Test) Weather shift ahead")
    assert [a["id"] for a in listed] == [alert["id"]]
    assert len(outlook["days"]) == 3


def test_the_weather_api_says_when_the_watch_is_off() -> None:
    app = create_app(
        Settings(_env_file=None), router=ModelRouter([FakeProvider("local")], "local_only")
    )
    with TestClient(app) as client:
        response = client.get("/v1/weather/outlook")
    assert response.status_code == 503 and "VARA_WEATHER_LATITUDE" in response.json()["detail"]


def test_chat_can_ask_for_the_outlook_as_a_tool(tmp_path) -> None:
    from tests.test_agent import call

    model = FakeProvider("local", script=[[call("weather_outlook")], "Sunday looks rough."])
    client, watch = app_client(tmp_path, model)
    with client:
        asyncio.run(watch.check(datetime.now(UTC)))
        reply = ask(client, "anything I should be ready for this week?")

    assert reply["actions"][0]["action"] == "weather_outlook"
    assert reply["actions"][0]["status"] == "succeeded"
    assert "Weather outlook for Columbus" in model.calls[1][-1].content
