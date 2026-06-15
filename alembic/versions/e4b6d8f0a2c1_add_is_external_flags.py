"""add is_external flags on agent + role (external provider users)

External providers (lawyer, notary, …) are agents wearing an external
system role. `role.is_external` classifies the role; `agent.is_external`
is the denormalized filter read by enforce() and every "agents of the
agency" listing. Both additive, default false (backfill = false: every
existing agent/role stays internal).

Revision ID: e4b6d8f0a2c1
Revises: d3f5a7c9e1b2
Create Date: 2026-06-15 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b6d8f0a2c1"
down_revision: Union[str, Sequence[str], None] = "d3f5a7c9e1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent",
        sa.Column(
            "is_external", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "role",
        sa.Column(
            "is_external", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("role", "is_external")
    op.drop_column("agent", "is_external")
