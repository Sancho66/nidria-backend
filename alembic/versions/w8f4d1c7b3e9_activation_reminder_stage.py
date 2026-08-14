"""Relance d'activation (lot 14/08) — le palier déjà envoyé.

UNE colonne sur `case_invitation` : `activation_reminder_stage` (0 = aucune
relance, 3 = J+3 envoyée, 7 = J+7 envoyée, puis plus jamais). NOT NULL avec
défaut 0, donc le parc existant démarre à zéro : les invitations en souffrance
de juillet recevront leur relance au premier balayage — c'est exactement ce
qu'on veut (six clients bloqués), et le palier J+7 est le dernier.

Revision ID: w8f4d1c7b3e9
Revises: w8f4d1c7e3a9
"""

import sqlalchemy as sa
from alembic import op

revision = "w8f4d1c7b3e9"
down_revision = "w8f4d1c7e3a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "case_invitation",
        sa.Column(
            "activation_reminder_stage",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("case_invitation", "activation_reminder_stage")
