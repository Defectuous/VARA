"""Conversations, messages, memories, and full-text search

Revision ID: 0001
Revises:
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# External-content FTS5 indexes, kept in sync with their tables by triggers.
_FTS = {"messages_fts": "messages", "memories_fts": "memories"}


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Integer,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("provider", sa.String(50)),
        sa.Column("model", sa.String(100)),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("cost_usd", sa.Float),
    )
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column(
            "source_message_id", sa.Integer, sa.ForeignKey("messages.id", ondelete="SET NULL")
        ),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    for fts, table in _FTS.items():
        op.execute(
            f"CREATE VIRTUAL TABLE {fts} USING fts5(content, content='{table}', content_rowid='id')"
        )
        # Remove deleted text from the index immediately instead of leaving it in old segments.
        op.execute(f"INSERT INTO {fts}({fts}, rank) VALUES ('secure-delete', 1)")
        op.execute(
            f"CREATE TRIGGER {table}_fts_insert AFTER INSERT ON {table} BEGIN "
            f"INSERT INTO {fts}(rowid, content) VALUES (new.id, new.content); END"
        )
        op.execute(
            f"CREATE TRIGGER {table}_fts_delete AFTER DELETE ON {table} BEGIN "
            f"INSERT INTO {fts}({fts}, rowid, content) VALUES ('delete', old.id, old.content); END"
        )
        op.execute(
            f"CREATE TRIGGER {table}_fts_update AFTER UPDATE OF content ON {table} BEGIN "
            f"INSERT INTO {fts}({fts}, rowid, content) VALUES ('delete', old.id, old.content); "
            f"INSERT INTO {fts}(rowid, content) VALUES (new.id, new.content); END"
        )


def downgrade() -> None:
    for fts, table in _FTS.items():
        for suffix in ("insert", "delete", "update"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_fts_{suffix}")
        op.execute(f"DROP TABLE IF EXISTS {fts}")
    op.drop_table("memories")
    op.drop_table("messages")
    op.drop_table("conversations")
