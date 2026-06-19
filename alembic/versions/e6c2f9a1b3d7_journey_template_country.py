"""journey_template country — ISO 3166-1 alpha-2 for sample grouping/flag/search

Additive: one NULLABLE column. Existing agency templates stay valid (NULL);
a sample carries a code (e.g. "PY"). No country table — the flag + localized
name are a front concern. Fully reversible (drop column).

Revision ID: e6c2f9a1b3d7
Revises: d4a7b2c9f6e1
Create Date: 2026-06-19 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6c2f9a1b3d7"
down_revision: Union[str, Sequence[str], None] = "d4a7b2c9f6e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("journey_template", sa.Column("country", sa.String(length=2), nullable=True))


def downgrade() -> None:
    op.drop_column("journey_template", "country")
