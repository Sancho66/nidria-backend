"""Keep one-off signature PDFs scoped to one case, person and step.

Revision ID: p9a5d1f7b3c0
Revises: n8f4c0e6a2b9
"""

from alembic import op
import sqlalchemy as sa

revision = "p9a5d1f7b3c0"
down_revision = "n8f4c0e6a2b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column, target in (
        ("case_id", "client_case.id"),
        ("case_step_progress_id", "case_step_progress.id"),
        ("person_id", "case_person.id"),
    ):
        op.add_column("document_template", sa.Column(column, sa.Uuid(), nullable=True))
        table, key = target.split(".")
        op.create_foreign_key(
            f"fk_document_template_{column}_{table}",
            "document_template",
            table,
            [column],
            [key],
            ondelete="CASCADE",
        )
    op.add_column("document_template", sa.Column("submitted_at", sa.DateTime(timezone=True)))
    op.create_index("ix_document_template_case_id", "document_template", ["case_id"])
    op.create_check_constraint(
        "one_off_scope_complete",
        "document_template",
        "(case_id IS NULL AND case_step_progress_id IS NULL AND person_id IS NULL AND submitted_at IS NULL)"
        " OR (case_id IS NOT NULL AND case_step_progress_id IS NOT NULL AND person_id IS NOT NULL)",
    )


def downgrade() -> None:
    # A downgrade must never turn private documents into reusable templates.
    op.execute("DELETE FROM document_template WHERE case_id IS NOT NULL")
    op.drop_constraint(
        op.f("ck_document_template_one_off_scope_complete"), "document_template", type_="check"
    )
    op.drop_index("ix_document_template_case_id", "document_template")
    for column in ("submitted_at", "person_id", "case_step_progress_id", "case_id"):
        op.drop_column("document_template", column)
