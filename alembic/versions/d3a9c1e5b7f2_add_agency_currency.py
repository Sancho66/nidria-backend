"""add_agency_currency

Revision ID: d3a9c1e5b7f2
Revises: b4f8d2a6c1e9
Create Date: 2026-07-09

Additive: a nullable ISO-4217 currency on the agency, for internal cost
tracking. Existing agencies get NULL (they pick their currency before entering
costs — never a fabricated default). Reversible: drop the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3a9c1e5b7f2"
down_revision: str | Sequence[str] | None = "b4f8d2a6c1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agency", sa.Column("currency", sa.String(length=3), nullable=True))


def downgrade() -> None:
    op.drop_column("agency", "currency")
