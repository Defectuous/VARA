import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from tests.test_search import FakeWeb, PlanningProvider, web_client
from vara.markets import MarketData, Quote, find_symbols, market_context, parse_chart


@pytest.mark.parametrize(
    ("text", "symbols"),
    [
        ("can you check the S&P 500 and NASDAQ?", ["^GSPC", "^IXIC"]),
        ("how did the s and p do", ["^GSPC"]),
        ("what's the Nasdaq-100 at", ["^NDX"]),
        ("Dow Jones today", ["^DJI"]),
        ("is the stock market up?", ["^GSPC", "^IXIC", "^DJI"]),
        ("what's bitcoin worth", ["BTC-USD"]),
        ("how is $NVDA doing", ["NVDA"]),
        ("what's the AAPL stock price", ["AAPL"]),
        ("write me a poem about the sea", []),
        ("what is 12 times 7", []),
        ("I think A price is fair", []),
    ],
)
def test_markets_named_in_a_message_are_found(text, symbols):
    assert [s for s, _ in find_symbols(text)] == symbols


def chart(price, closes, last_time, zone="America/New_York"):
    """A Yahoo chart response: daily bars at 9:30 local time, and the last price at `last_time`."""
    tz = ZoneInfo(zone)
    stamps = [int(datetime(2026, 11, d, 9, 30, tzinfo=tz).timestamp()) for d, _ in closes]
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": price,
                        "regularMarketTime": int(last_time.timestamp()),
                        "currency": "USD",
                        "exchangeTimezoneName": zone,
                        "fullExchangeName": "Nasdaq GIDS",
                    },
                    "timestamp": stamps,
                    "indicators": {"quote": [{"close": [c for _, c in closes]}]},
                }
            ]
        }
    }


def test_the_change_is_from_the_previous_day_close():
    close_friday = datetime(2026, 11, 6, 17, 15, tzinfo=ZoneInfo("America/New_York"))
    data = chart(24579.42, [(4, 25100.0), (5, 25437.0), (6, 24579.42)], close_friday)

    quote = parse_chart(data, "^IXIC", "NASDAQ Composite")

    assert quote.price == 24579.42
    assert quote.previous_close == 25437.0
    assert round(quote.change_percent, 2) == -3.37
    line = quote.line()
    assert "NASDAQ Composite (^IXIC): 24,579.42 points, -857.58 (-3.37%)" in line
    assert "Friday November 6, 2026 at 5:15 PM EST" in line


def test_market_context_tells_the_model_to_use_the_figures_exactly():
    quote = Quote(
        "^GSPC", "S&P 500", 7376.86, 7353.73, "USD", datetime(2026, 11, 6, tzinfo=UTC), "SNP"
    )
    context = market_context([quote], "Sunday, November 8, 2026")
    assert "use them exactly" in context
    assert "S&P 500 (^GSPC): 7,376.86 points, +23.13 (+0.31%)" in context


def test_a_failed_quote_is_skipped():
    def handler(request):
        if "IXIC" in request.url.path:
            return httpx.Response(404, json={"chart": {"result": None}})
        close = datetime(2026, 11, 6, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        return httpx.Response(200, json=chart(7376.86, [(5, 7353.73), (6, 7376.86)], close))

    markets = MarketData(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    quotes = asyncio.run(markets.quotes([("^GSPC", "S&P 500"), ("^IXIC", "NASDAQ Composite")]))
    assert [q.symbol for q in quotes] == ["^GSPC"]


class FakeMarkets:
    def __init__(self):
        self.asked: list[list[tuple[str, str]]] = []

    async def quotes(self, symbols):
        self.asked.append(symbols)
        when = datetime(2026, 11, 6, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        return [Quote(s, n, 100.0, 99.0, "USD", when, "X") for s, n in symbols]


def test_chat_gives_the_model_real_quotes_for_a_market_question():
    provider, web, markets = PlanningProvider("NO_SEARCH"), FakeWeb(), FakeMarkets()
    client = web_client(provider, web)
    client.app.state.markets = markets

    response = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "what did the NASDAQ close at?"}]},
    )

    assert markets.asked == [[("^IXIC", "NASDAQ Composite")]]
    assert "NASDAQ Composite (^IXIC): 100.00 points" in provider.systems[-1]
    assert response.json()["market_data"][0]["symbol"] == "^IXIC"


def test_a_follow_up_uses_the_markets_from_the_previous_question():
    provider, web, markets = PlanningProvider("NO_SEARCH"), FakeWeb(), FakeMarkets()
    client = web_client(provider, web)
    client.app.state.markets = markets
    first = client.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": "how is the Dow doing?"}]}
    ).json()

    client.post(
        "/v1/chat",
        json={
            "conversation_id": first["conversation_id"],
            "messages": [{"role": "user", "content": "and what was the change?"}],
        },
    )

    assert markets.asked[-1] == [("^DJI", "Dow Jones Industrial Average")]


def test_no_quotes_for_ordinary_messages():
    provider, web, markets = PlanningProvider("NO_SEARCH"), FakeWeb(), FakeMarkets()
    client = web_client(provider, web)
    client.app.state.markets = markets

    response = client.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": "write a haiku"}]}
    )

    assert markets.asked == []
    assert response.json()["market_data"] is None
