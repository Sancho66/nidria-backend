"""Trace des suppressions de masse de fiches (lot suppression par filtre).

`activity_log` est case-scopé : une fiche SANS dossier — la seule qu'on
ait le droit de supprimer — n'y laisse rien. Une suppression de masse ne
laissait donc aucune trace, seulement l'absence. Cette table garde le
geste : qui, quand, sur quel critère, combien.

Insert-only, acteur figé sur place (UUID nu + email verbatim) — même
grammaire d'audit qu'`agency_deletion_log`. Aucune donnée à reprendre :
la table naît vide, l'endpoint qui l'écrit arrive dans le même lot.

Rejouable ; downgrade complet.

Revision ID: j2e4c8a0b6d4
Revises: i6d0b2e8f4a2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "j2e4c8a0b6d4"
down_revision: str | None = "i6d0b2e8f4a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bulk_deletion_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agency.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity", sa.String(length=30), nullable=False),
        # Pas de FK : un agent qui part ne doit pas emporter le nom de
        # celui qui a fait le geste.
        sa.Column("performed_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("performed_by_email", sa.String(length=255), nullable=False),
        sa.Column(
            "selector",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("matching", sa.Integer(), nullable=False),
        sa.Column("protected", sa.Integer(), nullable=False),
        sa.Column("deletable", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_bulk_deletion_log_agency_id", "bulk_deletion_log", ["agency_id"], unique=False
    )
    op.create_index(
        "ix_bulk_deletion_log_agency_created",
        "bulk_deletion_log",
        ["agency_id", "created_at"],
        unique=False,
    )
    # Invariant du repo : aucune table nue sous Supabase (PostgREST
    # exposerait la table sans politique). Il vaut d'autant plus ici :
    # cette table est un JOURNAL D'AUDIT, la dernière chose qu'on veut
    # voir lisible ou modifiable par un chemin direct.
    op.execute('ALTER TABLE "bulk_deletion_log" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_index("ix_bulk_deletion_log_agency_created", table_name="bulk_deletion_log")
    op.drop_index("ix_bulk_deletion_log_agency_id", table_name="bulk_deletion_log")
    op.drop_table("bulk_deletion_log")
