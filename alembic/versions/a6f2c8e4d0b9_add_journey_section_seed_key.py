"""add journey_section.seed_key (samples phase B)

Stable anchor of a seeded section on a library sample (one of the 11
section-type keys); NULL on agency-made sections. Unique per template so
the seed reconciliation can never duplicate a section. Additive,
reversible.

Revision ID: a6f2c8e4d0b9
Revises: f5d2b8e4c1a6
Create Date: 2026-07-05 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a6f2c8e4d0b9"
down_revision: Union[str, Sequence[str], None] = "f5d2b8e4c1a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("journey_section", sa.Column("seed_key", sa.String(length=60), nullable=True))
    op.create_unique_constraint(
        "uq_section_seed_key", "journey_section", ["template_id", "seed_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_section_seed_key", "journey_section", type_="unique")
    op.drop_column("journey_section", "seed_key")
