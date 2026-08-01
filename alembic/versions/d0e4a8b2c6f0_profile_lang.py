"""Complément PATCH fiche — la langue de la fiche.

`client_profile.preferred_lang` (nullable) : le registre de l'AGENCE,
prime sur la préférence du compte à la lecture, ne touche jamais le
compte global. Additive.

Revision ID: d0e4a8b2c6f0
Revises: b8d2f6c0a4e8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0e4a8b2c6f0"
down_revision: str | None = "b8d2f6c0a4e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("client_profile", sa.Column("preferred_lang", sa.String(length=5)))


def downgrade() -> None:
    op.drop_column("client_profile", "preferred_lang")
