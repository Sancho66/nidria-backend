"""Alerte interne à chaque inscription d'agence (demande Eric 13/08) — le drapeau.

UNE colonne nullable sur `agency` : `signup_alert_sent_at`, posée APRÈS un
envoi réussi. Distincte de `onboarding_email_sent_at` À DESSEIN : ce sont deux
mails, deux destinataires (l'un part À l'agence à J+10 min, l'autre à l'équipe
immédiatement) et deux cycles de vie — les confondre rendrait impossible de
rejouer l'un sans l'autre.

Aucun backfill : le parc existant garde NULL et ne déclenche rien, parce que
l'alerte n'est émise que dans le chemin d'inscription lui-même (jamais par un
balayage). Une agence créée hier ne peut donc pas produire d'alerte tardive.

Revision ID: v7e3c9d5a2b8
Revises: u6d2b8e4f1a3
"""

import sqlalchemy as sa
from alembic import op

revision = "v7e3c9d5a2b8"
down_revision = "u6d2b8e4f1a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agency",
        sa.Column("signup_alert_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agency", "signup_alert_sent_at")
