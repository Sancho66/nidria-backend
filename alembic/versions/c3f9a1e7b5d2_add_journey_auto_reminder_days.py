"""add journey_template.auto_reminder_days_1/2 (NID-18, per-journey auto-reminder)

Additive, both NULLABLE. NULL = inherit (agency setting → system default
[20,30]). Every existing journey stays NULL → auto-reminder behaviour is
UNCHANGED. No data touched.

Revision ID: c3f9a1e7b5d2
Revises: b1d7f3a9c2e4
Create Date: 2026-07-23 16:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "c3f9a1e7b5d2"
down_revision: Union[str, Sequence[str], None] = "b1d7f3a9c2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("journey_template", sa.Column("auto_reminder_days_1", sa.Integer(), nullable=True))
    op.add_column("journey_template", sa.Column("auto_reminder_days_2", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("journey_template", "auto_reminder_days_2")
    op.drop_column("journey_template", "auto_reminder_days_1")
