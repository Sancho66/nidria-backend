"""Annuaire F4 (complément 2) — création directe de fiche.

`client_profile.expat_user_id` devient NULLABLE (une fiche peut naître
sans compte — prospect à froid) + identité propre de la fiche
(first_name/last_name/email) posée à la création directe, servie tant que
la liaison différée n'a pas eu lieu. Additive, aucune donnée touchée.

Revision ID: d4a8b2c6e0f2
Revises: c8f2a6d0e4b8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a8b2c6e0f2"
down_revision: str | None = "c8f2a6d0e4b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("client_profile", "expat_user_id", nullable=True)
    op.add_column("client_profile", sa.Column("first_name", sa.String(length=100), nullable=True))
    op.add_column("client_profile", sa.Column("last_name", sa.String(length=100), nullable=True))
    op.add_column("client_profile", sa.Column("email", sa.String(length=255), nullable=True))
    op.create_index("ix_client_profile_email", "client_profile", ["email"])


def downgrade() -> None:
    op.drop_index("ix_client_profile_email", table_name="client_profile")
    op.drop_column("client_profile", "email")
    op.drop_column("client_profile", "last_name")
    op.drop_column("client_profile", "first_name")
    # Les fiches jamais liées ne peuvent pas survivre au retour NOT NULL.
    op.execute("DELETE FROM client_profile WHERE expat_user_id IS NULL")
    op.alter_column("client_profile", "expat_user_id", nullable=False)
