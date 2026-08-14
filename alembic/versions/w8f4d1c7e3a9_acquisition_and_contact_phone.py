"""L'acquisition traçable + le téléphone de contact (lot 13/08).

TOUT est additif et nullable — aucun backfill possible ni souhaitable : la
source d'une inscription est PÉRISSABLE (elle n'existe que dans l'URL
d'arrivée et le referrer), donc les agences déjà créées n'en ont pas et n'en
auront jamais. Une valeur inventée serait pire qu'un NULL honnête.

Sur `agency` :
- utm_source / utm_medium / utm_campaign / referrer : d'où vient l'inscription,
  bornés à 200 comme à l'entrée (entrée publique non authentifiée) ;
- acquisition_captured_at : la PREMIÈRE TOUCHE, distincte de created_at — le
  délai entre les deux est un signal ;
- contact_phone : le téléphone rejoint l'identité légale, à côté de
  contact_email (décision Alexandre) — jeton {contact_phone} et segment du
  modèle généré.

Sur `signup_verification` : les mêmes champs d'acquisition, capturés dès
l'étape 1 et reportés sur l'agence à l'étape 3. La ligne est supprimée à la
complétion, donc ces colonnes ne survivent jamais à l'inscription.

Revision ID: w8f4d1c7e3a9
Revises: v7e3c9d5a2b8
"""

import sqlalchemy as sa

from alembic import op

revision = "w8f4d1c7e3a9"
down_revision = "v7e3c9d5a2b8"
branch_labels = None
depends_on = None

_ACQUISITION = (
    ("utm_source", sa.String(200)),
    ("utm_medium", sa.String(200)),
    ("utm_campaign", sa.String(200)),
    ("referrer", sa.String(200)),
    ("acquisition_captured_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for table in ("agency", "signup_verification"):
        for name, type_ in _ACQUISITION:
            op.add_column(table, sa.Column(name, type_, nullable=True))
    op.add_column("agency", sa.Column("contact_phone", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("agency", "contact_phone")
    for table in ("agency", "signup_verification"):
        for name, _type in _ACQUISITION:
            op.drop_column(table, name)
