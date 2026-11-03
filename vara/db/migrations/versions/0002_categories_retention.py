"""Memory categories and retention rules

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Defaults keep everything except unapproved suggestions. None = keep until deleted.
_DEFAULT_RULES = {
    "conversations": None,
    "proposed": 30,
    "identity": None,
    "preference": None,
    "relationship": None,
    "location": None,
    "work": None,
    "health": None,
    "finance": None,
    "commitment": None,
    "other": None,
}


# Plain ADD/DROP COLUMN, never batch mode: rebuilding `memories` would drop its FTS triggers.


def upgrade() -> None:
    op.add_column(
        "memories", sa.Column("category", sa.String(30), nullable=False, server_default="other")
    )
    # A pinned memory is kept regardless of any retention rule.
    op.add_column(
        "memories", sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false())
    )
    rules = op.create_table(
        "retention_rules",
        sa.Column("key", sa.String(30), primary_key=True),
        sa.Column("keep_days", sa.Integer),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.bulk_insert(rules, [{"key": k, "keep_days": v} for k, v in _DEFAULT_RULES.items()])


def downgrade() -> None:
    op.drop_table("retention_rules")
    op.drop_column("memories", "pinned")
    op.drop_column("memories", "category")
