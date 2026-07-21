"""add platform_task.estimated_minutes (effort estimate, informative)

Additive, nullable. Canonical unit = minutes. Never read by
overdue/order/timeline (those stay on due_at only) — display field.

Revision ID: d2f6a8c4e0b2
Revises: f4b8d0a6c2e8
Create Date: 2026-07-21 16:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2f6a8c4e0b2"
down_revision: Union[str, Sequence[str], None] = "f4b8d0a6c2e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("platform_task", sa.Column("estimated_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("platform_task", "estimated_minutes")
