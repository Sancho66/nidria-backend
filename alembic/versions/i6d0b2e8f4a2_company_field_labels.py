"""Labels des clés de sack société (demande design A, 03/08).

Le label choisi à la création de champ depuis la grille d'import se
perdait côté société (le sack ne porte que des valeurs nues). Verdict de
la forme la plus simple : une TABLE DE LABELS D'AGENCE — une vérité par
(agence, clé), jamais une copie par société ; le kind de la naissance
voyage avec (les imports suivants coercent comme à la création).

Aucune donnée à reprendre : la création depuis la grille société n'est
pas encore déployée (lot grille non commité), les clés libres de prod
n'ont jamais eu de label (constat nommé au rapport).

Rejouable ; downgrade complet.

Revision ID: i6d0b2e8f4a2
Revises: h8c2a6e0d4b8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "i6d0b2e8f4a2"
down_revision: str | None = "h8c2a6e0d4b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_field_label",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agency.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False, server_default="text"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("agency_id", "key", name="uq_company_field_label"),
    )
    op.create_index("ix_company_field_label_agency_id", "company_field_label", ["agency_id"])
    # Invariant du repo : aucune table nue sous Supabase (PostgREST
    # exposerait la table sans politique) — la frontière effective reste
    # applicative, cette ligne ferme la porte du chemin direct.
    op.execute('ALTER TABLE "company_field_label" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_index("ix_company_field_label_agency_id", table_name="company_field_label")
    op.drop_table("company_field_label")
