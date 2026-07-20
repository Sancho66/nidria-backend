"""add platform_task.assigned_by_agent_id + assigned_at (assignment trace)

Additive, both nullable. The LAST assigner and when — reassignment
overwrites (history, if ever needed, lives in activity_log).

Revision ID: c8f4a2e6b0d4
Revises: b4e0c2a8d6f2
Create Date: 2026-07-21 12:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f4a2e6b0d4"
down_revision: Union[str, Sequence[str], None] = "b4e0c2a8d6f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "platform_task",
        sa.Column("assigned_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "platform_task",
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_platform_task_assigned_by_agent",
        "platform_task",
        "agent",
        ["assigned_by_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_platform_task_assigned_by_agent", "platform_task", type_="foreignkey")
    op.drop_column("platform_task", "assigned_at")
    op.drop_column("platform_task", "assigned_by_agent_id")
