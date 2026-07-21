"""add agency.sectors_onboarding_required (self-signup sector onboarding)

Additive. Boolean NOT NULL default false → EVERY existing agency gets
false (never bothered — the guarantee). Only a fresh self-signup agency
is created with true.

Revision ID: f8a4c0e6b2d8
Revises: e6c2a8f4b0d6
Create Date: 2026-07-21 18:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8a4c0e6b2d8"
down_revision: Union[str, Sequence[str], None] = "e6c2a8f4b0d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agency",
        sa.Column(
            "sectors_onboarding_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agency", "sectors_onboarding_required")
