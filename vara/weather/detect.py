"""The cold-front detector and the 3-day risk score.

Pure functions over hourly values (vara/weather/openmeteo.py), so they can be tested with saved weather.

For each of the next three days (local calendar days, starting today), a trigger belongs to the day it
lands: the end of a pressure or temperature change, the peak of a swing, the first rainy hour. A day
counts the largest change that ends in it, measured over any window of up to 24 hours:

- pressure_drop: pressure falls at least `pressure_drop_hpa` (a front coming)
- pressure_rise: pressure rises at least `pressure_rise_hpa`
- temp_drop: temperature falls at least `temp_drop_f`
- dewpoint_drop: dew point falls at least `dewpoint_drop_f` (drier air coming in)
- rain: a rain chance of at least `rain_chance` within 24 hours after a qualifying pressure drop
- swing: a pressure low followed by a rise of at least `swing_rise_hpa` within 48 hours. The owner's last
  illness started at the high-pressure peak right after a swing like that, so the peak is the moment.

The score adds each trigger's weight, scaled by how far past its threshold it goes (up to 1.5 times the
weight), and is capped at 100.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo

from vara.weather.openmeteo import Hour

WINDOW = 24  # hours: the span a change is measured over
SWING_HOURS = 48
DAYS = 3


@dataclass(frozen=True)
class Thresholds:
    pressure_drop_hpa: float = 15.0
    pressure_rise_hpa: float = 15.0
    temp_drop_f: float = 15.0
    dewpoint_drop_f: float = 15.0
    rain_chance: float = 50.0
    swing_rise_hpa: float = 20.0


DEFAULT_WEIGHTS = {
    "pressure_drop": 30.0,
    "pressure_rise": 20.0,
    "temp_drop": 20.0,
    "dewpoint_drop": 15.0,
    "rain": 15.0,
    "swing": 35.0,
}


@dataclass(frozen=True)
class Trigger:
    kind: str
    amount: float  # hPa, °F, or percent (rain)
    start: datetime  # UTC
    end: datetime  # UTC: when it lands (the swing's peak, the first rainy hour)
    before: float | None = None  # the value at the start
    after: float | None = None  # the value at the end

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "amount": round(self.amount, 1),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "before": None if self.before is None else round(self.before, 1),
            "after": None if self.after is None else round(self.after, 1),
        }


@dataclass
class DayRisk:
    day: date
    score: int
    triggers: list[Trigger] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "score": self.score,
            "summary": self.summary,
            "triggers": [t.to_dict() for t in self.triggers],
        }


def _largest_change(
    hours: list[Hour], attr: str, falling: bool, end_from: int, end_to: int, start_from: int = 0
) -> tuple[float, int, int] | None:
    """The largest fall (or rise) over a window of up to WINDOW hours starting at or after
    `start_from` and ending at an index in [end_from, end_to): (amount, start index, end index)."""
    best = None
    for j in range(max(end_from, 1), end_to):
        b = getattr(hours[j], attr)
        if b is None:
            continue
        for i in range(max(start_from, j - WINDOW), j):
            a = getattr(hours[i], attr)
            if a is None:
                continue
            change = a - b if falling else b - a
            if change > 0 and (best is None or change > best[0]):
                best = (change, i, j)
    return best


def _swing(hours: list[Hour], peak_from: int, peak_to: int) -> tuple[float, int, int] | None:
    """The largest rise from a pressure low to a peak within SWING_HOURS after it, for peaks in
    [peak_from, peak_to): (rise, low index, peak index)."""
    best = None
    for k in range(max(peak_from, 1), peak_to):
        peak = hours[k].pressure_hpa
        if peak is None:
            continue
        lows = [
            (hours[i].pressure_hpa, i)
            for i in range(max(0, k - SWING_HOURS), k)
            if hours[i].pressure_hpa is not None
        ]
        if not lows:
            continue
        low, i = min(lows)
        rise = peak - low
        if best is None or rise > best[0]:
            best = (rise, i, k)
    return best


def _index_range(hours: list[Hour], start: datetime, end: datetime) -> tuple[int, int]:
    first = next((i for i, h in enumerate(hours) if h.time >= start), len(hours))
    last = next((i for i, h in enumerate(hours) if h.time >= end), len(hours))
    return first, last


def detect_day(
    hours: list[Hour],
    start: datetime,
    end: datetime,
    thresholds: Thresholds,
    now: datetime | None = None,
) -> list[Trigger]:
    """The triggers that land between `start` and `end` (UTC). Changes are measured only from `now` on:
    what already happened isn't a warning (a swing may still start from a recent low)."""
    lo, hi = _index_range(hours, start, end)
    if lo >= hi:
        return []
    first, _ = _index_range(hours, now or hours[0].time, end)
    found: list[Trigger] = []

    def add(kind: str, change: tuple[float, int, int] | None, attr: str, limit: float) -> None:
        if change is not None and change[0] >= limit:
            amount, i, j = change
            found.append(
                Trigger(
                    kind,
                    amount,
                    hours[i].time,
                    hours[j].time,
                    getattr(hours[i], attr),
                    getattr(hours[j], attr),
                )
            )

    add(
        "pressure_drop",
        _largest_change(hours, "pressure_hpa", True, lo, hi, first),
        "pressure_hpa",
        thresholds.pressure_drop_hpa,
    )
    add(
        "pressure_rise",
        _largest_change(hours, "pressure_hpa", False, lo, hi, first),
        "pressure_hpa",
        thresholds.pressure_rise_hpa,
    )
    add(
        "temp_drop",
        _largest_change(hours, "temp_f", True, lo, hi, first),
        "temp_f",
        thresholds.temp_drop_f,
    )
    add(
        "dewpoint_drop",
        _largest_change(hours, "dew_point_f", True, lo, hi, first),
        "dew_point_f",
        thresholds.dewpoint_drop_f,
    )
    add("swing", _swing(hours, lo, hi), "pressure_hpa", thresholds.swing_rise_hpa)

    drop = next((t for t in found if t.kind == "pressure_drop"), None)
    if drop is not None:
        rainy = [
            h
            for h in hours
            if max(drop.start, now or drop.start) <= h.time <= drop.end + timedelta(hours=WINDOW)
            and (h.rain_chance or 0) >= thresholds.rain_chance
        ]
        if rainy:
            peak = max(h.rain_chance or 0 for h in rainy)
            found.append(Trigger("rain", peak, rainy[0].time, rainy[0].time))
    return found


THRESHOLD_FOR = {
    "pressure_drop": "pressure_drop_hpa",
    "pressure_rise": "pressure_rise_hpa",
    "temp_drop": "temp_drop_f",
    "dewpoint_drop": "dewpoint_drop_f",
    "rain": "rain_chance",
    "swing": "swing_rise_hpa",
}


def score(
    triggers: list[Trigger], thresholds: Thresholds, weights: dict[str, float] | None = None
) -> int:
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    total = 0.0
    for t in triggers:
        limit = getattr(thresholds, THRESHOLD_FOR[t.kind])
        total += weights.get(t.kind, 0.0) * min(1.5, t.amount / limit if limit else 1.0)
    return min(100, round(total))


def _clock(moment: datetime, zone: tzinfo) -> str:
    local = moment.astimezone(zone)
    return local.strftime("%I %p").lstrip("0").replace(":00", "")


def _when(moment: datetime, zone: tzinfo) -> str:
    local = moment.astimezone(zone)
    hour = local.hour
    if hour < 5:
        return f"early {local:%A}"
    part = (
        "morning"
        if hour < 12
        else "afternoon"
        if hour < 17
        else "evening"
        if hour < 21
        else "night"
    )
    return f"{local:%A} {part}"


def describe(trigger: Trigger, zone: tzinfo) -> str:
    """One trigger in plain words, e.g. "pressure drops 22 hPa (1018 → 996) by Saturday night"."""
    by = f"by {_when(trigger.end, zone)} ({_clock(trigger.end, zone)})"
    span = f"({trigger.before:.0f} → {trigger.after:.0f})" if trigger.before is not None else ""
    match trigger.kind:
        case "pressure_drop":
            return f"pressure drops {trigger.amount:.0f} hPa {span} {by}"
        case "pressure_rise":
            return f"pressure rises {trigger.amount:.0f} hPa {span} {by}"
        case "temp_drop":
            return f"temperature falls {trigger.amount:.0f}°F {span.replace(')', '°F)')} {by}"
        case "dewpoint_drop":
            return f"dew point falls {trigger.amount:.0f}°F (drier air) {by}"
        case "rain":
            return f"rain likely ({trigger.amount:.0f}%) from {_when(trigger.end, zone)}"
        case "swing":
            return (
                f"a fast pressure swing: up {trigger.amount:.0f} hPa from a low, peaking "
                f"{_when(trigger.end, zone)} ({_clock(trigger.end, zone)})"
            )
    return trigger.kind


ORDER = ["swing", "pressure_drop", "temp_drop", "dewpoint_drop", "rain", "pressure_rise"]


def outlook(
    hours: list[Hour],
    now: datetime,
    zone: tzinfo,
    thresholds: Thresholds | None = None,
    weights: dict[str, float] | None = None,
    days: int = DAYS,
) -> list[DayRisk]:
    """The risk for today and the next days (local calendar days), from what's still ahead of `now`."""
    thresholds = thresholds or Thresholds()
    today = now.astimezone(zone).date()
    result = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        day_start = datetime.combine(day, datetime.min.time(), zone)
        start = max(now, day_start)
        end = day_start + timedelta(days=1)
        triggers = sorted(
            detect_day(hours, start, end, thresholds, now), key=lambda t: ORDER.index(t.kind)
        )
        risk = DayRisk(day, score(triggers, thresholds, weights), triggers)
        name = "Today" if offset == 0 else "Tomorrow" if offset == 1 else f"{day:%A}"
        risk.summary = summarize(risk, name, zone)
        result.append(risk)
    return result


def summarize(risk: DayRisk, name: str, zone: tzinfo) -> str:
    """ "Sunday: 72/100. Pressure drops 22 hPa (1018 → 996) by Sunday night (9 PM); rain likely ..."."""
    if not risk.triggers:
        return f"{name}: {risk.score}/100. No big shifts."
    details = "; ".join(describe(t, zone) for t in risk.triggers)
    return f"{name}: {risk.score}/100. {details[0].upper()}{details[1:]}."


SPOKEN = {
    "swing": "pressure swings up {amount:.0f} hectopascals, peaking {when}",
    "pressure_drop": "pressure drops {amount:.0f} hectopascals by {when}",
    "temp_drop": "the temperature falls {amount:.0f} degrees by {when}",
    "dewpoint_drop": "the air turns a lot drier by {when}",
    "rain": "rain is likely from {when}",
    "pressure_rise": "pressure climbs {amount:.0f} hectopascals by {when}",
}


def spoken(risk: DayRisk, zone: tzinfo) -> str:
    """The riskiest day in a sentence for the voice listener: no symbols, at most two reasons."""
    reasons = [
        SPOKEN[t.kind].format(amount=t.amount, when=_when(t.end, zone)) for t in risk.triggers[:2]
    ]
    joined = " and ".join(reasons) if reasons else "no single big change"
    return f"{risk.day:%A} scores {risk.score} out of 100: {joined}."
