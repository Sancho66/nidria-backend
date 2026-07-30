"""Lot contreseing agence (30/07).

`document_template.agency_countersigns` (bool, défaut false) ; le siège de
signature devient personne OU agent : `signature_signer.case_person_id`
nullable + `agent_id` (FK agent) + CHECK exactement l'un des deux.

Revision ID: b6e0c4a8d2f6
Revises: a2c6e0b4d8f2
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b6e0c4a8d2f6"
down_revision = "a2c6e0b4d8f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_template",
        sa.Column(
            "agency_countersigns", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.alter_column("signature_signer", "case_person_id", nullable=True)
    op.add_column(
        "signature_signer",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_signature_signer_agent",
        "signature_signer",
        "agent",
        ["agent_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_signature_signer_agent_id", "signature_signer", ["agent_id"])
    op.create_check_constraint(
        "signer_person_xor_agent",
        "signature_signer",
        "num_nonnulls(case_person_id, agent_id) = 1",
    )


def downgrade() -> None:
    op.drop_constraint("signer_person_xor_agent", "signature_signer", type_="check")
    op.drop_index("ix_signature_signer_agent_id", table_name="signature_signer")
    op.drop_constraint("fk_signature_signer_agent", "signature_signer", type_="foreignkey")
    op.drop_column("signature_signer", "agent_id")
    op.execute("DELETE FROM signature_signer WHERE case_person_id IS NULL")
    op.alter_column("signature_signer", "case_person_id", nullable=False)
    op.drop_column("document_template", "agency_countersigns")
