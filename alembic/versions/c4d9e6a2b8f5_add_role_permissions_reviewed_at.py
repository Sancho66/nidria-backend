"""add_role_permissions_reviewed_at

Revision ID: c4d9e6a2b8f5
Revises: b3c8d5f1a2e4
Create Date: 2026-07-10

The clone's exact decision trace: `role.permissions_reviewed_at` = the agency's
last MATRIX decision on the role. Set at creation, bumped only by the matrix
PUT (set_role_permissions) — a rename never touches it. The seed's clone
catch-up fills a permission iff it was born AFTER this moment.

Backfill = created_at: every existing role is treated as last-reviewed at its
creation (the clone copied the full system matrix then — the honest floor; a
matrix edit made before this column existed left no trace, so its post-birth
omissions read as ignorance ONCE, then any removal is stamped and final).
Additive, reversible: drop the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4d9e6a2b8f5"
down_revision: str | Sequence[str] | None = "b3c8d5f1a2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "role",
        sa.Column(
            "permissions_reviewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
    )
    # Deterministic backfill: the last known decision of every existing role
    # is its creation (overwrites the transient now() the default just wrote).
    op.execute("UPDATE role SET permissions_reviewed_at = created_at")
    op.alter_column(
        "role",
        "permissions_reviewed_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("role", "permissions_reviewed_at")
