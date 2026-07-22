"""add journey_template.sector (multi-sector library, STRUCTURAL ONLY)

Additive column, nullable. Mirror of `country`. The 7 GLOBAL sector
templates are seeded at BOOT (seed_sector_templates), agency_id NULL —
NEVER via a data migration into agencies. This migration touches NO
agency, NO journey, NO case (the critical invariant).

Revision ID: a0c6e2f8b4d0
Revises: f8a4c0e6b2d8
Create Date: 2026-07-22 10:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a0c6e2f8b4d0"
down_revision: Union[str, Sequence[str], None] = "f8a4c0e6b2d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("journey_template", sa.Column("sector", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("journey_template", "sector")
