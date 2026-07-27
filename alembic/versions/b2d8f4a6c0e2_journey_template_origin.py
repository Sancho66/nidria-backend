"""journey_template.origin — provenance seed|user (note Eric 26/07)

Le discriminant STRUCTUREL du milestone d'onboarding « créer son
parcours » : un parcours seedé par le système ne le valide plus. Backfill
des seedés identifiables : la bibliothèque (is_sample), les clones
sectoriels offerts (agency_id posé + sector posé — la seule provenance de
`sector` sur une ligne d'agence), et le parcours démo legacy (par son nom,
UNE FOIS, ici seulement — le runtime ne s'appuie jamais sur le nom).
Défaut 'user' : un parcours créé/cloné par l'agence est un geste réel.

Revision ID: b2d8f4a6c0e2
Revises: a1c7e9f2b4d8
"""

import sqlalchemy as sa
from alembic import op

revision = "b2d8f4a6c0e2"
down_revision = "a1c7e9f2b4d8"
branch_labels = None
depends_on = None

_LEGACY_DEMO_NAME = "Exemple : Installation à l'étranger"


def upgrade() -> None:
    op.add_column(
        "journey_template",
        sa.Column("origin", sa.String(length=10), server_default=sa.text("'user'"), nullable=False),
    )
    op.execute(
        sa.text(
            "UPDATE journey_template SET origin = 'seed' "
            "WHERE is_sample = true "
            "   OR (agency_id IS NOT NULL AND sector IS NOT NULL) "
            "   OR name = :legacy"
        ).bindparams(legacy=_LEGACY_DEMO_NAME)
    )


def downgrade() -> None:
    op.drop_column("journey_template", "origin")
