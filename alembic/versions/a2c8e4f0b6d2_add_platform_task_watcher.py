"""add platform_task_watcher (observers on platform tasks)

Nidria-pure adaptation (Prism has no watcher concept). Additive; both
FKs CASCADE, UNIQUE(task_id, agent_id).

Revision ID: a2c8e4f0b6d2
Revises: f2a6c0e8b4d0
Create Date: 2026-07-20 17:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2c8e4f0b6d2"
down_revision: Union[str, Sequence[str], None] = "f2a6c0e8b4d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_task_watcher",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["platform_task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "agent_id"),
    )
    op.create_index(op.f("ix_platform_task_watcher_task_id"), "platform_task_watcher", ["task_id"])
    op.create_index(
        op.f("ix_platform_task_watcher_agent_id"), "platform_task_watcher", ["agent_id"]
    )
    op.execute("ALTER TABLE platform_task_watcher ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("platform_task_watcher")
