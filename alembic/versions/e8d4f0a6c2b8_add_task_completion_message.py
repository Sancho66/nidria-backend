"""add platform_task.completion_message (client-facing note on done)

Additive, nullable. Reopen never clears it (provided content lives
forever); the done email carries it verbatim.

Revision ID: e8d4f0a6c2b8
Revises: d0f6b2c8a4e2
Create Date: 2026-07-20 23:30:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8d4f0a6c2b8"
down_revision: Union[str, Sequence[str], None] = "d0f6b2c8a4e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("platform_task", sa.Column("completion_message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("platform_task", "completion_message")
