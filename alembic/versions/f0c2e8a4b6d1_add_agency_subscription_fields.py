"""add agency subscription fields (structure F pricing, manual billing)

plan / billing_cycle / seats / prices / founding / converted_at on the
agency root (a 1-1 forever concern, like trial_ends_at - no join tax on
the seat gate). trial_ends_at is UNCHANGED: it stays the pre-conversion
marker. Additive, cleanly reversible; agency RLS state untouched
(columns on an existing table).

Revision ID: f0c2e8a4b6d1
Revises: e1b7c3a9d5f2
Create Date: 2026-07-07 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0c2e8a4b6d1"
down_revision: Union[str, Sequence[str], None] = "e1b7c3a9d5f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agency", sa.Column("plan", sa.String(length=20), nullable=True))
    op.add_column("agency", sa.Column("billing_cycle", sa.String(length=10), nullable=True))
    op.add_column(
        "agency",
        sa.Column("seats_included", sa.Integer(), server_default=sa.text("3"), nullable=False),
    )
    op.add_column(
        "agency",
        sa.Column(
            "founding_free_seats", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "agency",
        sa.Column("base_price_eur", sa.Integer(), server_default=sa.text("99"), nullable=False),
    )
    op.add_column("agency", sa.Column("seat_price_eur", sa.Integer(), nullable=True))
    op.add_column("agency", sa.Column("price_locked_until", sa.Date(), nullable=True))
    op.add_column(
        "agency",
        sa.Column("is_founding", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "agency", sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "agency_founding_free_seats_check",
        "agency",
        "founding_free_seats >= 0 AND founding_free_seats <= 3",
    )


def downgrade() -> None:
    op.drop_constraint("agency_founding_free_seats_check", "agency", type_="check")
    for column in (
        "converted_at",
        "is_founding",
        "price_locked_until",
        "seat_price_eur",
        "base_price_eur",
        "founding_free_seats",
        "seats_included",
        "billing_cycle",
        "plan",
    ):
        op.drop_column("agency", column)
