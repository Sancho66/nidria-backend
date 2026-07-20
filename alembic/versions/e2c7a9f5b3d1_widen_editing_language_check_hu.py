"""Widen journey_template.editing_language CHECK to 7 languages (+ hu)

The hu lot (d8f4a0c6e2b9) widened agency.default_language but missed
this second language CHECK (posed by f6a2b8d4c0e1): the API accepts
editing_language=hu (derived validation) and the DB then rejects it.
Same additive pattern: drop + recreate. No data migration.

Revision ID: e2c7a9f5b3d1
Revises: d8f4a0c6e2b9
Create Date: 2026-07-20 16:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2c7a9f5b3d1"
down_revision: str | Sequence[str] | None = "d8f4a0c6e2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "journey_template_editing_language_check"
_TABLE = "journey_template"
_OLD = "editing_language IN ('fr', 'en', 'es', 'ru', 'pt', 'it')"
_NEW = "editing_language IN ('fr', 'en', 'es', 'ru', 'pt', 'it', 'hu')"


def upgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _NEW)


def downgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _OLD)
