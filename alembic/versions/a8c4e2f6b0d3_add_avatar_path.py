"""add agent.avatar_path + expat_user.avatar_path (user settings bloc 1)

Private-bucket storage path of the profile picture, served by the backend
only. NULL = no avatar (initials fallback). Purely additive, cleanly
reversible.

Revision ID: a8c4e2f6b0d3
Revises: f6a2b8d4c0e1
Create Date: 2026-07-03 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8c4e2f6b0d3"
down_revision: Union[str, Sequence[str], None] = "f6a2b8d4c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent", sa.Column("avatar_path", sa.String(length=500), nullable=True))
    op.add_column("expat_user", sa.Column("avatar_path", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("expat_user", "avatar_path")
    op.drop_column("agent", "avatar_path")
