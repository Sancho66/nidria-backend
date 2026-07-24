"""platform task comments (+ comment_id on attachments)

The "Commentaires" tab of the superadmin task editor: a free-form thread
per platform task, with attachments reusing the existing
platform_task_attachment upload path.

- platform_task_comment: new table, CASCADE on the parent task, author
  agent SET NULL (a departed operator never orphans a row).
- platform_task_attachment.comment_id: additive nullable FK. NULL = the
  existing task-level "Fichiers" attachment (every existing row); set = a
  file attached to a comment. CASCADE so deleting a comment drops its
  attachment rows (blobs wiped by the manager first).

Revision ID: f7b3d1a9c4e6
Revises: e4a2c6d8f1b3
"""

import sqlalchemy as sa
from alembic import op

revision = "f7b3d1a9c4e6"
down_revision = "e4a2c6d8f1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_task_comment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("author_agent_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["platform_task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_agent_id"], ["agent.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Deny-all RLS posture (Supabase PostgREST exposes naked tables): every
    # new table enables RLS with no policy, like every other table.
    op.execute("ALTER TABLE platform_task_comment ENABLE ROW LEVEL SECURITY")
    op.create_index(
        "ix_platform_task_comment_task_id", "platform_task_comment", ["task_id"], unique=False
    )

    op.add_column(
        "platform_task_attachment", sa.Column("comment_id", sa.Uuid(), nullable=True)
    )
    op.create_index(
        "ix_platform_task_attachment_comment_id",
        "platform_task_attachment",
        ["comment_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_platform_task_attachment_comment_id",
        "platform_task_attachment",
        "platform_task_comment",
        ["comment_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_platform_task_attachment_comment_id", "platform_task_attachment", type_="foreignkey"
    )
    op.drop_index("ix_platform_task_attachment_comment_id", table_name="platform_task_attachment")
    op.drop_column("platform_task_attachment", "comment_id")

    op.drop_index("ix_platform_task_comment_task_id", table_name="platform_task_comment")
    op.drop_table("platform_task_comment")
