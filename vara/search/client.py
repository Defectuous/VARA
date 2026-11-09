"""Searching through SearXNG, and turning results into context the model can answer from."""

from dataclasses import asdict, dataclass, field

import httpx


class SearchFailed(RuntimeError):
    pass


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str
    engines: list[str]
    published: str | None = None


@dataclass
class WebSearch:
    query: str
    results: list[WebResult]
    answers: list[str] = field(default_factory=list)  # direct answers / infobox summaries
    unresponsive: list[str] = field(default_factory=list)  # engines that blocked or timed out

    def to_dict(self) -> dict:
        return asdict(self)


class SearxngClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=15.0)

    async def search(self, query: str, *, category: str = "general", limit: int = 6) -> WebSearch:
        try:
            response = await self._client.get(
                "/search", params={"q": query, "format": "json", "categories": category}
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchFailed(f"Web search is unavailable: {exc}") from exc

        results = []
        for item in data.get("results", []):
            if not item.get("url") or not item.get("title"):
                continue
            results.append(
                WebResult(
                    title=item["title"].strip(),
                    url=item["url"],
                    snippet=" ".join((item.get("content") or "").split())[:400],
                    engines=sorted(set(item.get("engines") or [item.get("engine", "")]) - {""}),
                    published=item.get("publishedDate"),
                )
            )
        # SearXNG already ranks results; among the top ones, prefer those several engines agree on.
        top = results[: limit * 2]
        top.sort(key=lambda r: -len(r.engines))
        answers = [
            str(a.get("answer", a)) if isinstance(a, dict) else str(a)
            for a in data.get("answers", [])
        ]
        for box in data.get("infoboxes", [])[:1]:
            if box.get("content"):
                answers.append(
                    f"{box.get('infobox', '')}: {' '.join(box['content'].split())[:400]}"
                )
        unresponsive = [str(e[0]) for e in data.get("unresponsive_engines", []) if e]
        return WebSearch(
            query=query, results=top[:limit], answers=answers, unresponsive=unresponsive
        )


def search_context(search: WebSearch, retrieved: str) -> str:
    """The results as a block for the system prompt, numbered so the answer can cite them."""
    intro = (
        f'Web search results for "{search.query}" (retrieved {retrieved}). Use them to answer when '
        "they are relevant; they are more current than your training. Mention the source briefly "
        '(e.g. "according to Wikipedia"). If the results disagree or don\'t answer the question, say so. '
        "Only state a number, price, score, or date if it appears in these results (or in market data "
        "given above); never estimate or fill one in. If the figure isn't there, say the results didn't "
        "include it."
    )
    lines = [intro]
    for answer in search.answers:
        lines.append(f"- Direct answer: {answer}")
    for i, r in enumerate(search.results, 1):
        agreed = f"found by {', '.join(r.engines)}" if r.engines else ""
        date = f", published {r.published[:10]}" if r.published else ""
        lines.append(f"[{i}] {r.title} ({r.url}; {agreed}{date})\n    {r.snippet}")
    return "\n".join(lines)
