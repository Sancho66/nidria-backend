"""Accès à vie : le drapeau qui dit pourquoi une agence n'a plus d'échéance.

`trial_ends_at = NULL` signifiait déjà « pas de calendrier, jamais bloquée »
partout où l'essai se lit — mais il ne disait pas POURQUOI. Une agence sans
date pouvait être un cadeau assumé comme une anomalie de création hors
wizard, et la table superadmin les affichait pareil (« unknown »).

Ce drapeau tranche : posé, il vaut décision ; absent, une agence sans date
reste l'anomalie à signaler. Il ne remplace pas le NULL, il l'explique — le
geste pose les deux ensemble.

Distinct d'`is_internal` (agence MAISON, hors facturation) : une agence à
vie reste un client, avec son plan, ses crédits et ses statistiques.

Colonne booléenne NOT NULL default false : aucune agence existante ne
change d'état. Rejouable ; downgrade complet.

Revision ID: k4f6a2c8e0d2
Revises: j2e4c8a0b6d4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k4f6a2c8e0d2"
down_revision: str | None = "j2e4c8a0b6d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agency",
        sa.Column(
            "lifetime_access",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agency", "lifetime_access")
