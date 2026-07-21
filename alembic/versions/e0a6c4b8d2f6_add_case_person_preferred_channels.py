"""add case_person.preferred_channels (display-only contact channels)

Additive. NOT NULL JSONB default '[]'. Existing persons get an empty
list. DISPLAY/preference only — the reminder dispatch stays email-only,
and phone/whatsapp reuse the existing `phone` column.

Revision ID: e0a6c4b8d2f6
Revises: c8f4a2e6b0d4
Create Date: 2026-07-21 14:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e0a6c4b8d2f6"
down_revision: Union[str, Sequence[str], None] = "c8f4a2e6b0d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "case_person",
        sa.Column(
            "preferred_channels",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("case_person", "preferred_channels")
