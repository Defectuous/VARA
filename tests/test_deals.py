import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_search import FakeWeb, PlanningProvider, web_client
from vara import price_watch
from vara.config import Settings
from vara.db.database import Database
from vara.db.models import PriceWatch
from vara.deals import (
    DealFinder,
    DealReport,
    DealRequest,
    Offer,
    RobotsCache,
    deal_context,
    parse_product_page,
    parse_request,
    parse_slickdeals,
    relevant,
)
from vara.search.client import WebResult, WebSearch

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


# ---------------------------------------------------------------- understanding the request


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "find me the best deal on a Samsung 990 Pro 2TB",
            DealRequest("find", "Samsung 990 Pro 2TB"),
        ),
        (
            "where can I buy a Logitech MX Master 3S cheapest?",
            DealRequest("find", "Logitech MX Master 3S"),
        ),
        ("any deals on a 27 inch 4k monitor?", DealRequest("find", "27 inch 4k monitor")),
        ("is the Steam Deck OLED on sale", DealRequest("find", "Steam Deck OLED")),
        (
            "tell me when the AirPods Pro 3 drop below $170",
            DealRequest("watch", "AirPods Pro 3", 170.0),
        ),
        ("watch the price of a Dyson V15 Detect", DealRequest("watch", "Dyson V15 Detect")),
        ("track the price of a PS5 Pro under $600", DealRequest("watch", "PS5 Pro", 600.0)),
        ("what prices are you watching?", DealRequest("list_watches")),
        ("stop watching the AirPods", DealRequest("stop_watch", "AirPods")),
    ],
)
def test_buying_requests_are_understood(text, expected):
    assert parse_request(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "let's watch a movie tonight",
        "what's the price of bitcoin",
        "write a haiku",
        "how is the Dow",
    ],
)
def test_other_messages_are_not_buying_requests(text):
    assert parse_request(text) is None


@pytest.mark.parametrize(
    ("query", "title", "match"),
    [
        ("Samsung 990 Pro 2TB", "2TB Samsung 990 PRO PCIe Gen4 M.2 SSD $150", True),
        ("Samsung 990 Pro 2TB", "2TB Samsung 9100 PRO M.2 PCIe 5.0 SSD $190", False),
        ("Samsung 990 Pro 2TB", "4TB Samsung 990 PRO SSD $260", False),
        ("Samsung 990 Pro 2TB", "Samsung 990 Pro 2 TB NVMe", True),
        ("AirPods Pro 2", "Apple AirPods Pro 3 + 2-Yr AppleCare+ $200", False),
        ("AirPods Pro 3", "Apple AirPods Pro 3 Wireless Earbuds $179", True),
        ("Logitech MX Master 3S", "MX Master 3S Bluetooth Edition", True),
        ("Dyson V15 Detect", "Dyson V12 Detect Slim", False),
    ],
)
def test_offers_must_be_the_thing_asked_for(query, title, match):
    assert relevant(query, title) is match


# ---------------------------------------------------------------- reading the sources


def feed(*items: tuple[str, str, datetime, str, int]) -> str:
    """Slickdeals RSS with (title, description, posted, link slug, votes) items."""
    xml = []
    for title, description, posted, slug, votes in items:
        xml.append(
            f"<item><title>{title}</title><link>https://slickdeals.net/f/1-{slug}?utm_source=rss</link>"
            f"<description><![CDATA[{description}]]></description>"
            f"<content:encoded><![CDATA[<div>Thumb Score: {votes:+d} </div>]]></content:encoded>"
            f"<pubDate>{format_datetime(posted)}</pubDate></item>"
        )
    return (
        '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>'
        + "".join(xml)
        + "</channel></rss>"
    )


def test_deal_posts_give_store_exact_price_date_and_votes():
    text = feed(
        (
            "2TB Samsung 990 PRO SSD $150 + Free Shipping",
            "Amazon has *2TB Samsung 990 PRO* on sale for *$149.99*. List price $229.99.",
            NOW - timedelta(days=2),
            "990-pro-at-amazon",
            45,
        )
    )
    [offer] = parse_slickdeals(text, "Samsung 990 Pro 2TB", NOW, vetted=True)
    assert (offer.store, offer.price, offer.posted, offer.votes) == (
        "Amazon",
        149.99,
        "2026-11-06",
        45,
    )
    assert offer.source == "front-page deal"
    assert offer.url == "https://slickdeals.net/f/1-990-pro-at-amazon"


def test_old_refurbished_question_and_downvoted_posts_are_dropped():
    text = feed(
        ("Samsung 990 Pro 2TB $120", "Newegg has it for $120", NOW - timedelta(days=60), "a", 10),
        ("Refurbished Samsung 990 Pro 2TB $99", "eBay has it for $99", NOW, "b", 10),
        ("Is the Samsung 990 Pro 2TB worth $140?", "", NOW, "c", 3),
        ("Samsung 990 Pro 2TB $80", "Someone has it for $80", NOW, "d", -6),
    )
    assert parse_slickdeals(text, "Samsung 990 Pro 2TB", NOW) == []


def test_the_store_is_found_in_the_title_when_the_description_does_not_say():
    text = feed(
        ("Costco Members: Apple AirPods Pro 3 $200", "Great deal.", NOW, "x", 5),
        ("Apple AirPods Pro 3 $179 at Walmart", "Great deal.", NOW, "y", 5),
        ("Apple AirPods Pro 3 $185", "Great deal.", NOW, "airpods-pro-3-at-best-buy", 5),
    )
    stores = [o.store for o in parse_slickdeals(text, "AirPods Pro 3", NOW)]
    assert stores == ["Costco", "Walmart", "Best Buy"]


PRODUCT_PAGE = """<html><script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product", "name": "MX Master 3S Wireless Mouse",
 "offers": {"@type": "Offer", "price": "89.99", "priceCurrency": "USD",
            "availability": "https://schema.org/InStock"}}
</script></html>"""


def test_a_store_page_price_is_read_from_its_structured_data():
    offer = parse_product_page(PRODUCT_PAGE, "https://www.logitech.com/mx", "Logitech MX Master 3S")
    assert (offer.price, offer.store, offer.in_stock) == (89.99, "logitech.com", True)
    # A different product on the page isn't reported as the one asked for.
    assert parse_product_page(PRODUCT_PAGE, "https://x.example", "MX Anywhere 3") is None


def test_pages_whose_robots_rules_forbid_readers_are_not_fetched():
    fetched = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if request.url.path == "/robots.txt":
            body = "User-agent: *\nDisallow: /" if request.url.host == "shop.example" else ""
            return httpx.Response(200, text=body)
        return httpx.Response(200, text=PRODUCT_PAGE, headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    class Web:
        async def search(self, query, category="general", limit=6):
            return WebSearch(
                query,
                [
                    WebResult("Shop", "https://shop.example/mx", "", ["bing"]),
                    WebResult("Logitech", "https://logi.example/mx", "", ["bing"]),
                ],
            )

    offers = asyncio.run(DealFinder(Web(), client).store_pages("Logitech MX Master 3S"))

    assert [o.store for o in offers] == ["logi.example"]
    assert "https://shop.example/mx" not in fetched
    assert asyncio.run(RobotsCache(client).allowed("https://shop.example/anything")) is False


def test_the_context_lists_only_found_prices_cheapest_first():
    report = DealReport(
        "AirPods Pro 3",
        [
            Offer(
                "AirPods Pro 3", 169.0, "Staples", "https://s", "front-page deal", "2026-11-01", 215
            )
        ],
        checked=["Slickdeals"],
        failed=["store pages"],
    )
    context = deal_context(report, "Sunday")
    assert "never estimate" in context
    assert (
        "[1] $169.00 at Staples: AirPods Pro 3 [front-page deal, posted 2026-11-01, +215 votes]"
        in context
    )
    assert "unavailable: store pages" in context
    assert "No prices were found" in deal_context(DealReport("x", []), "Sunday")


# ---------------------------------------------------------------- watching prices


def offer(price, *, posted=None, store="Amazon"):
    return Offer("AirPods Pro 3", price, store, f"https://deal/{price}", "deal post", posted)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "vara.db")
    database.upgrade()
    return database


def test_a_target_price_raises_one_alert_per_lower_price(db):
    today = datetime.now(UTC).date().isoformat()
    with db.session() as s:
        watch = price_watch.add_watch(s, "AirPods Pro 3", 170.0)
        assert price_watch.record_check(s, watch, DealReport("q", [offer(179.0)])) is None
        first = price_watch.record_check(s, watch, DealReport("q", [offer(169.0, posted=today)]))
        assert (first.price, first.store) == (169.0, "Amazon")
        assert (
            price_watch.record_check(s, watch, DealReport("q", [offer(169.0)])) is None
        )  # same deal
        assert price_watch.record_check(s, watch, DealReport("q", [offer(159.0)])).price == 159.0
        assert watch.best_price == 159.0


def test_without_a_target_a_five_percent_drop_raises_an_alert(db):
    with db.session() as s:
        watch = price_watch.add_watch(s, "Dyson V15", None)
        assert (
            price_watch.record_check(s, watch, DealReport("q", [offer(600.0)])) is None
        )  # baseline
        assert (
            price_watch.record_check(s, watch, DealReport("q", [offer(590.0)])) is None
        )  # under 5%
        assert price_watch.record_check(s, watch, DealReport("q", [offer(560.0)])).price == 560.0


def test_old_deal_posts_do_not_raise_alerts(db):
    old = (datetime.now(UTC) - timedelta(days=20)).date().isoformat()
    with db.session() as s:
        watch = price_watch.add_watch(s, "AirPods Pro 3", 170.0)
        assert (
            price_watch.record_check(s, watch, DealReport("q", [offer(150.0, posted=old)])) is None
        )


def test_the_first_check_only_sets_the_baseline(db):
    with db.session() as s:
        watch = price_watch.add_watch(s, "AirPods Pro 3", 170.0)
        assert (
            price_watch.record_check(s, watch, DealReport("q", [offer(160.0)]), alert=False) is None
        )
        assert price_watch.unseen_alerts(s) == []
        # Already told at this price, so the next check at the same price stays quiet.
        assert price_watch.record_check(s, watch, DealReport("q", [offer(160.0)])) is None


def test_stopping_matches_loosely_and_adding_again_updates_the_target(db):
    with db.session() as s:
        price_watch.add_watch(s, "AirPods Pro 3", 170.0)
        again = price_watch.add_watch(s, "airpods pro 3", 160.0)
        assert again.target_price == 160.0 and len(price_watch.active_watches(s)) == 1
        assert [w.query for w in price_watch.stop_watches(s, "AirPods")] == ["AirPods Pro 3"]
        assert price_watch.active_watches(s) == []


# ---------------------------------------------------------------- chat and API


class FakeFinder:
    def __init__(self, offers=None):
        self.queries = []
        self.offers = (
            offers
            if offers is not None
            else [offer(179.0, store="Walmart"), offer(169.0, store="Staples")]
        )

    async def find(self, query):
        self.queries.append(query)
        return DealReport(query, sorted(self.offers, key=lambda o: o.price), checked=["Slickdeals"])


def deals_client(finder=None) -> tuple[TestClient, PlanningProvider, FakeFinder, FakeWeb]:
    provider, web, finder = PlanningProvider(), FakeWeb(), finder or FakeFinder()
    client = web_client(provider, web)
    client.app.state.deals = finder
    return client, provider, finder, web


def chat(client, text, **extra):
    return client.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": text}], **extra}
    ).json()


def test_a_deal_question_gives_the_model_the_prices_and_skips_web_search():
    client, provider, finder, web = deals_client()

    reply = chat(client, "find me the best deal on AirPods Pro 3")

    assert finder.queries == ["AirPods Pro 3"]
    assert web.queries == []  # no general search
    assert "[1] $169.00 at Staples" in provider.systems[-1]
    assert [o["store"] for o in reply["deals"]["offers"]] == ["Staples", "Walmart"]


def test_a_watch_request_starts_watching_and_says_so():
    client, provider, _finder, _ = deals_client()

    reply = chat(client, "tell me when the AirPods Pro 3 drop below $150")

    assert reply["deals"]["watch"]["target_price"] == 150.0
    assert reply["deals"]["watch"]["best_price"] == 169.0
    assert 'now watching "AirPods Pro 3"' in provider.systems[-1]
    watches = client.get("/v1/deals/watches").json()
    assert [w["query"] for w in watches] == ["AirPods Pro 3"]

    chat(client, "what prices are you watching?")
    assert (
        "AirPods Pro 3 (alert at or below $150.00; lowest seen $169.00 at Staples)"
        in provider.systems[-1]
    )

    chat(client, "stop watching the AirPods")
    assert client.get("/v1/deals/watches").json() == []


def test_waiting_alerts_are_told_in_the_next_reply_then_marked_seen():
    client, provider, _, _ = deals_client()
    db = client.app.state.db
    with db.session() as s:
        watch = price_watch.add_watch(s, "AirPods Pro 3", 170.0)
        price_watch.record_check(s, watch, DealReport("q", [offer(165.0, store="Target")]))
    assert len(client.get("/v1/deals/alerts?unseen=true").json()) == 1

    reply = chat(client, "write a haiku")

    assert "AirPods Pro 3: $165.00 at Target" in provider.systems[-1]
    assert reply["price_alerts"][0]["store"] == "Target"
    assert client.get("/v1/deals/alerts?unseen=true").json() == []


def test_the_watch_api_adds_lists_and_stops_watches():
    client, _, _, _ = deals_client()

    created = client.post("/v1/deals/watches", json={"query": "PS5 Pro", "target_price": 600})
    assert created.status_code == 201
    watch_id = created.json()["id"]
    assert client.get("/v1/deals").status_code == 422  # q is required
    assert client.delete(f"/v1/deals/watches/{watch_id}").status_code == 204
    assert client.delete("/v1/deals/watches/999").status_code == 404
    with client.app.state.db.session() as s:
        assert s.get(PriceWatch, watch_id).active is False


def test_price_checks_are_unavailable_when_web_search_is_off():
    from vara.main import create_app

    client = TestClient(create_app(Settings()))
    assert client.get("/v1/deals", params={"q": "PS5"}).status_code == 503
