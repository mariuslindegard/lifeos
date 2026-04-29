"""Add chat analysis tracking and agent run cursors.

Revision ID: 0003_chat_analysis_tracking
Revises: 0002_dynamic_dashboard
Create Date: 2026-04-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_chat_analysis_tracking"
down_revision: Union[str, None] = "0002_dynamic_dashboard"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("analysis_status", sa.String(length=32), nullable=False, server_default="pending"))
    op.add_column("chat_messages", sa.Column("analyzed_at", sa.DateTime(timezone=True)))
    op.add_column("chat_messages", sa.Column("analysis_version", sa.String(length=32), nullable=False, server_default="v1"))
    op.add_column("chat_messages", sa.Column("analysis_error", sa.Text()))
    op.add_column("agent_runs", sa.Column("mode", sa.String(length=64)))
    op.add_column("agent_runs", sa.Column("input_message_ids", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("agent_runs", sa.Column("output_card_ids", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("agent_runs", "output_card_ids")
    op.drop_column("agent_runs", "input_message_ids")
    op.drop_column("agent_runs", "mode")
    op.drop_column("chat_messages", "analysis_error")
    op.drop_column("chat_messages", "analysis_version")
    op.drop_column("chat_messages", "analyzed_at")
    op.drop_column("chat_messages", "analysis_status")

