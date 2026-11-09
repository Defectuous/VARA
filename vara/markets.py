"""Market quotes for chat: when a message names an index, a cryptocurrency, or a ticker, V.A.R.A. fetches
the actual figures and gives them to the model.

Web-search snippets rarely contain the number itself, and a model asked for one without it will invent it.
Quotes come from Yahoo Finance's public chart data (no account or key; delayed like any free quote). Only the
ticker symbol leaves this PC, as with search words.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("vara.markets")

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
MAX_SYMBOLS = 4

# Names people say -> Yahoo symbols. Checked in order; the first pattern that matches each name wins.
NAMED = [
    (r"s\s?&\s?p(\s?500)?|s\s?and\s?p(\s?500)?|spx", "^GSPC", "S&P 500"),
    (r"nasdaq(\s+composite)?", "^IXIC", "NASDAQ Composite"),
    (r"nasdaq[\s-]?100", "^NDX", "Nasdaq-100"),
    (r"dow(\s+jones)?(\s+industrial(\s+average)?)?|djia", "^DJI", "Dow Jones Industrial Average"),
    (r"russell(\s+2000)?", "^RUT", "Russell 2000"),
    (r"vix|volatility\s+index", "^VIX", "CBOE Volatility Index (VIX)"),
    (r"bitcoin|btc", "BTC-USD", "Bitcoin (USD)"),
    (r"ethereum|eth\b|ether\b", "ETH-USD", "Ethereum (USD)"),
    (r"gold(\s+price)?", "GC=F", "Gold futures (USD/oz)"),
    (r"(crude\s+)?oil(\s+price)?|wti", "CL=F", "WTI crude oil futures (USD/barrel)"),
]
_NAMED = [(re.compile(rf"\b(?:{p})\b", re.IGNORECASE), s, n) for p, s, n in NAMED]
# "$AAPL", or "AAPL stock/shares/price/ticker" (capitals only, so ordinary words don't count).
_TICKER = re.compile(
    r"\$([A-Z]{1,5})\b|\b([A-Z]{1,5})(?:'s)?\s+(?:stock|shares|share\s+price|price|ticker)\b"
)
# A broad market question with no index named gets the three main US indexes.
_BROAD = re.compile(r"\b(stock\s+market|the\s+market|markets|wall\s+street)\b", re.IGNORECASE)
_NASDAQ_100 = re.compile(r"\bnasdaq[\s-]?100\b", re.IGNORECASE)


def find_symbols(text: str) -> list[tuple[str, str]]:
    """(symbol, display name) for each market the text mentions, in order, without repeats."""
    found: list[tuple[int, str, str]] = []
    for pattern, symbol, name in _NAMED:
        if symbol == "^IXIC" and _NASDAQ_100.search(text):
            continue  # "Nasdaq-100" is the other index
        if match := pattern.search(text):
            found.append((match.start(), symbol, name))
    for match in _TICKER.finditer(text):
        ticker = match.group(1) or match.group(2)
        if ticker not in {"I", "A", "THE", "US", "USA", "AI"}:
            found.append((match.start(), ticker, ticker))
    if not found and _BROAD.search(text):
        found = [
            (0, "^GSPC", "S&P 500"),
            (1, "^IXIC", "NASDAQ Composite"),
            (2, "^DJI", "Dow Jones"),
        ]
    result: list[tuple[str, str]] = []
    for _, symbol, name in sorted(found):
        if symbol not in {s for s, _ in result}:
            result.append((symbol, name))
    return result[:MAX_SYMBOLS]


@dataclass
class Quote:
    symbol: str
    name: str
    price: float
    previous_close: float | None
    currency: str
    as_of: datetime  # in the exchange's time zone
    exchange: str

    @property
    def change(self) -> float | None:
        return None if self.previous_close is None else self.price - self.previous_close

    @property
    def change_percent(self) -> float | None:
        if self.previous_close in (None, 0):
            return None
        return (self.price - self.previous_close) / self.previous_close * 100

    def line(self) -> str:
        t = self.as_of  # written out by hand: %-d / %-I aren't supported on Windows
        hour = t.hour % 12 or 12
        when = (
            f"{t:%A %B} {t.day}, {t.year} at {hour}:{t:%M} {'AM' if t.hour < 12 else 'PM'} {t:%Z}"
        )
        unit = (
            "points" if self.symbol.startswith("^") else self.currency
        )  # indexes aren't priced in $
        text = f"- {self.name} ({self.symbol}): {self.price:,.2f} {unit}"
        if self.change is not None:
            text += f", {self.change:+,.2f} ({self.change_percent:+.2f}%) from the previous close"
            text += f" of {self.previous_close:,.2f}"
        return f"{text}; last price {when} ({self.exchange})"


def parse_chart(data: dict, symbol: str, name: str) -> Quote:
    result = data["chart"]["result"][0]
    meta = result["meta"]
    zone = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
    as_of = datetime.fromtimestamp(meta["regularMarketTime"], UTC).astimezone(zone)
    # Daily closes; the last bar is the current session when it's the same day as the last price.
    closes = [
        (datetime.fromtimestamp(t, UTC).astimezone(zone).date(), c)
        for t, c in zip(
            result.get("timestamp", []),
            result["indicators"]["quote"][0].get("close", []),
            strict=False,
        )
        if c is not None
    ]
    earlier = [c for day, c in closes if day < as_of.date()]
    return Quote(
        symbol=symbol,
        name=meta.get("longName") if name == symbol and meta.get("longName") else name,
        price=float(meta["regularMarketPrice"]),
        previous_close=earlier[-1] if earlier else None,
        currency=meta.get("currency") or "",
        as_of=as_of,
        exchange=meta.get("fullExchangeName") or meta.get("exchangeName") or "",
    )


class MarketData:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=8, headers={"User-Agent": "Mozilla/5.0 (V.A.R.A. personal assistant)"}
        )

    async def quote(self, symbol: str, name: str) -> Quote | None:
        try:
            response = await self._client.get(
                CHART_URL.format(symbol=symbol), params={"range": "5d", "interval": "1d"}
            )
            response.raise_for_status()
            return parse_chart(response.json(), symbol, name)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("No quote for %s: %s", symbol, exc)
            return None

    async def quotes(self, symbols: list[tuple[str, str]]) -> list[Quote]:
        found = await asyncio.gather(*(self.quote(s, n) for s, n in symbols))
        return [q for q in found if q is not None]


def market_context(quotes: list[Quote], retrieved: str) -> str:
    intro = (
        f"Market data from Yahoo Finance (retrieved {retrieved}; free quotes can be delayed). These are "
        "the actual figures: use them exactly, say when the price is from (a close on an earlier day "
        "means the market is closed now), and name Yahoo Finance as the source."
    )
    lines = [intro, *(q.line() for q in quotes)]
    return "\n".join(lines)
