"""add platform_task_attachment (Prism attachments port, dedicated table)

Additive. DB CASCADE from platform_task cleans the rows; the storage
blobs are deleted applicatively by the manager before the task row.

Revision ID: d0f6b2c8a4e2
Revises: c6a2e8b4d0f6
Create Date: 2026-07-20 23:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0f6b2c8a4e2"
down_revision: Union[str, Sequence[str], None] = "c6a2e8b4d0f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_task_attachment",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.String(length=500), nullable=False),
        sa.Column("uploaded_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["platform_task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_agent_id"], ["agent.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_path"),
    )
    op.create_index(
        op.f("ix_platform_task_attachment_task_id"), "platform_task_attachment", ["task_id"]
    )
    op.execute("ALTER TABLE platform_task_attachment ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("platform_task_attachment")
