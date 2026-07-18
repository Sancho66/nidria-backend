"""agency.is_internal (lifetime internal agencies, outside billing)

The clean flag the manual-converted trick approximated: internal agencies
(Nidria Demo today) live outside billing (409 billing.internal_agency,
never auto-blocked, never nurtured) and show a distinct "Interne" badge
in the admin table. Backfills Nidria Demo — the one internal agency.

Revision ID: e2a8c4f0b6d3
Revises: d1f7b3e9a5c2
Create Date: 2026-07-18 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a8c4f0b6d3"
down_revision: str | Sequence[str] | None = "d1f7b3e9a5c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agency",
        sa.Column("is_internal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute("UPDATE agency SET is_internal = true WHERE slug = 'nidria-demo'")


def downgrade() -> None:
    op.drop_column("agency", "is_internal")
