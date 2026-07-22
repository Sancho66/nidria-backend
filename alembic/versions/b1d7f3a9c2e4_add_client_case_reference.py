"""add client_case.reference (agency internal case reference, free text)

Additive, nullable, NOT unique. Free-text human label — never an identifier.
No agency data touched (all existing rows → reference NULL).

Revision ID: b1d7f3a9c2e4
Revises: a0c6e2f8b4d0
Create Date: 2026-07-22 11:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1d7f3a9c2e4"
down_revision: Union[str, Sequence[str], None] = "a0c6e2f8b4d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("client_case", sa.Column("reference", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("client_case", "reference")
