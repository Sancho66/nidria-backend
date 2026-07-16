"""drop agency.seats_included (the constant is the truth)

Grid nidria.com/#tarifs (2026-07): included seats become a PLAN property
(3 cabinet / 6 agence — SEATS_INCLUDED_BY_PLAN in agencies_manager), no
longer a per-row copy. The column was written by nobody (default 3) and
read only by the seat derivation; keeping it "in sync" would be a second
truth waiting to diverge. Dropping it upgrades every future Agence
conversion to 6 included seats by construction — verified: no prod agency
carries a plan today, zero rows affected in practice.

Revision ID: e6f2a8c4d0b3
Revises: d5e9f3a7b1c4
Create Date: 2026-07-17 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f2a8c4d0b3"
down_revision: str | Sequence[str] | None = "d5e9f3a7b1c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("agency", "seats_included")


def downgrade() -> None:
    op.add_column(
        "agency",
        sa.Column("seats_included", sa.Integer(), server_default=sa.text("3"), nullable=False),
    )
