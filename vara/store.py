import re
from collections.abc import Sequence

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from vara.db.models import Conversation, Memory, MemoryStatus, RetentionRule, StoredMessage
from vara.providers.base import Message

_WORD = re.compile(r"\w+", re.UNICODE)
_STOPWORDS = frozenset(
    "the and for are but not you your yours with this that what when where which who why how "  # noqa: SIM905
    "can could would should will was were has have had does did from into about there their "
    "them then than they its it's i'm me my mine our out all any some just also very".split()
)


def keywords(text_: str) -> list[str]:
    """Distinct meaningful lowercase words, in order of appearance."""
    words = _WORD.findall(text_.lower())
    return list(dict.fromkeys(w for w in words if len(w) > 2 and w not in _STOPWORDS))


def fts_query(text_: str) -> str | None:
    """Turn free text into a safe FTS5 query: distinct meaningful words, ORed and quoted."""
    terms = keywords(text_)
    if not terms:
        return None
    return " OR ".join(f'"{term}"' for term in terms)


def title_from(text_: str) -> str:
    first_line = text_.strip().splitlines()[0] if text_.strip() else "New conversation"
    return first_line[:60] + ("…" if len(first_line) > 60 else "")


def history(conversation: Conversation) -> list[Message]:
    return [Message(m.role, m.content) for m in conversation.messages]  # type: ignore[arg-type]


def save_exchange(
    session: Session,
    conversation: Conversation,
    new_messages: Sequence[Message],
    reply: str,
    summary: dict,
) -> StoredMessage:
    for message in new_messages:
        conversation.messages.append(StoredMessage(role=message.role, content=message.content))
    stored = StoredMessage(
        role="assistant",
        content=reply,
        provider=summary["provider"],
        model=summary["model"],
        input_tokens=summary["usage"]["input_tokens"],
        output_tokens=summary["usage"]["output_tokens"],
        cost_usd=summary["cost_usd"],
    )
    conversation.messages.append(stored)
    session.add(conversation)
    session.flush()
    return stored


def retrieve_memories(session: Session, query: str, limit: int = 5) -> list[Memory]:
    """Confirmed memories most relevant to `query`, best match first."""
    match = fts_query(query)
    if match is None:
        return []
    rows = session.execute(
        text(
            "SELECT memories.id FROM memories_fts JOIN memories ON memories.id = memories_fts.rowid "
            "WHERE memories_fts MATCH :match AND memories.status = :status "
            "ORDER BY bm25(memories_fts) LIMIT :limit"
        ),
        {"match": match, "status": MemoryStatus.CONFIRMED.value, "limit": limit},
    ).scalars()
    ids = list(rows)
    found = {m.id: m for m in session.scalars(select(Memory).where(Memory.id.in_(ids)))}
    return [found[i] for i in ids]


def memory_prompt(system: str | None, memories: Sequence[Memory]) -> str | None:
    if not memories:
        return system
    facts = "\n".join(f"- {m.content}" for m in memories)
    block = f"Facts the user has asked you to remember (use them only when relevant):\n{facts}"
    return f"{system}\n\n{block}" if system else block


def search(session: Session, query: str, limit: int = 20) -> dict:
    match = fts_query(query)
    if match is None:
        return {"messages": [], "memories": []}
    messages = session.execute(
        text(
            "SELECT m.id, m.conversation_id, m.role, m.created_at, "
            "snippet(messages_fts, 0, '[', ']', '…', 12) AS snippet "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "WHERE messages_fts MATCH :match ORDER BY bm25(messages_fts) LIMIT :limit"
        ),
        {"match": match, "limit": limit},
    ).mappings()
    memories = session.execute(
        text(
            "SELECT m.id, m.content, m.status FROM memories_fts "
            "JOIN memories m ON m.id = memories_fts.rowid "
            "WHERE memories_fts MATCH :match ORDER BY bm25(memories_fts) LIMIT :limit"
        ),
        {"match": match, "limit": limit},
    ).mappings()
    return {
        "messages": [{**row, "created_at": str(row["created_at"])} for row in messages],
        "memories": [dict(row) for row in memories],
    }


def usage_summary(session: Session) -> list[dict]:
    rows = session.execute(
        select(
            StoredMessage.provider,
            StoredMessage.model,
            func.count(),
            func.coalesce(func.sum(StoredMessage.input_tokens), 0),
            func.coalesce(func.sum(StoredMessage.output_tokens), 0),
            func.coalesce(func.sum(StoredMessage.cost_usd), 0.0),
            func.count() - func.count(StoredMessage.cost_usd),
        )
        .where(StoredMessage.role == "assistant")
        .group_by(StoredMessage.provider, StoredMessage.model)
        .order_by(StoredMessage.provider, StoredMessage.model)
    )
    keys = ("provider", "model", "requests", "input_tokens", "output_tokens", "cost_usd")
    return [{**dict(zip(keys, row[:6], strict=True)), "unpriced_requests": row[6]} for row in rows]


def message_dict(m: StoredMessage) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "created_at": m.created_at.isoformat(),
        "provider": m.provider,
        "model": m.model,
        "input_tokens": m.input_tokens,
        "output_tokens": m.output_tokens,
        "cost_usd": m.cost_usd,
    }


def memory_dict(m: Memory) -> dict:
    return {
        "id": m.id,
        "content": m.content,
        "category": m.category,
        "status": m.status,
        "pinned": m.pinned,
        "source": m.source,
        "source_message_id": m.source_message_id,
        "created_at": m.created_at.isoformat(),
        "updated_at": m.updated_at.isoformat(),
    }


def conversation_dict(c: Conversation, *, with_messages: bool = False) -> dict:
    data = {
        "id": c.id,
        "title": c.title,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }
    if with_messages:
        data["messages"] = [message_dict(m) for m in c.messages]
    return data


def export_all(session: Session) -> dict:
    conversations = session.scalars(select(Conversation).order_by(Conversation.id))
    memories = session.scalars(select(Memory).order_by(Memory.id))
    return {
        "format": "vara-export",
        "version": 1,
        "conversations": [conversation_dict(c, with_messages=True) for c in conversations],
        "memories": [memory_dict(m) for m in memories],
        "retention_rules": {
            r.key: r.keep_days
            for r in session.scalars(select(RetentionRule).order_by(RetentionRule.key))
        },
    }
