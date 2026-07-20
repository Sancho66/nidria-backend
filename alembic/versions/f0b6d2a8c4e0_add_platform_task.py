"""add platform_task (superadmin ops backlog, Prism tasks port v1)

PLATFORM-scoped table (job_config precedent): no agency tenant — the
nullable agency_id SET NULL is the subject of the work, never a scope.
RLS enabled (post-sweep rule); the app connection bypasses it, the
barrier is the platform.task_manage permission.

Revision ID: f0b6d2a8c4e0
Revises: e2c7a9f5b3d1
Create Date: 2026-07-20 18:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0b6d2a8c4e0"
down_revision: Union[str, Sequence[str], None] = "e2c7a9f5b3d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_task",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="todo"),
        sa.Column("priority", sa.String(length=20), nullable=False, server_default="medium"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agency_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_to_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completed_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agency_id"], ["agency.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assigned_to_agent_id"], ["agent.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_agent_id"], ["agent.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["completed_by_agent_id"], ["agent.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_platform_task_status"), "platform_task", ["status"])
    op.create_index(op.f("ix_platform_task_due_at"), "platform_task", ["due_at"])
    op.create_index(op.f("ix_platform_task_agency_id"), "platform_task", ["agency_id"])
    op.create_index(
        op.f("ix_platform_task_assigned_to_agent_id"), "platform_task", ["assigned_to_agent_id"]
    )
    op.execute("ALTER TABLE platform_task ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("platform_task")
