from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vara.db.models import (
    Conversation,
    Memory,
    MemoryCategory,
    MemoryStatus,
    RetentionRule,
    utcnow,
)

CONVERSATIONS = "conversations"
PROPOSED = "proposed"

DESCRIPTIONS = {
    CONVERSATIONS: "Conversation history, counted from the last message",
    PROPOSED: "Suggested memories you have not approved, counted from when they were suggested",
    MemoryCategory.IDENTITY: "Name, age, languages, and other facts about who you are",
    MemoryCategory.PREFERENCE: "Likes, dislikes, and how you want things done",
    MemoryCategory.RELATIONSHIP: "Family, friends, colleagues, and pets",
    MemoryCategory.LOCATION: "Where you live, work, or travel",
    MemoryCategory.WORK: "Job, projects, skills, and tools",
    MemoryCategory.HEALTH: "Health conditions, allergies, medication, fitness",
    MemoryCategory.FINANCE: "Income, budgets, bills, and accounts",
    MemoryCategory.COMMITMENT: "Plans, deadlines, and promises; kept until you delete them",
    MemoryCategory.OTHER: "Anything that fits no other category",
}

# Categories that deserve extra care; flagged in the overview so their rule is a conscious choice.
SENSITIVE = frozenset({MemoryCategory.HEALTH, MemoryCategory.FINANCE})


def rules(session: Session) -> dict[str, int | None]:
    return {r.key: r.keep_days for r in session.scalars(select(RetentionRule))}


def set_rule(session: Session, key: str, keep_days: int | None) -> None:
    rule = session.get(RetentionRule, key)
    if rule is None:
        raise KeyError(key)
    rule.keep_days = keep_days


def _cutoff(now: datetime, days: int | None) -> datetime | None:
    return None if days is None else now - timedelta(days=days)


def expired(session: Session, now: datetime | None = None) -> dict[str, list]:
    """Conversations and unpinned memories older than their rule allows."""
    now = now or utcnow()
    current = rules(session)
    conversations: list[Conversation] = []
    if (cutoff := _cutoff(now, current.get(CONVERSATIONS))) is not None:
        conversations = list(
            session.scalars(select(Conversation).where(Conversation.updated_at < cutoff))
        )

    unpinned = select(Memory).where(Memory.pinned.is_(False))
    memories: list[Memory] = []
    if (cutoff := _cutoff(now, current.get(PROPOSED))) is not None:
        memories += session.scalars(
            unpinned.where(Memory.status == MemoryStatus.PROPOSED, Memory.created_at < cutoff)
        )
    for category in MemoryCategory:
        if (cutoff := _cutoff(now, current.get(category))) is not None:
            memories += session.scalars(
                unpinned.where(
                    Memory.status == MemoryStatus.CONFIRMED,
                    Memory.category == category,
                    Memory.updated_at < cutoff,
                )
            )
    return {"conversations": conversations, "memories": memories}


def apply(session: Session, now: datetime | None = None) -> dict[str, int]:
    found = expired(session, now)
    for item in found["conversations"] + found["memories"]:
        session.delete(item)
    session.flush()
    return {"conversations": len(found["conversations"]), "memories": len(found["memories"])}


def overview(session: Session) -> list[dict]:
    """The retention list: one rule per kind of data, the categories you have most of first."""
    current = rules(session)
    counts = Counter(
        {
            (category, status): n
            for category, status, n in session.execute(
                select(Memory.category, Memory.status, func.count()).group_by(
                    Memory.category, Memory.status
                )
            )
        }
    )
    pinned = Counter(
        dict(
            session.execute(
                select(Memory.category, func.count())
                .where(Memory.pinned.is_(True))
                .group_by(Memory.category)
            ).all()
        )
    )
    found = expired(session)
    due = Counter(m.category for m in found["memories"] if m.status == MemoryStatus.CONFIRMED)

    categories = [
        {
            "key": category.value,
            "kind": "memory_category",
            "description": DESCRIPTIONS[category],
            "keep_days": current.get(category),
            "sensitive": category in SENSITIVE,
            "confirmed": counts[(category, MemoryStatus.CONFIRMED)],
            "proposed": counts[(category, MemoryStatus.PROPOSED)],
            "pinned": pinned[category],
            "due_for_deletion": due[category],
        }
        for category in MemoryCategory
    ]
    categories.sort(key=lambda row: (-(row["confirmed"] + row["proposed"]), row["key"]))

    return [
        {
            "key": CONVERSATIONS,
            "kind": "conversations",
            "description": DESCRIPTIONS[CONVERSATIONS],
            "keep_days": current.get(CONVERSATIONS),
            "count": session.scalar(select(func.count()).select_from(Conversation)),
            "due_for_deletion": len(found["conversations"]),
        },
        {
            "key": PROPOSED,
            "kind": "unapproved_suggestions",
            "description": DESCRIPTIONS[PROPOSED],
            "keep_days": current.get(PROPOSED),
            "count": sum(n for (_, status), n in counts.items() if status == MemoryStatus.PROPOSED),
            "due_for_deletion": sum(
                1 for m in found["memories"] if m.status == MemoryStatus.PROPOSED
            ),
        },
        *categories,
    ]
