"""add journey_template.canvas_layout (visual canvas editor, MVP-1)

Pure-presentation node positions for the visual journey editor —
{ "<step_id>": {"x", "y"} }. NULL = never opened in canvas. Additive,
nullable, never touches journey logic.

Revision ID: f3c7a9e1b5d8
Revises: e2b5c8d4f1a7
Create Date: 2026-06-16 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f3c7a9e1b5d8"
down_revision: Union[str, Sequence[str], None] = "e2b5c8d4f1a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "journey_template",
        sa.Column("canvas_layout", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("journey_template", "canvas_layout")
