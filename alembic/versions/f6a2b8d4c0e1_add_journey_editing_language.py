"""add journey_template.editing_language (point 6c)

An EDITING convenience only: pre-selects the language in the front's
step/section/field editors of THIS template. Read by no backend
resolution path (client resolution stays client language → agency
default → fr; notifications untouched). Nullable + CHECK on the
supported set, mirroring agency.default_language. Purely additive,
cleanly reversible.

Revision ID: f6a2b8d4c0e1
Revises: e5f1a7c3b9d2
Create Date: 2026-07-03 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a2b8d4c0e1"
down_revision: Union[str, Sequence[str], None] = "e5f1a7c3b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "journey_template", sa.Column("editing_language", sa.String(length=5), nullable=True)
    )
    op.create_check_constraint(
        "journey_template_editing_language_check",
        "journey_template",
        "editing_language IN ('fr', 'en', 'es', 'ru', 'pt', 'it')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "journey_template_editing_language_check", "journey_template", type_="check"
    )
    op.drop_column("journey_template", "editing_language")
