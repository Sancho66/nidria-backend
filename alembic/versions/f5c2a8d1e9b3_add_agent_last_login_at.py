"""add_agent_last_login_at

Revision ID: f5c2a8d1e9b3
Revises: e4b1a2c6d8f0
Create Date: 2026-07-09

Additive: agent.last_login_at, posed at LOGIN token issuance (never a refresh,
never impersonation). The adoption dashboard reads MAX(last_login_at) per
agency. Reversible: drop the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5c2a8d1e9b3"
down_revision: str | Sequence[str] | None = "e4b1a2c6d8f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("agent", "last_login_at")
