"""add_case_billed_price

Revision ID: d5e0f7b3c9a6
Revises: c4d9e6a2b8f5
Create Date: 2026-07-11

The price the agency bills a dossier (Reside: "know what is left at the end").
Two nullable columns on client_case — one price per case, not a ledger; the
costs are the detail. Additive, reversible: drop the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5e0f7b3c9a6"
down_revision: str | Sequence[str] | None = "c4d9e6a2b8f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "client_case",
        sa.Column("billed_amount", sa.Numeric(precision=18, scale=4), nullable=True),
    )
    op.add_column("client_case", sa.Column("billed_currency", sa.String(length=3), nullable=True))


def downgrade() -> None:
    op.drop_column("client_case", "billed_currency")
    op.drop_column("client_case", "billed_amount")
