"""add agency.cover_path (client-space cover banner)

Same family as logo_path: nullable private-bucket path, served by
authenticated endpoints only. Additive, cleanly reversible; the agency
table already carries RLS.

Revision ID: f5d2b8e4c1a6
Revises: e4c1a7d3b9f5
Create Date: 2026-07-03 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5d2b8e4c1a6"
down_revision: Union[str, Sequence[str], None] = "e4c1a7d3b9f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agency", sa.Column("cover_path", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("agency", "cover_path")
