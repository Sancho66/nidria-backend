"""Lot conditions au nom de l'agence (13/08) — l'identité légale de l'agence
+ le drapeau de relecture des conditions.

8 colonnes légales OPTIONNELLES (le générateur de modèle remplit ce qui
existe, laisse un marqueur visible pour le reste) + `client_terms_reviewed_at`
(le geste « J'ai vérifié », NULL = généré non relu → tâche dashboard +
onboarding). Toutes nullables, aucun défaut : l'existant reste NULL, la
saisie vient de l'écran « Profil & marque ».

Revision ID: t4f8c2a6d9e1
Revises: s3e7b1d9c5f2
"""

import sqlalchemy as sa
from alembic import op

revision = "t4f8c2a6d9e1"
down_revision = "s3e7b1d9c5f2"
branch_labels = None
depends_on = None

_LEGAL_COLUMNS = [
    ("legal_name", sa.String(length=200)),
    ("legal_form", sa.String(length=100)),
    ("registration_number", sa.String(length=100)),
    ("address", sa.String(length=255)),
    ("city", sa.String(length=100)),
    ("postal_code", sa.String(length=20)),
    ("country", sa.String(length=2)),
    ("contact_email", sa.String(length=255)),
]


def upgrade() -> None:
    for name, type_ in _LEGAL_COLUMNS:
        op.add_column("agency", sa.Column(name, type_, nullable=True))
    op.add_column(
        "agency",
        sa.Column("client_terms_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agency", "client_terms_reviewed_at")
    for name, _ in reversed(_LEGAL_COLUMNS):
        op.drop_column("agency", name)
