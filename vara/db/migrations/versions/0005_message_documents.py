"""Word and PDF documents made from a message

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A JSON list of file names in data/documents (vara/documents.py).
    op.add_column("messages", sa.Column("documents", sa.Text))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("documents")
