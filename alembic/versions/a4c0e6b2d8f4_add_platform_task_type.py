"""add platform_task.task_type (Prism: task/call/meeting/follow_up)

Additive: NOT NULL via server_default 'task' — existing rows become
plain tasks, no backfill needed.

Revision ID: a4c0e6b2d8f4
Revises: f0b6d2a8c4e0
Create Date: 2026-07-20 20:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c0e6b2d8f4"
down_revision: Union[str, Sequence[str], None] = "f0b6d2a8c4e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "platform_task",
        sa.Column("task_type", sa.String(length=20), nullable=False, server_default="task"),
    )


def downgrade() -> None:
    op.drop_column("platform_task", "task_type")
