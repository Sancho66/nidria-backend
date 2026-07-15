"""add agency.past_due_since (billing-lock grace anchor)

The billing lock (read-only, never destructive) grants a grace period after
a subscription enters past_due — Paddle poses past_due at the FIRST failed
payment and its dunning runs DURING it, so blocking at J+0 would punish an
expired card instantly. This column anchors the grace: posed at the first
past_due webhook (its occurred_at), cleared by any other status.

Revision ID: d5e9f3a7b1c4
Revises: c4d8e2f6a1b3
Create Date: 2026-07-15 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e9f3a7b1c4"
down_revision: str | Sequence[str] | None = "c4d8e2f6a1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agency", sa.Column("past_due_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("agency", "past_due_since")
