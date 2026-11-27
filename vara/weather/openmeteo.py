"""Open-Meteo (https://open-meteo.com): free forecasts and historical weather, no key.

Only the configured latitude and longitude leave this PC. Every request has a timeout and is retried with
backoff; a failure raises WeatherUnavailable so the caller can carry on without it.

Checked 2026-11-27 for Columbus, OH: the forecast API returns 7 days of hourly values (plus past hours with
`past_days`), and the archive API has the same variables for past dates (no precipitation probability,
since that's a forecast-only value). The air-quality API has PM2.5 and US AQI here, but its pollen fields
are Europe-only and come back empty.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime

import httpx

log = logging.getLogger("vara.weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = (
    "pressure_msl",
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "precipitation_probability",
    "precipitation",
    "wind_speed_10m",
    "wind_direction_10m",
)
UNITS = {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch"}
TIMEOUT = 20.0
ATTEMPTS = 3


class WeatherUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Hour:
    time: datetime  # UTC, aware
    pressure_hpa: float | None
    temp_f: float | None
    humidity: float | None
    dew_point_f: float | None
    rain_chance: float | None  # percent; None in the archive
    rain_in: float | None
    wind_mph: float | None
    wind_dir: float | None

    def to_dict(self) -> dict:
        return {**asdict(self), "time": self.time.isoformat()}

    @classmethod
    def from_dict(cls, data: dict) -> Hour:
        return cls(**{**data, "time": datetime.fromisoformat(data["time"])})


def parse_hourly(payload: dict) -> list[Hour]:
    """Open-Meteo's column arrays as one Hour per row. Times must be UTC (request timezone=UTC)."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []

    def column(name: str) -> list:
        values = hourly.get(name)
        return values if values is not None else [None] * len(times)

    cols = [column(name) for name in HOURLY]
    return [
        Hour(datetime.fromisoformat(t).replace(tzinfo=UTC), *(c[i] for c in cols))
        for i, t in enumerate(times)
    ]


class OpenMeteo:
    def __init__(
        self, latitude: float, longitude: float, client: httpx.AsyncClient | None = None
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self._client = client

    async def _get(self, url: str, params: dict) -> dict:
        params = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": "UTC",
            **UNITS,
            **params,
        }
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            try:
                if self._client is not None:
                    response = await self._client.get(url, params=params, timeout=TIMEOUT)
                else:
                    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                        response = await client.get(url, params=params)
                if response.status_code == 400:  # a bad request won't get better on retry
                    raise WeatherUnavailable(
                        f"Open-Meteo refused the request: {response.text[:200]}"
                    )
                response.raise_for_status()
                return response.json()
            except WeatherUnavailable:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                log.warning("Open-Meteo attempt %d failed: %s", attempt + 1, exc)
                if attempt + 1 < ATTEMPTS:
                    await asyncio.sleep(2 ** (attempt + 1))
        raise WeatherUnavailable(f"Open-Meteo didn't answer: {last}")

    async def forecast(self, days: int = 7, past_days: int = 1) -> list[Hour]:
        """Hourly forecast for `days`, plus the last `past_days` (what actually happened, as modelled)."""
        payload = await self._get(
            FORECAST_URL,
            {"hourly": ",".join(HOURLY), "forecast_days": days, "past_days": past_days},
        )
        return parse_hourly(payload)

    async def archive(self, start: date, end: date) -> list[Hour]:
        hourly = [h for h in HOURLY if h != "precipitation_probability"]
        payload = await self._get(
            ARCHIVE_URL,
            {
                "hourly": ",".join(hourly),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )
        return parse_hourly(payload)
