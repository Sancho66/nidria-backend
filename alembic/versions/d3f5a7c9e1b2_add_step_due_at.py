"""add due_at (firm deadline) on case_step_progress

Optional hard deadline set by the agency on a dossier step. When present
it takes priority over the estimated_days-derived target for the
days-remaining counter. Additive, nullable.

Revision ID: d3f5a7c9e1b2
Revises: c2d4e6f8a1b3
Create Date: 2026-06-15 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3f5a7c9e1b2"
down_revision: Union[str, Sequence[str], None] = "c2d4e6f8a1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "case_step_progress",
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("case_step_progress", "due_at")
