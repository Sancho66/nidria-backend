"""add the appointment block to platform_task (Prism: scheduled_at,
scheduled_timezone, duration_minutes, location)

Additive, all nullable — a plain task stays a plain task.

Revision ID: c6a2e8b4d0f6
Revises: a4c0e6b2d8f4
Create Date: 2026-07-20 21:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6a2e8b4d0f6"
down_revision: Union[str, Sequence[str], None] = "a4c0e6b2d8f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("platform_task", sa.Column("scheduled_at", sa.DateTime(timezone=True)))
    op.add_column("platform_task", sa.Column("scheduled_timezone", sa.String(length=50)))
    op.add_column("platform_task", sa.Column("duration_minutes", sa.Integer()))
    op.add_column("platform_task", sa.Column("location", sa.String(length=500)))


def downgrade() -> None:
    op.drop_column("platform_task", "location")
    op.drop_column("platform_task", "duration_minutes")
    op.drop_column("platform_task", "scheduled_timezone")
    op.drop_column("platform_task", "scheduled_at")
