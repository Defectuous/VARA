import json
import re
from dataclasses import dataclass

from vara.db.models import MemoryCategory, StoredMessage
from vara.logging import redact
from vara.providers.base import Message, TextDelta
from vara.store import keywords

EXTRACTION_PROMPT = f"""You find durable facts about the user in their own messages, for a personal \
assistant's long-term memory.

Return only JSON, with no other text:
{{"facts": [{{"content": "...", "category": "...", "message": 1}}]}}

- content: one short note about the user, written in the third person without "I" or "the user" \
(for example, starting with "Is", "Has", "Prefers", or "Promised"), keeping the specific details \
they gave (who, what, where, when). Use only details from the messages.
- category: one of {", ".join(c.value for c in MemoryCategory)}. Use commitment for plans, \
deadlines, and promises.
- message: the number of the message the fact came from.

Only include facts the user stated about themselves or their life, that would still be useful in a \
later conversation. Skip questions, one-off requests, and guesses. Never include passwords, API \
keys, account or card numbers, or other secrets. If there are no such facts, return {{"facts": []}}."""

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class Suggestion:
    content: str
    category: MemoryCategory
    source_message_id: int | None


def extraction_request(user_messages: list[StoredMessage]) -> list[Message]:
    numbered = "\n\n".join(f"[{i}] {m.content}" for i, m in enumerate(user_messages, start=1))
    return [Message("user", f"The user's messages:\n\n{numbered}")]


async def collect_text(events) -> tuple[str, str, str]:
    """Drain a routed stream into (text, provider, model)."""
    parts, provider, model = [], "", ""
    async for routed in events:
        provider = routed.provider
        if isinstance(routed.event, TextDelta):
            parts.append(routed.event.text)
        else:
            model = routed.event.model
    return "".join(parts), provider, model


def parse_suggestions(raw: str, user_messages: list[StoredMessage]) -> tuple[list[Suggestion], int]:
    """Validated suggestions from the model's reply, and how many items were rejected."""
    match = _JSON_OBJECT.search(raw)
    try:
        items = json.loads(match.group(0))["facts"] if match else []
    except (json.JSONDecodeError, KeyError, TypeError):
        items = []
    if not isinstance(items, list):
        items = []

    suggestions: list[Suggestion] = []
    rejected = 0
    for item in items:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, str) or not 3 <= len(content.strip()) <= 300:
            rejected += 1
            continue
        content = content.strip()
        if redact(content) != content:  # looks like a credential; never store it
            rejected += 1
            continue
        try:
            category = MemoryCategory(str(item.get("category", "")).lower())
        except ValueError:
            category = MemoryCategory.OTHER
        index = item.get("message")
        cited = (
            user_messages[index - 1]
            if isinstance(index, int) and 1 <= index <= len(user_messages)
            else None
        )
        # Grounding check: a fact must share a meaningful word with what the user wrote,
        # which catches facts the model invented or copied from its instructions.
        evidence = cited.content if cited else " ".join(m.content for m in user_messages)
        if not set(keywords(content)) & set(keywords(evidence)):
            rejected += 1
            continue
        suggestions.append(Suggestion(content, category, cited.id if cited else None))
    return suggestions, rejected
