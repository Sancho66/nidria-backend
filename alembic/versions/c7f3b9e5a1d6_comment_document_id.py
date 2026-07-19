"""step_comment.document_id (piece jointe au fil de discussion)

Additive: a comment may reference ONE case document (the attachment IS a
regular document — GAP-B rules, one truth, two displays: the thread and
the step's documents panel). SET NULL: deleting the document mutes the
reference, deleting the comment never kills the document.

Revision ID: c7f3b9e5a1d6
Revises: b5d1f7c3e9a6
Create Date: 2026-07-19 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7f3b9e5a1d6"
down_revision: str | Sequence[str] | None = "b5d1f7c3e9a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("step_comment", sa.Column("document_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_step_comment_document_id",
        "step_comment",
        "document",
        ["document_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_step_comment_document_id", "step_comment", type_="foreignkey")
    op.drop_column("step_comment", "document_id")
