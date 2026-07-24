"""platform task events (watcher digest queue)

A queue of notifiable changes (status change | new comment) on a platform
task, consumed by the 20-minute watcher-digest job. `notified_at` is the
idempotence marker. Additive, no backfill: existing tasks simply have no
queued events.

Revision ID: a1c7e9f2b4d8
Revises: f7b3d1a9c4e6
"""

import sqlalchemy as sa
from alembic import op

revision = "a1c7e9f2b4d8"
down_revision = "f7b3d1a9c4e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_task_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("actor_agent_id", sa.Uuid(), nullable=True),
        sa.Column("actor_name", sa.String(length=255), nullable=True),
        sa.Column("old_status", sa.String(length=20), nullable=True),
        sa.Column("new_status", sa.String(length=20), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["platform_task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_agent_id"], ["agent.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Deny-all RLS posture, like every other table.
    op.execute("ALTER TABLE platform_task_event ENABLE ROW LEVEL SECURITY")
    op.create_index(
        "ix_platform_task_event_task_id", "platform_task_event", ["task_id"], unique=False
    )
    op.create_index(
        "ix_platform_task_event_created_at", "platform_task_event", ["created_at"], unique=False
    )
    op.create_index(
        "ix_platform_task_event_notified_at", "platform_task_event", ["notified_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_platform_task_event_notified_at", table_name="platform_task_event")
    op.drop_index("ix_platform_task_event_created_at", table_name="platform_task_event")
    op.drop_index("ix_platform_task_event_task_id", table_name="platform_task_event")
    op.drop_table("platform_task_event")
