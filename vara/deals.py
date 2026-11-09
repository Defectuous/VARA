"""Finding the best price for something to buy: deal posts and store pages, cheapest first.

Sources (all read-only; nothing is ever bought):
- Slickdeals' public search feed: community-posted deals with the store, the exact price, the date, and a
  vote score. Deals older than DEAL_MAX_AGE are ignored.
- Store and manufacturer product pages found by V.A.R.A.'s web search, read for the price they publish as
  structured data (schema.org Product/Offer). Only pages whose robots.txt allows automated readers are
  fetched, and nothing is done to get past a site that blocks them (Walmart, Target, and others do).
- Best Buy's official Products API, when a free key is stored (`python -m vara deals bestbuy-key`).

A price is only reported when it was read from one of these; the model is told never to fill one in.
Only the product name leaves this PC, as with web search.
"""

import asyncio
import html
import itertools
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib import robotparser
from urllib.parse import quote_plus, urlparse

import httpx

log = logging.getLogger("vara.deals")

DEAL_MAX_AGE = timedelta(days=45)
MAX_PAGES = 8
PAGE_BYTES = 2_000_000
USER_AGENT = "Mozilla/5.0 (compatible; VARA personal assistant)"
# Front-page deals are vetted by Slickdeals' editors; the full search adds community posts.
SLICKDEALS_FRONTPAGE = "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1&q={q}"
SLICKDEALS_ALL = "https://slickdeals.net/newsearch.php?searcharea=deals&searchin=first&rss=1&q={q}"
PAGE_TIMEOUT = 6.0  # seconds for one store page, robots.txt included
BESTBUY_API = "https://api.bestbuy.com/v1/products({terms})"

# Search results from these are about a product, not selling it.
NOT_STORES = {
    "wikipedia.org",
    "reddit.com",
    "youtube.com",
    "rtings.com",
    "tomshardware.com",
    "pcmag.com",
    "theverge.com",
    "cnet.com",
    "techradar.com",
    "camelcamelcamel.com",
    "pcpartpicker.com",
    "support.logi.com",
    "quora.com",
    "facebook.com",
    "x.com",
    "twitter.com",
    "slickdeals.net",
}
# Stores whose terms of use forbid automated reading even where robots.txt doesn't. Their deals still
# arrive through Slickdeals posts.
TERMS_FORBID = {"amazon.com", "walmart.com", "target.com"}


# ---------------------------------------------------------------------------------------------
# Understanding the request


@dataclass
class DealRequest:
    kind: str  # "find" | "watch" | "list_watches" | "stop_watch"
    query: str = ""
    target: float | None = None


_FILLER = re.compile(
    r"^(?:(?:a|an|the|some|me|new)\s+)+|\s+(?:please|for\s+me|right\s+now|today|online|"
    r"in\s+the\s+us|this\s+week)$",
    re.IGNORECASE,
)
_MONEY = r"\$?\s?(\d[\d,]*(?:\.\d{1,2})?)"
_WATCH_PATTERNS = [
    # "tell me when the MX Master 3S drops below $70"
    re.compile(
        r"\b(?:tell|let|alert|notify|ping)\s+me\s+(?:know\s+)?(?:when|if)\s+(?:the\s+price\s+(?:of|on|for)\s+)?"
        r"(?P<q>.+?)\s+(?:drops?|goes|falls|is|gets|comes)\s+(?:down\s+)?(?:below|under|to|less\s+than)\s+"
        + _MONEY,
        re.IGNORECASE,
    ),
    # "watch the price of X (and tell me when it drops) (below $N)"
    re.compile(
        r"\b(?:watch|track|monitor|keep\s+an\s+eye\s+on)\s+(?:the\s+)?(?:price|prices|deals?)\s+(?:of|on|for)\s+"
        r"(?P<q>.+?)(?:\s+(?:and|then)\s+(?:tell|let|alert|notify).*?)?"
        r"(?:\s+(?:below|under|drops?\s+(?:below|under|to)|for\s+less\s+than)\s+"
        + _MONEY
        + r")?\s*[.?!]*$",
        re.IGNORECASE,
    ),
    # "watch X for a price drop", "track X price"
    re.compile(
        r"\b(?:watch|track|monitor)\s+(?P<q>.+?)\s+(?:for\s+(?:a\s+)?(?:price\s+drop|deals?|sales?)|price|prices)"
        r"(?:\s+(?:below|under)\s+" + _MONEY + r")?\s*[.?!]*$",
        re.IGNORECASE,
    ),
]
_FIND_PATTERNS = [
    re.compile(
        r"\b(?:find|get|look\s+for|search\s+for|show|check)\s+(?:me\s+)?(?:the\s+|a\s+|any\s+)?"
        r"(?:best|cheapest|lowest|good|great|current)?\s*(?:deals?|prices?|offers?|sales?)\s+(?:on|for)\s+(?P<q>.+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:best|cheapest|lowest)\s+(?:deals?|prices?|offers?)\s+(?:on|for)\s+(?P<q>.+)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:any|good)\s+deals?\s+(?:on|for)\s+(?P<q>.+)", re.IGNORECASE),
    re.compile(
        r"\bwhere\s+(?:can|should|do)\s+i\s+(?:buy|get|find)\s+(?P<q>.+?)(?:\s+(?:cheap(?:est)?|for\s+less))?\s*[.?!]*$",
        re.IGNORECASE,
    ),
    re.compile(r"\bcheapest\s+(?:place\s+to\s+(?:buy|get)\s+)?(?P<q>.+)", re.IGNORECASE),
    re.compile(r"\bis\s+(?P<q>.+?)\s+on\s+sale\b", re.IGNORECASE),
]
_LIST_WATCHES = re.compile(
    r"\b(?:what|which)\s+(?:prices?|deals?|things|items|products)\s+(?:are\s+you|do\s+you|you're)\s+"
    r"(?:watching|tracking|monitoring)|\b(?:my|list)\s+(?:price\s+)?watch(?:es|list)\b",
    re.IGNORECASE,
)
_STOP_WATCH = re.compile(
    r"\b(?:stop|quit|cancel)\s+(?:watching|tracking|monitoring)\s+(?:the\s+price\s+(?:of|on|for)\s+)?(?P<q>.+)",
    re.IGNORECASE,
)


def _clean(query: str) -> str:
    query = query.strip(" ?.!,;:\"'")
    for _ in range(3):
        query = _FILLER.sub("", query).strip(" ?.!,;:\"'")
    return query[:120]


def _money(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def parse_request(text: str) -> DealRequest | None:
    """What the message asks for, or None if it isn't about buying something."""
    if _LIST_WATCHES.search(text):
        return DealRequest("list_watches")
    if match := _STOP_WATCH.search(text):
        return DealRequest("stop_watch", _clean(match.group("q")))
    for pattern in _WATCH_PATTERNS:
        if (match := pattern.search(text)) and (query := _clean(match.group("q"))):
            target = _money(next((g for g in match.groups()[1:] if g), None))
            return DealRequest("watch", query, target)
    for pattern in _FIND_PATTERNS:
        if (match := pattern.search(text)) and (query := _clean(match.group("q"))):
            return DealRequest("find", query)
    return None


# ---------------------------------------------------------------------------------------------
# Matching offers to what was asked for


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower())


def relevant(query: str, title: str) -> bool:
    """Whether an offer's title is the thing asked for. Model numbers and sizes ("990", "2TB", "3S") must
    all appear exactly; most of the other words must too. So "990 Pro 2TB" doesn't match a 9100 Pro."""
    wanted = [t for t in _tokens(query) if t not in {"the", "a", "an", "and", "for", "with", "new"}]
    have = set(_tokens(title))
    # "2 tb" / "2tb", "27 inch" / "27in" / "27\"": compare joined forms too.
    joined = set(
        re.findall(
            r"[a-z0-9]+", re.sub(r"(\d)\s+(tb|gb|in|inch|hz|w|mm)\b", r"\1\2", title.lower())
        )
    )
    have |= joined
    codes = [t for t in wanted if any(c.isdigit() for c in t)]
    words = [t for t in wanted if t not in codes and len(t) > 2]
    if any(c not in have for c in codes):
        return False
    # A short bare number belongs to the word before it: "Pro 2" must appear as "pro 2" (not "Pro 3 ...
    # 2-Yr AppleCare"), "iPhone 16" as "iphone 16".
    title_tokens = _tokens(title)
    pairs = set(itertools.pairwise(title_tokens))
    for before, number in itertools.pairwise(wanted):
        bare = number.isdigit() and len(number) <= 2 and not before.isdigit()
        if bare and (before, number) not in pairs and f"{before}{number}" not in have:
            return False
    if not words:
        return bool(codes)
    return sum(w in have or w.rstrip("s") in have for w in words) >= max(
        1, round(len(words) * 0.67)
    )


# ---------------------------------------------------------------------------------------------
# Offers and sources


@dataclass
class Offer:
    title: str
    price: float
    store: str
    url: str
    source: str  # "deal post" | "store page" | "Best Buy API"
    posted: str | None = None  # ISO date of a deal post
    votes: int | None = None  # Slickdeals thumb score
    regular_price: float | None = None
    in_stock: bool | None = None

    def line(self, i: int) -> str:
        bits = [f"[{i}] ${self.price:,.2f} at {self.store}: {self.title}"]
        if self.regular_price and self.regular_price > self.price:
            bits.append(f"(regularly ${self.regular_price:,.2f})")
        if self.in_stock is False:
            bits.append("(out of stock)")
        detail = [self.source]
        if self.posted:
            detail.append(f"posted {self.posted[:10]}")
        if self.votes is not None:
            detail.append(f"{self.votes:+d} votes")
        bits.append(f"[{', '.join(detail)}] {self.url}")
        return " ".join(bits)


@dataclass
class DealReport:
    query: str
    offers: list[Offer]
    checked: list[str] = field(default_factory=list)  # sources that answered
    failed: list[str] = field(default_factory=list)  # sources that didn't

    @property
    def best(self) -> Offer | None:
        in_stock = [o for o in self.offers if o.in_stock is not False]
        return min(in_stock or self.offers, key=lambda o: o.price, default=None)

    def to_dict(self) -> dict:
        return asdict(self)


NOT_NEW = re.compile(
    r"\b(refurb(?:ished)?|renewed|open[\s-]box|used|pre[\s-]?owned|certified\s+pre|recertified|"
    r"seller\s+refurb)\b",
    re.IGNORECASE,
)


def _store_of(description: str, title: str, link: str) -> str:
    """Where the deal is: "Amazon has ..." in the description, "... $319.99 at Dyson" in the title, or a
    post address ending in "-at-best-buy"."""
    if m := re.match(
        r"\s*([A-Z0-9][\w&.'\- ]{1,40}?)\s+(?:has|is\s+offering|offers)\b", description
    ):
        return m.group(1)
    # "Costco Members: ...", "Select Staples Stores: ...", "Prime Members: ..."
    if m := re.match(
        r"\s*(?:Select\s+|In[\s-]Store\s+)?"
        r"([A-Z][\w&.'\-]+(?:\s+(?!(?:Stores?|Members?|Customers?)\b)[A-Z][\w&.'\-]+)?)"
        r"(?:\s+(?:Stores?|Members?|Customers?))?\s*:",
        title,
    ):
        name = m.group(1)
        return "Amazon" if name in ("Prime", "Amazon Prime") else name
    if m := re.search(r"\bat\s+([A-Z0-9][\w&.'\-]*(?:\s+[A-Z][\w&.'\-]*){0,2})", title):
        return m.group(1)
    if m := re.search(r"-at-([a-z0-9-]+?)/?$", link):
        return m.group(1).replace("-", " ").title()
    return "see post"


def parse_slickdeals(
    feed: str, query: str, now: datetime | None = None, *, vetted: bool = False
) -> list[Offer]:
    now = now or datetime.now(UTC)
    root = ET.fromstring(feed)
    offers = []
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        description = html.unescape(item.findtext("description") or "")
        link = (item.findtext("link") or "").split("?")[0]
        try:
            posted = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if now - posted > DEAL_MAX_AGE or not relevant(query, title):
            continue
        if "?" in title:  # a question in the forum ("Would you buy ...?"), not a deal
            continue
        if NOT_NEW.search(title):  # new items only
            continue
        content = "".join(item.itertext())
        votes = re.search(r"Thumb Score:\s*([+-]?\d+)", content)
        if votes and int(votes.group(1)) < 0:  # the community voted it down (wrong, expired, scam)
            continue
        # The title's price is the deal ("$190"); the description has it exactly ("$189.99") among others
        # such as the regular price, so take the description's price closest to the title's.
        titled = _money(m.group(1)) if (m := re.search(r"\$([\d,]+(?:\.\d{2})?)", title)) else None
        described = [_money(p) for p in re.findall(r"\$([\d,]+(?:\.\d{2})?)", description)]
        described = [p for p in described if p]
        if titled is not None:
            close = [p for p in described if abs(p - titled) <= max(1.0, titled * 0.01)]
            price = close[0] if close else titled
        else:
            price = described[0] if described else None
        if price is None:
            continue
        store = _store_of(description, title, link)
        offers.append(
            Offer(
                title=title,
                price=price,
                store=store,
                url=link,
                source="front-page deal" if vetted else "community deal post",
                posted=posted.astimezone(UTC).date().isoformat(),
                votes=int(votes.group(1)) if votes else None,
            )
        )
    return offers


_LD_JSON = re.compile(
    r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE
)


def _walk(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _walk(value)


def _is(node: dict, kind: str) -> bool:
    types = node.get("@type")
    return kind in (types if isinstance(types, list) else [types])


def parse_product_page(page: str, url: str, query: str) -> Offer | None:
    """The price a page publishes as schema.org Product data, if it's the product asked for."""
    for block in _LD_JSON.findall(page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for node in _walk(data):
            if not _is(node, "Product"):
                continue
            name = html.unescape(str(node.get("name") or ""))
            if not relevant(query, name) or NOT_NEW.search(name):
                continue
            offers = node.get("offers") or []
            for offer in _walk(offers):
                price = _money(str(offer.get("price") or offer.get("lowPrice") or ""))
                currency = offer.get("priceCurrency") or "USD"
                if price is None or price <= 0 or currency != "USD":
                    continue
                availability = str(offer.get("availability") or "")
                host = urlparse(url).netloc.removeprefix("www.")
                return Offer(
                    title=name,
                    price=price,
                    store=host,
                    url=url,
                    source="store page",
                    in_stock=None if not availability else "InStock" in availability,
                )
    return None


class RobotsCache:
    """robots.txt per site, fetched once per run. A site whose rules can't be read is skipped."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._rules: dict[str, robotparser.RobotFileParser | None] = {}

    async def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        site = f"{parts.scheme}://{parts.netloc}"
        if site not in self._rules:
            try:
                response = await self._client.get(f"{site}/robots.txt", timeout=5)
                rules = robotparser.RobotFileParser()
                if response.status_code >= 400:
                    rules.parse([])  # no robots.txt: allowed
                else:
                    rules.parse(response.text.splitlines())
                self._rules[site] = rules
            except httpx.HTTPError:
                self._rules[site] = None
        rules = self._rules[site]
        return rules is not None and rules.can_fetch("VARA", url)


class DealFinder:
    def __init__(
        self, web, client: httpx.AsyncClient | None = None, bestbuy_key: str | None = None
    ):
        """`web` is V.A.R.A.'s SearXNG client (or anything with the same `search`)."""
        self.web = web
        self.bestbuy_key = bestbuy_key
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(8, connect=5),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )

    async def slickdeals(self, query: str) -> list[Offer]:
        async def feed(url: str, vetted: bool) -> list[Offer]:
            response = await self._client.get(url.format(q=quote_plus(query)))
            response.raise_for_status()
            return parse_slickdeals(response.text, query, vetted=vetted)

        front, everything = await asyncio.gather(
            feed(SLICKDEALS_FRONTPAGE, True), feed(SLICKDEALS_ALL, False)
        )
        vetted_links = {o.url for o in front}
        return front + [o for o in everything if o.url not in vetted_links]

    async def store_pages(self, query: str) -> list[Offer]:
        found = await asyncio.gather(
            self.web.search(f"{query} price", limit=10),
            self.web.search(f"buy {query}", limit=10),
            return_exceptions=True,
        )
        urls: list[str] = []
        for search in found:
            if isinstance(search, BaseException):
                continue
            for result in search.results:
                host = urlparse(result.url).netloc.removeprefix("www.")
                if any(host == s or host.endswith("." + s) for s in NOT_STORES | TERMS_FORBID):
                    continue
                if result.url not in urls:
                    urls.append(result.url)
        robots = RobotsCache(self._client)

        async def read(url: str) -> Offer | None:
            try:
                return await asyncio.wait_for(read_page(url), PAGE_TIMEOUT)
            except TimeoutError:
                return None

        async def read_page(url: str) -> Offer | None:
            if not await robots.allowed(url):
                return None
            try:
                async with self._client.stream("GET", url) as response:
                    if response.status_code != 200 or "html" not in response.headers.get(
                        "content-type", ""
                    ):
                        return None
                    body = b""
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > PAGE_BYTES:
                            break
                return parse_product_page(body.decode("utf-8", "replace"), url, query)
            except httpx.HTTPError:
                return None

        pages = await asyncio.gather(*(read(u) for u in urls[:MAX_PAGES]))
        return [o for o in pages if o is not None]

    async def bestbuy(self, query: str) -> list[Offer]:
        terms = "&".join(f"search={quote_plus(t)}" for t in _tokens(query))
        response = await self._client.get(
            BESTBUY_API.format(terms=terms),
            params={
                "apiKey": self.bestbuy_key,
                "format": "json",
                "pageSize": 10,
                "show": "name,salePrice,regularPrice,onlineAvailability,url",
            },
        )
        response.raise_for_status()
        offers = []
        for product in response.json().get("products", []):
            if not relevant(query, product.get("name", "")):
                continue
            offers.append(
                Offer(
                    title=product["name"],
                    price=float(product["salePrice"]),
                    store="Best Buy",
                    url=product.get("url") or "https://www.bestbuy.com",
                    source="Best Buy API",
                    regular_price=_money(str(product.get("regularPrice") or "")),
                    in_stock=product.get("onlineAvailability"),
                )
            )
        return offers

    async def find(self, query: str) -> DealReport:
        sources = {"Slickdeals": self.slickdeals(query), "store pages": self.store_pages(query)}
        if self.bestbuy_key:
            sources["Best Buy"] = self.bestbuy(query)
        results = await asyncio.gather(*sources.values(), return_exceptions=True)
        report = DealReport(query=query, offers=[])
        for name, result in zip(sources, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("Deal source %s failed: %s", name, result)
                report.failed.append(name)
            else:
                report.checked.append(name)
                report.offers.extend(result)
        # One line per store and price; cheapest first.
        unique: dict[tuple[str, float], Offer] = {}
        for offer in report.offers:
            unique.setdefault((offer.store.lower(), offer.price), offer)
        report.offers = sorted(unique.values(), key=lambda o: o.price)[:12]
        log.info("Deals for %r: %d offers from %s", query, len(report.offers), report.checked)
        return report


def deal_context(report: DealReport, retrieved: str) -> str:
    lines = [
        f'Price check for "{report.query}" (retrieved {retrieved}; sources checked: '
        f"{', '.join(report.checked) or 'none'}"
        + (f"; unavailable: {', '.join(report.failed)}" if report.failed else "")
        + "). These are the only prices you know: quote them exactly with the store, never estimate or "
        "add others, and lead with the cheapest that is in stock. Say a deal post's date (deals can "
        "expire), and point out if an item differs from what was asked (size, model, refurbished, "
        "bundle). V.A.R.A. never buys anything."
    ]
    if not report.offers:
        lines.append(
            "No prices were found. Say so, and suggest a more specific product name or model number."
        )
    lines += [o.line(i) for i, o in enumerate(report.offers, 1)]
    return "\n".join(lines)
