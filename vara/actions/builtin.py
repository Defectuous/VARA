"""The built-in actions. They wrap features V.A.R.A. already has (web search, market quotes, price checks,
memory), so the agent loop starts with tools that are already proven."""

from dataclasses import asdict

from pydantic import Field

from vara import price_watch, store
from vara.actions.base import (
    Action,
    ActionContext,
    ActionFailed,
    ActionRegistry,
    ActionResult,
    NoParams,
    Params,
    Risk,
)
from vara.deals import deal_context
from vara.markets import find_symbols, market_context
from vara.search import planner
from vara.search.client import SearchFailed, search_context


def _clock(ctx: ActionContext, template: str) -> str:
    from vara.api import fill_clock  # imported late: vara.api must never depend on the action layer

    return fill_clock(template, ctx.now, ctx.timezone)


def _retrieved(ctx: ActionContext) -> str:
    return _clock(ctx, "{date} at {time} ({timezone})")


async def current_time(ctx: ActionContext, _params: NoParams) -> ActionResult:
    return ActionResult(_clock(ctx, "It is {time} on {date} ({timezone})."))


class MemoryQuery(Params):
    query: str = Field(min_length=1, max_length=300, description="What to look for")


async def search_memory(ctx: ActionContext, params: MemoryQuery) -> ActionResult:
    with ctx.db.session() as session:
        facts = [m.content for m in store.retrieve_memories(session, params.query, limit=8)]
        found = store.search(session, params.query, limit=8)
    lines = []
    if facts:
        lines.append("Facts the user asked V.A.R.A. to remember:")
        lines += [f"- {f}" for f in facts]
    if found["messages"]:
        lines.append("Matching lines from past conversations:")
        lines += [
            f"- ({m['created_at'][:10]}, {m['role']}) {m['snippet']}" for m in found["messages"]
        ]
    if not lines:
        return ActionResult(f'Nothing in memory or past conversations matches "{params.query}".')
    return ActionResult(
        "\n".join(lines), {"memories": len(facts), "messages": len(found["messages"])}
    )


class WebQuery(Params):
    query: str = Field(
        min_length=1, max_length=200, description="Search words, as for a search engine"
    )


async def web_search(ctx: ActionContext, params: WebQuery) -> ActionResult:
    # Not the model's choice: qwen3 picked the news engines for nearly everything recent ("Intel CEO"),
    # and those return headlines, not answers. Like chat's fixed steps, only a query about news,
    # headlines, or announcements searches the news engines.
    category = planner.category_for(params.query)
    try:
        found = await ctx.web.search(params.query, category=category)
    except SearchFailed as exc:
        raise ActionFailed(str(exc)) from exc
    return ActionResult(
        search_context(found, _retrieved(ctx)),
        {
            "query": found.query,
            "urls": [r.url for r in found.results],
            "results": [
                {"title": r.title, "url": r.url, "engines": r.engines} for r in found.results
            ],
            "unresponsive_engines": list(found.unresponsive),
        },
    )


class MarketQuery(Params):
    markets: str = Field(
        min_length=1,
        max_length=200,
        description='Indexes, cryptocurrencies, or tickers, e.g. "S&P 500, bitcoin, AAPL"',
    )


async def market_quotes(ctx: ActionContext, params: MarketQuery) -> ActionResult:
    symbols = find_symbols(params.markets)
    if not symbols:  # bare tickers the pattern didn't catch, e.g. "nvda"
        tickers = [t.strip().upper() for t in params.markets.split(",") if t.strip()]
        symbols = [(t, t) for t in tickers[:5]]
    quotes = await ctx.markets.quotes(symbols)
    if not quotes:
        raise ActionFailed(f"No quotes found for {params.markets}")
    return ActionResult(
        market_context(quotes, _retrieved(ctx)),
        {
            "symbols": [q.symbol for q in quotes],
            "quotes": [
                {"symbol": q.symbol, "name": q.name, "price": q.price, "as_of": q.as_of.isoformat()}
                for q in quotes
            ],
        },
    )


class Product(Params):
    product: str = Field(
        min_length=2, max_length=120, description="The product, as specific as possible"
    )


async def find_prices(ctx: ActionContext, params: Product) -> ActionResult:
    report = await ctx.deals.find(params.product)
    return ActionResult(
        deal_context(report, _retrieved(ctx)),
        {
            "query": report.query,
            "offers": [asdict(o) for o in report.offers[:8]],
            "checked": report.checked,
            "failed": report.failed,
        },
    )


async def list_price_watches(ctx: ActionContext, _params: NoParams) -> ActionResult:
    with ctx.db.session() as session:
        return ActionResult(price_watch.watches_context(price_watch.active_watches(session)))


class WatchRequest(Product):
    target_price: float | None = Field(
        default=None,
        gt=0,
        description="Alert at or below this price in dollars; omit to alert on any 5% drop",
    )


def _describe_watch(params: WatchRequest) -> str:
    rule = (
        f"alert at or below ${params.target_price:,.2f}"
        if params.target_price
        else "alert when it drops 5% below the lowest price seen"
    )
    return f'Start watching the price of "{params.product}" every 6 hours ({rule}).'


async def watch_price(ctx: ActionContext, params: WatchRequest) -> ActionResult:
    with ctx.db.session() as session:
        watch = price_watch.add_watch(session, params.product, params.target_price)
        return ActionResult(
            f'Now watching "{watch.query}". {_describe_watch(params)}', {"watch_id": watch.id}
        )


async def stop_price_watch(ctx: ActionContext, params: Product) -> ActionResult:
    with ctx.db.session() as session:
        stopped = [w.query for w in price_watch.stop_watches(session, params.product)]
    if not stopped:
        return ActionResult(f'No price watch matched "{params.product}"; nothing was stopped.')
    return ActionResult(f"Stopped watching: {', '.join(stopped)}.")


async def weather_outlook(ctx: ActionContext, _params: NoParams) -> ActionResult:
    from datetime import UTC, datetime, timedelta

    from vara.weather.openmeteo import WeatherUnavailable

    watch = ctx.weather
    outlook = watch.latest_outlook()
    fetched = datetime.fromisoformat(outlook["fetched_at"]) if outlook else None
    if fetched is None or datetime.now(UTC) - fetched > timedelta(hours=6):
        try:
            outlook = await watch.check()
        except WeatherUnavailable as exc:
            if outlook is None:
                raise ActionFailed(str(exc)) from exc
    header = (
        f"Weather outlook for {outlook['place']} (Open-Meteo forecast, fetched "
        f"{outlook['fetched_at'][:16].replace('T', ' ')} UTC). Risk is for the shifts that tend to "
        "set off the user's sinus trouble: 0 means calm, 60 or more gets an alert."
    )
    lines = [header]
    for day in outlook["days"]:
        weather = ", ".join(
            part
            for part in (
                f"high {day['high_f']}°F" if day["high_f"] is not None else "",
                f"low {day['low_f']}°F" if day["low_f"] is not None else "",
                f"rain chance up to {day['rain_chance']}%"
                if day["rain_chance"] is not None
                else "",
            )
            if part
        )
        lines.append(f"- {day['date']}: {day['summary']} ({weather})")
    if outlook.get("stale"):
        lines.append(f"(This is from an older forecast: {outlook.get('error')})")
    return ActionResult("\n".join(lines), {"days": [d["score"] for d in outlook["days"]]})


BUILTIN_ACTIONS = [
    Action(
        "current_time",
        "The current date, time, and time zone where the user is.",
        NoParams,
        Risk.READ_ONLY,
        current_time,
        describe=lambda _p: "Read the clock.",
    ),
    Action(
        "search_memory",
        "Search the facts the user asked V.A.R.A. to remember, and past conversations.",
        MemoryQuery,
        Risk.READ_ONLY,
        search_memory,
        describe=lambda p: f'Search memory and past conversations for "{p.query}".',
    ),
    Action(
        "web_search",
        "Search the web for current information: news, facts that change, who holds an office, "
        "events, scores, weather. Only the search words leave this PC.",
        WebQuery,
        Risk.READ_ONLY,
        web_search,
        describe=lambda p: f'Search the web for "{p.query}".',
        needs="web",
    ),
    Action(
        "market_quotes",
        "Current prices of stock indexes, cryptocurrencies, and stock tickers (Yahoo Finance).",
        MarketQuery,
        Risk.READ_ONLY,
        market_quotes,
        describe=lambda p: f"Look up market prices for {p.markets}.",
        needs="markets",
    ),
    Action(
        "find_prices",
        "Find where a product is cheapest right now (deal posts and store pages). Never buys anything.",
        Product,
        Risk.READ_ONLY,
        find_prices,
        describe=lambda p: f'Check prices for "{p.product}".',
        timeout=60.0,
        needs="deals",
    ),
    Action(
        "weather_outlook",
        "The weather where the user lives for today and the next two days: highs, lows, rain, and a "
        "0-100 risk score for the shifts that set off their sinus trouble (cold fronts, pressure swings, "
        "big temperature and dew-point drops). Use it for any weather question about their area.",
        NoParams,
        Risk.READ_ONLY,
        weather_outlook,
        describe=lambda _p: "Check the weather outlook.",
        needs="weather",
    ),
    Action(
        "list_price_watches",
        "List the products V.A.R.A. is watching for price drops.",
        NoParams,
        Risk.READ_ONLY,
        list_price_watches,
        describe=lambda _p: "List the price watches.",
    ),
    Action(
        "watch_price",
        "Start watching a product's price and alert the user when it drops. The user approves it first.",
        WatchRequest,
        Risk.LOCAL_CHANGE,
        watch_price,
        describe=_describe_watch,
    ),
    Action(
        "stop_price_watch",
        "Stop watching a product's price. The user approves it first.",
        Product,
        Risk.LOCAL_CHANGE,
        stop_price_watch,
        describe=lambda p: f'Stop watching the price of "{p.product}".',
    ),
]


def default_registry() -> ActionRegistry:
    return ActionRegistry(list(BUILTIN_ACTIONS))
