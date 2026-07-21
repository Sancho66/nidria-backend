"""add agency.sectors (multi-sector groundwork, inert)

Additive. NOT NULL JSONB default '[]' — every existing agency becomes
neutral ([]). INERT: nothing consumes it yet.

Revision ID: e6c2a8f4b0d6
Revises: d2f6a8c4e0b2
Create Date: 2026-07-21 17:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6c2a8f4b0d6"
down_revision: Union[str, Sequence[str], None] = "d2f6a8c4e0b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agency",
        sa.Column(
            "sectors",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agency", "sectors")
