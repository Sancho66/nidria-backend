"""notification_window replaces step_comment_notification (anti-burst demi-lot)

The window becomes per (case, recipient email, category) instead of per
(step, recipient side): two steps touched minutes apart cost ONE email,
two recipients never share a window. Categories: "comments" and "steps".
The old per-step tracker is dropped (its rows are 15-minute ephemera,
nothing worth migrating). RLS enabled (deny-all posture).

Revision ID: d1f7b3e9a5c2
Revises: c0e6a2d8f4b1
Create Date: 2026-07-18 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f7b3e9a5c2"
down_revision: str | Sequence[str] | None = "c0e6a2d8f4b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_window",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["client_case.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "case_id", "recipient_email", "category", name="uq_notification_window"
        ),
    )
    op.create_index(
        op.f("ix_notification_window_case_id"), "notification_window", ["case_id"], unique=False
    )
    op.execute("ALTER TABLE notification_window ENABLE ROW LEVEL SECURITY")
    op.drop_table("step_comment_notification")


def downgrade() -> None:
    op.create_table(
        "step_comment_notification",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_step_progress_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_type", sa.String(length=20), nullable=False),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_step_progress_id"], ["case_step_progress.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "case_step_progress_id", "recipient_type", name="uq_step_comment_notification"
        ),
    )
    op.execute("ALTER TABLE step_comment_notification ENABLE ROW LEVEL SECURITY")
    op.drop_table("notification_window")
