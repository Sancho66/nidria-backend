"""journey_template sample — agency_id nullable + is_sample (library pattern)

Lets a journey template live as a shared LIBRARY sample (agency_id IS NULL +
is_sample=true), mirroring a system `role` (agency_id NULL + is_system). A
sample is READ-ONLY for agencies by construction: every write path filters
WHERE agency_id == me, and `NULL = <agency>` is never true → unreachable.

Additive: agency_id becomes nullable + a new is_sample column (NOT NULL,
default false → all existing rows are owned templates, unchanged). No data
written here (samples are seeded in a later block).

⚠️ DOWNGRADE: re-imposes NOT NULL on agency_id — this FAILS if any sample
(agency_id IS NULL) exists. Remove every sample row before downgrading.

Revision ID: d4a7b2c9f6e1
Revises: c3f1a8d5e7b2
Create Date: 2026-06-18 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a7b2c9f6e1"
down_revision: Union[str, Sequence[str], None] = "c3f1a8d5e7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "journey_template",
        sa.Column(
            "is_sample",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.alter_column(
        "journey_template",
        "agency_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    # Re-impose NOT NULL — requires that no sample (agency_id IS NULL) remains.
    op.alter_column(
        "journey_template",
        "agency_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_column("journey_template", "is_sample")
