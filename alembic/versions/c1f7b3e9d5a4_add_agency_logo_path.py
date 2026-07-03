"""add agency.logo_path (agency branding)

Private-bucket path of the agency logo, served by the backend
(authenticated + one assumed public exception by slug for the client
login page). NULL = no logo. The agency table already carries RLS from
the a103249eb0a1 sweep — a new column changes nothing there. Additive,
cleanly reversible.

Revision ID: c1f7b3e9d5a4
Revises: b9d5f1a3c7e2
Create Date: 2026-07-03 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1f7b3e9d5a4"
down_revision: Union[str, Sequence[str], None] = "b9d5f1a3c7e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agency", sa.Column("logo_path", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("agency", "logo_path")
