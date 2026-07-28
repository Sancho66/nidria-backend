"""LOT 6 — la source du document signable

`signature_document_path`/`filename` sur step_requirement (le PDF que
l'agence fait signer, uploadé au template) et case_step_requirement (le
snapshot à la matérialisation). Additif pur, nullable, zéro backfill.

Revision ID: e8b4d0f6a2c4
Revises: d6a2c8e4f0b2
"""

import sqlalchemy as sa
from alembic import op

revision = "e8b4d0f6a2c4"
down_revision = "d6a2c8e4f0b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("step_requirement", "case_step_requirement"):
        op.add_column(
            table, sa.Column("signature_document_path", sa.String(length=500), nullable=True)
        )
        op.add_column(
            table, sa.Column("signature_document_filename", sa.String(length=255), nullable=True)
        )


def downgrade() -> None:
    for table in ("case_step_requirement", "step_requirement"):
        op.drop_column(table, "signature_document_filename")
        op.drop_column(table, "signature_document_path")
