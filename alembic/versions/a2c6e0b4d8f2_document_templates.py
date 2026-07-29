"""Méga-lot modèles de documents — bibliothèque + câblage exigences.

`document_template` (bibliothèque par agence : PDF source chez nous, ref
provider opaque, constat builder fields_configured/roles_count) ;
`document_template_id` sur step_requirement (RESTRICT — défense sous le
409 applicatif) et case_step_requirement (SET NULL — l'historique ne
bloque jamais). Le chemin PDF-direct MEURT : DROP des colonnes
signature_document_path/filename des deux tables (verdict prod : 0 ligne
signable, 0 PDF-direct, 0 demande — suppression propre, pas de backfill).

Revision ID: a2c6e0b4d8f2
Revises: e8b4d0f6a2c4
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a2c6e0b4d8f2"
down_revision = "e8b4d0f6a2c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_template",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agency.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("storage_path", sa.String(length=500), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column(
            "provider", sa.String(length=20), server_default="docuseal", nullable=False
        ),
        sa.Column("provider_template_ref", sa.String(length=100), nullable=False),
        sa.Column(
            "fields_configured", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("roles_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.create_index("ix_document_template_agency_id", "document_template", ["agency_id"])
    op.execute('ALTER TABLE "document_template" ENABLE ROW LEVEL SECURITY')

    op.add_column(
        "step_requirement",
        sa.Column("document_template_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_step_requirement_document_template",
        "step_requirement",
        "document_template",
        ["document_template_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "case_step_requirement",
        sa.Column("document_template_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_case_step_requirement_document_template",
        "case_step_requirement",
        "document_template",
        ["document_template_id"],
        ["id"],
        ondelete="SET NULL",
    )

    for table in ("case_step_requirement", "step_requirement"):
        op.drop_column(table, "signature_document_filename")
        op.drop_column(table, "signature_document_path")


def downgrade() -> None:
    for table in ("step_requirement", "case_step_requirement"):
        op.add_column(
            table, sa.Column("signature_document_path", sa.String(length=500), nullable=True)
        )
        op.add_column(
            table, sa.Column("signature_document_filename", sa.String(length=255), nullable=True)
        )
    op.drop_constraint(
        "fk_case_step_requirement_document_template", "case_step_requirement", type_="foreignkey"
    )
    op.drop_column("case_step_requirement", "document_template_id")
    op.drop_constraint(
        "fk_step_requirement_document_template", "step_requirement", type_="foreignkey"
    )
    op.drop_column("step_requirement", "document_template_id")
    op.drop_index("ix_document_template_agency_id", table_name="document_template")
    op.drop_table("document_template")
