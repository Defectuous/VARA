"""Decides when a chat request needs a web search, and what to search for.

1. A quick word check: most messages ("write me a poem", "what's 12 times 7") have no sign of needing
   current information and skip search with no added delay.
2. If the check fires, the model is asked, briefly, whether to search and with what query. An explicit
   "search for ..." / "look up ..." searches directly.
"""

import re
from collections.abc import AsyncIterator, Callable

from vara.providers.base import Completed, Message, TextDelta

_EXPLICIT = re.compile(
    r"^\s*(?:please\s+)?(?:search(?:\s+the\s+web)?\s+for|look\s+up|google|find\s+out)\s+(.+)",
    re.IGNORECASE,
)
_CUES = re.compile(
    r"\b(current(ly)?|latest|today|tonight|yesterday|this\s+(week|month|year)|right\s+now|now|news|"
    r"headlines?|recent(ly)?|who\s+is|who's|who\s+are|who\s+won|president|prime\s+minister|ceo|"
    r"governor|mayor|leader|price|prices|cost|stock|weather|forecast|score|scores|won|winner|"
    r"election|released?|release\s+date|update|search|look\s+up|20[2-3]\d|"
    # Markets and other figures that change daily.
    r"stocks?|shares?|markets?|s\s?&\s?p(\s?500)?|nasdaq|dow(\s+jones)?|index|indices|indexes|"
    r"ticker|quote|crypto|bitcoin|btc|ethereum|exchange\s+rates?|interest\s+rates?|inflation|"
    r"mortgage\s+rates?|earnings|bullish|bearish|"
    # Asking for current or public information directly.
    r"live|real[\s-]?time|public\s+data|online|internet|web|check)\b",
    re.IGNORECASE,
)
_NEWS = re.compile(r"\b(news|headlines?|happened|announced)\b", re.IGNORECASE)

PLANNER_PROMPT = (
    "You decide whether answering the user's message needs a web search. Your training data ends in "
    "2023 and today is {date}, so anything that may have changed since then (who holds an office, "
    "news, prices, scores, weather, releases, recent events) needs a search. Timeless facts, math, "
    "writing, and opinions do not. Earlier messages, if any, are there so you can understand a follow-up "
    '("and the NASDAQ?"); write a query that stands on its own.\n'
    "Reply with exactly one line:\n"
    "SEARCH: <a short web search query>\n"
    "or\n"
    "NO_SEARCH"
)


def might_need_search(text: str) -> bool:
    return bool(_EXPLICIT.match(text) or _CUES.search(text))


def explicit_query(text: str) -> str | None:
    match = _EXPLICIT.match(text)
    return match.group(1).strip(" ?.!") if match else None


def category_for(text: str) -> str:
    return "news" if _NEWS.search(text) else "general"


def parse_decision(reply: str) -> str | None:
    """The search query from the model's reply, or None for NO_SEARCH / anything unclear."""
    for line in reply.strip().splitlines():
        match = re.match(r"\s*SEARCH\s*:\s*(.+)", line, re.IGNORECASE)
        if match:
            query = match.group(1).strip().strip('"').strip()
            return query[:200] or None
        if "NO_SEARCH" in line.upper():
            return None
    return None


HISTORY_MESSAGES = 4  # earlier messages the planner sees, for follow-ups


async def plan_search(
    text: str,
    today: str,
    ask_model: Callable[[list[Message], str], AsyncIterator],
    history: list[Message] | None = None,
) -> str | None:
    """The query to search for, or None. `ask_model(messages, system)` streams a routed model reply.
    `history` is the conversation before `text`; a follow-up to a question that needed searching
    ("can you check the NASDAQ too?") is considered even if it has no cue words of its own."""
    if (query := explicit_query(text)) is not None:
        return query
    recent = (history or [])[-HISTORY_MESSAGES:]
    previous_question = next((m.content for m in reversed(recent) if m.role == "user"), "")
    if not (might_need_search(text) or might_need_search(previous_question)):
        return None
    transcript = "\n".join(f"{m.role}: {m.content[:500]}" for m in recent)
    asked = f"Earlier messages:\n{transcript}\n\nLatest message: {text}" if recent else text
    parts: list[str] = []
    async for routed in ask_model([Message("user", asked)], PLANNER_PROMPT.format(date=today)):
        event = getattr(routed, "event", routed)
        if isinstance(event, TextDelta):
            parts.append(event.text)
        elif isinstance(event, Completed):
            break
    return parse_decision("".join(parts))
