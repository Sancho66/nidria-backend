"""Modèles de message : les deux étiquettes (lot 14/08).

DEUX colonnes NULLABLES sur `message_template` — `language` et `channel`.
Additives et sans défaut : le parc existant reste non étiqueté, ce qui est
exactement son état de fait (rien ne les renseignait). NULL n'est pas un trou
à combler, c'est « tous canaux / non précisé ».

Ce sont des ÉTIQUETTES DE RECHERCHE, pas des règles : elles servent à
retrouver un modèle dans une liste qui grandit, elles ne filtrent ni ne
contraignent la création d'un rappel.

PAS de colonne `subject` : voir la note du modèle — le sujet du mail est la
chrome, un objet stocké ici serait ignoré à l'envoi.

Revision ID: x9a5e2f8c4d6
Revises: w8f4d1c7b3e9
"""

import sqlalchemy as sa

from alembic import op

revision = "x9a5e2f8c4d6"
down_revision = "w8f4d1c7b3e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("message_template", sa.Column("language", sa.String(length=5), nullable=True))
    op.add_column("message_template", sa.Column("channel", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("message_template", "channel")
    op.drop_column("message_template", "language")
