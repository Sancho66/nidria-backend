"""add agency.onboarding_dismissed_at (activation checklist dismiss)

NULL = checklist shown; set once by POST /agencies/me/onboarding/dismiss,
no un-dismiss. The checklist state itself is computed live from the
usage milestones/events (no state table). Additive, cleanly reversible.

Revision ID: e1b7c3a9d5f2
Revises: d9f6a2c4e8b1
Create Date: 2026-07-07 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1b7c3a9d5f2"
down_revision: Union[str, Sequence[str], None] = "d9f6a2c4e8b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agency",
        sa.Column("onboarding_dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agency", "onboarding_dismissed_at")
