"""add agent.deactivated_at (offboarding, never a DELETE)

The identity of a departed agent lives in the audit trail (activity log,
step completions, reminder approvals): deletion would rewrite history.
Deactivation cuts login + live tokens, drops the member out of every
seat/provider count, and pushes the Paddle quantity down. NULL = active.

Revision ID: a8c4e0f6b2d9
Revises: f7a3b9d5c1e8
Create Date: 2026-07-17 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8c4e0f6b2d9"
down_revision: str | Sequence[str] | None = "f7a3b9d5c1e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent", sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("agent", "deactivated_at")
