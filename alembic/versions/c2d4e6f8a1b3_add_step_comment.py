"""add step_comment + step_comment_notification (per-step thread, vague 5)

Revision ID: c2d4e6f8a1b3
Revises: b7f3c1a9d2e4
Create Date: 2026-06-13 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c2d4e6f8a1b3"
down_revision: Union[str, Sequence[str], None] = "b7f3c1a9d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "step_comment",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_step_progress_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_type", sa.String(length=20), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["case_step_progress_id"], ["case_step_progress.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_step_comment_case_step_progress_id"),
        "step_comment",
        ["case_step_progress_id"],
        unique=False,
    )
    op.create_table(
        "step_comment_notification",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_step_progress_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_type", sa.String(length=20), nullable=False),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_step_progress_id"], ["case_step_progress.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "case_step_progress_id", "recipient_type", name="uq_step_comment_notification"
        ),
    )
    op.create_index(
        op.f("ix_step_comment_notification_case_step_progress_id"),
        "step_comment_notification",
        ["case_step_progress_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_step_comment_notification_case_step_progress_id"),
        table_name="step_comment_notification",
    )
    op.drop_table("step_comment_notification")
    op.drop_index(op.f("ix_step_comment_case_step_progress_id"), table_name="step_comment")
    op.drop_table("step_comment")
