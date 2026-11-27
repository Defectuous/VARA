"""Weather watch: forecast snapshots and weather alerts

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weather_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "fetched_at", sa.DateTime, nullable=False, server_default=sa.func.now(), index=True
        ),
        sa.Column("latitude", sa.Float, nullable=False),
        sa.Column("longitude", sa.Float, nullable=False),
        sa.Column("hours", sa.Text),
        sa.Column("outlook", sa.Text),
        sa.Column("error", sa.Text),
    )
    op.create_table(
        "weather_alerts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now(), index=True
        ),
        sa.Column("target_date", sa.String(10), nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("spoken", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("outlook", sa.Text, nullable=False),
        sa.Column("test", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("discord_sent_at", sa.DateTime),
        sa.Column("discord_error", sa.Text),
        sa.Column("announced_at", sa.DateTime),
        sa.Column("seen_at", sa.DateTime),
    )


def downgrade() -> None:
    op.drop_table("weather_alerts")
    op.drop_table("weather_snapshots")
