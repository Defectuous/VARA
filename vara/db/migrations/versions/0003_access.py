"""File access permissions and first-run setup

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_grants",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("path", sa.Text),
        sa.Column("granted_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "setup_steps",
        sa.Column("key", sa.String(30), primary_key=True),
        sa.Column("completed_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("setup_steps")
    op.drop_table("access_grants")
