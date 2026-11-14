"""Agent runs, the action audit trail (and approval queue), and agent controls

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "conversation_id", sa.Integer, sa.ForeignKey("conversations.id", ondelete="SET NULL")
        ),
        sa.Column("client", sa.String(64)),
        sa.Column("provider", sa.String(50)),
        sa.Column("model", sa.String(100)),
        sa.Column("steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_steps", sa.Integer, nullable=False),
        sa.Column("final_text", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime),
    )
    op.create_table(
        "action_records",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("step", sa.Integer, nullable=False),
        sa.Column("tool_call_id", sa.String(100)),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("risk", sa.String(20)),
        sa.Column("arguments", sa.Text, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, index=True),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("permission", sa.String(40)),
        sa.Column("decided_at", sa.DateTime),
        sa.Column("decision_note", sa.Text),
        sa.Column("result", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime),
        sa.Column("finished_at", sa.DateTime),
    )
    op.create_table(
        "agent_controls",
        sa.Column("key", sa.String(30), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("agent_controls")
    op.drop_table("action_records")
    op.drop_table("agent_runs")
