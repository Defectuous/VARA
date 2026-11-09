"""Price watches and price alerts

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_watches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("query", sa.String(120), nullable=False),
        sa.Column("target_price", sa.Float),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("last_checked_at", sa.DateTime),
        sa.Column("best_price", sa.Float),
        sa.Column("best_store", sa.String(100)),
        sa.Column("best_title", sa.Text),
        sa.Column("best_url", sa.Text),
        sa.Column("alerted_price", sa.Float),
    )
    op.create_table(
        "price_alerts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "watch_id",
            sa.Integer,
            sa.ForeignKey("price_watches.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("query", sa.String(120), nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("store", sa.String(100), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("seen_at", sa.DateTime),
    )


def downgrade() -> None:
    op.drop_table("price_alerts")
    op.drop_table("price_watches")
