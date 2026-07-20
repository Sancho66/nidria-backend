"""Widen agency.default_language CHECK to 7 languages (+ hu)

Hungarian enters the product (decision Eric, 2026-07-20). Same additive
pattern as c4e1a7b9d2f8 (3 to 6): drop + recreate the CHECK with the
widened set. No data migration.

Revision ID: d8f4a0c6e2b9
Revises: c7f3b9e5a1d6
Create Date: 2026-07-20 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8f4a0c6e2b9"
down_revision: str | Sequence[str] | None = "c7f3b9e5a1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "agency_default_language_check"
_OLD = "default_language IN ('fr', 'en', 'es', 'ru', 'pt', 'it')"
_NEW = "default_language IN ('fr', 'en', 'es', 'ru', 'pt', 'it', 'hu')"


def upgrade() -> None:
    op.drop_constraint(_NAME, "agency", type_="check")
    op.create_check_constraint(_NAME, "agency", _NEW)


def downgrade() -> None:
    op.drop_constraint(_NAME, "agency", type_="check")
    op.create_check_constraint(_NAME, "agency", _OLD)
