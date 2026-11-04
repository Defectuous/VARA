from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # stored as naive UTC


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    messages: Mapped[list["StoredMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="StoredMessage.id"
    )


class StoredMessage(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Set on assistant messages: who answered and what it cost.
    provider: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd: Mapped[float | None]

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class MemoryStatus(StrEnum):
    CONFIRMED = "confirmed"  # approved by the user; eligible for retrieval
    PROPOSED = "proposed"  # suggested (for example by a model); never used until approved


class MemorySource(StrEnum):
    USER = "user"
    CONVERSATION = "conversation"


class MemoryCategory(StrEnum):
    """The kind of information a memory holds; retention rules are set per category."""

    IDENTITY = "identity"
    PREFERENCE = "preference"
    RELATIONSHIP = "relationship"
    LOCATION = "location"
    WORK = "work"
    HEALTH = "health"
    FINANCE = "finance"
    COMMITMENT = "commitment"
    OTHER = "other"


class Memory(Base):
    """An explicit fact about the user, kept separate from conversation history."""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[MemoryCategory] = mapped_column(String(30), default=MemoryCategory.OTHER)
    pinned: Mapped[bool] = mapped_column(default=False)  # exempt from every retention rule
    status: Mapped[MemoryStatus] = mapped_column(String(20), default=MemoryStatus.CONFIRMED)
    source: Mapped[MemorySource] = mapped_column(String(20), default=MemorySource.USER)
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class RetentionRule(Base):
    """How long to keep one kind of data. Keys: a MemoryCategory, "conversations", or
    "proposed" (unapproved suggestions). keep_days None means keep until deleted."""

    __tablename__ = "retention_rules"

    key: Mapped[str] = mapped_column(String(30), primary_key=True)
    keep_days: Mapped[int | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AccessKind(StrEnum):
    FOLDER = "folder"  # V.A.R.A. may read files in this folder and below
    EXCLUDE = "exclude"  # never read this folder, even inside an allowed one
    GOOGLE_DOCS = "google_docs"  # consent to read Google Docs once the account is connected


class AccessGrant(Base):
    """A permission the user gave (or a carve-out they made). Nothing is read without one."""

    __tablename__ = "access_grants"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[AccessKind] = mapped_column(String(20))
    path: Mapped[str | None] = mapped_column(Text)  # resolved absolute path; None for Google
    granted_at: Mapped[datetime] = mapped_column(default=utcnow)


class SetupStep(Base):
    """First-run steps the user has completed, such as choosing what V.A.R.A. may access."""

    __tablename__ = "setup_steps"

    key: Mapped[str] = mapped_column(String(30), primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(default=utcnow)
