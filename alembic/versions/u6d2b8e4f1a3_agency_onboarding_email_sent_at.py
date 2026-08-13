"""Mail d'onboarding J+10 min (spec Eric 13/08) — le drapeau d'idempotence.

UNE colonne nullable sur `agency` : `onboarding_email_sent_at`, posée APRÈS
un envoi réussi (jamais avant — un envoi qui échoue doit pouvoir être rejoué
au balayage suivant). Aucun backfill : le balayage ne regarde que les agences
créées dans sa fenêtre de rattrapage (24 h par défaut), donc le parc existant
est hors périmètre par construction, sans écrire une fausse date d'envoi.

Revision ID: u6d2b8e4f1a3
Revises: t4f8c2a6d9e1
"""

import sqlalchemy as sa
from alembic import op

revision = "u6d2b8e4f1a3"
down_revision = "t4f8c2a6d9e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agency",
        sa.Column("onboarding_email_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agency", "onboarding_email_sent_at")
