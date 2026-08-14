"""Modèles de document : l'état de vie draft|active (lot 14/08).

POURQUOI. La modale « Nouveau modèle » créait le modèle AVANT que l'agence
n'ait posé la moindre zone, parce que le builder embeddé du provider refuse
de s'ouvrir sans un document déjà matérialisé chez lui (son jeton exige
`template_id` ou `document_urls` — le geste ne peut pas précéder ce qui le
rend possible). Fermer le builder laissait donc un modèle vide DANS la
bibliothèque de l'agence. On ne peut pas ne rien créer ; on peut ne rien
MONTRER : le modèle naît `draft`, il est promu `active` au premier
builder-sync qui constate une zone posée, et le janitor emporte les
brouillons abandonnés.

LE BACKFILL N'EST PAS UNE FORMALITÉ. Le défaut de la colonne est `draft`
parce que c'est le bon état pour les lignes À VENIR. Appliqué tel quel au
parc EXISTANT, il ferait disparaître de leur bibliothèque des modèles que
les agences utilisent aujourd'hui. D'où l'UPDATE explicite ci-dessous :
tout ce qui existe au moment de la migration est, par définition, déjà dans
la bibliothèque — donc `active`.

Constat prod au moment d'écrire (SELECT en lecture seule) : 1 seul modèle,
configuré, 0 fantôme. Le backfill protège cette ligne-là. Il n'y a AUCUN
marquage rétroactif de fantômes à faire — le passif est nul, et écrire une
migration de données pour classer zéro ligne serait du risque sans objet.

Revision ID: y1b6c3d9e5f2
Revises: x9a5e2f8c4d6
"""

import sqlalchemy as sa

from alembic import op

revision = "y1b6c3d9e5f2"
down_revision = "x9a5e2f8c4d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_template",
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default="draft",
        ),
    )
    # Le parc existant EST la bibliothèque : il devient actif, sans quoi les
    # modèles en service disparaîtraient de la liste des agences.
    op.execute("UPDATE document_template SET state = 'active'")
    op.create_index(
        "ix_document_template_state",
        "document_template",
        ["state"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_template_state", table_name="document_template")
    op.drop_column("document_template", "state")
