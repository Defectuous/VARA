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
    # Word and PDF files made from this message: a JSON list of names in data/documents.
    documents: Mapped[str | None] = mapped_column(Text)

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


class PriceWatch(Base):
    """Something to re-price on a schedule (vara/price_watch.py). With a target price, an alert is raised
    when an offer is at or below it; without one, when the price drops at least 5% below the lowest seen."""

    __tablename__ = "price_watches"

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(String(120))
    target_price: Mapped[float | None]
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_checked_at: Mapped[datetime | None]
    # The lowest price seen so far, and where.
    best_price: Mapped[float | None]
    best_store: Mapped[str | None] = mapped_column(String(100))
    best_title: Mapped[str | None] = mapped_column(Text)
    best_url: Mapped[str | None] = mapped_column(Text)
    alerted_price: Mapped[
        float | None
    ]  # the price of the last alert, so the same deal isn't re-sent


class PriceAlert(Base):
    __tablename__ = "price_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(
        ForeignKey("price_watches.id", ondelete="CASCADE"), index=True
    )
    query: Mapped[str] = mapped_column(String(120))
    price: Mapped[float]
    store: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    seen_at: Mapped[
        datetime | None
    ]  # set once V.A.R.A. has told you (in a reply or on the chat page)


class SetupStep(Base):
    """First-run steps the user has completed, such as choosing what V.A.R.A. may access."""

    __tablename__ = "setup_steps"

    key: Mapped[str] = mapped_column(String(30), primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(default=utcnow)


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"  # by you, or by the pause switch
    STOPPED = "stopped"  # hit its step or time limit
    INTERRUPTED = "interrupted"  # the server stopped while it was running


class AgentRun(Base):
    """One request handled by the agent loop (vara/agent.py): the goal, who answered, and how it ended."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(String(20), default=RunStatus.RUNNING)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    client: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    steps: Mapped[int] = mapped_column(default=0)
    max_steps: Mapped[int]
    final_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]

    actions: Mapped[list["ActionRecord"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ActionRecord.id"
    )


class ActionStatus(StrEnum):
    INVALID = "invalid"  # unknown tool or bad arguments; nothing ran
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"  # you said no
    EXPIRED = "expired"  # nobody answered the approval in time
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    DUPLICATE = (
        "duplicate"  # already done; the earlier result was reused instead of running it again
    )


class ActionRecord(Base):
    """The audit trail: one row per action the model asked for, whatever happened to it. Rows waiting
    for approval are the approval queue. Arguments and results are stored redacted."""

    __tablename__ = "action_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    step: Mapped[int]  # the loop step (model turn) that asked for it
    tool_call_id: Mapped[str | None] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(60))
    risk: Mapped[str | None] = mapped_column(String(20))
    arguments: Mapped[str] = mapped_column(Text)  # JSON
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)  # action + arguments, hashed
    description: Mapped[str | None] = mapped_column(Text)  # what it will do, in words
    status: Mapped[ActionStatus] = mapped_column(String(20), index=True)
    # What allowed it to run: "read-only" for safe actions, "approved" for ones you approved.
    permission: Mapped[str | None] = mapped_column(String(40))
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)  # what the model was told
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    run: Mapped[AgentRun] = relationship(back_populates="actions")


class AgentControl(Base):
    """Switches that outlive a restart, such as the pause on all agent activity."""

    __tablename__ = "agent_controls"

    key: Mapped[str] = mapped_column(String(30), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class WeatherSnapshot(Base):
    """One forecast as it was fetched (vara/weather/watch.py). Kept as history, not just the latest, so
    later reports can see what the forecast said before an illness and what actually happened."""

    __tablename__ = "weather_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    latitude: Mapped[float]
    longitude: Mapped[float]
    hours: Mapped[str | None] = mapped_column(Text)  # JSON: the past 24 h and the next 7 days
    outlook: Mapped[str | None] = mapped_column(Text)  # JSON: the 3-day risk, as computed then
    error: Mapped[str | None] = mapped_column(Text)  # set when the fetch failed


class WeatherAlert(Base):
    """A warning that the weather is about to shift. Sent to Discord outside quiet hours, spoken by the
    voice listener when you're active, and mentioned in V.A.R.A.'s next reply."""

    __tablename__ = "weather_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    target_date: Mapped[str] = mapped_column(String(10))  # the riskiest day, YYYY-MM-DD (local)
    score: Mapped[int]
    spoken: Mapped[str] = mapped_column(Text)  # a sentence or two for the voice listener
    text: Mapped[str] = mapped_column(Text)  # the full alert, with the checklist
    outlook: Mapped[str] = mapped_column(Text)  # JSON
    test: Mapped[bool] = mapped_column(default=False)
    discord_sent_at: Mapped[datetime | None]
    discord_error: Mapped[str | None] = mapped_column(Text)
    announced_at: Mapped[datetime | None]  # the voice listener said it
    seen_at: Mapped[datetime | None]  # mentioned in a reply
