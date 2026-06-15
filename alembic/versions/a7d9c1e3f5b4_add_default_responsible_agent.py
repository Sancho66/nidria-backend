"""add default_responsible_agent_id on journey_template_step (wave C)

Optional named default responsible (a precise internal agent) on a
template step. Additive, nullable.

Revision ID: a7d9c1e3f5b4
Revises: f5c7e9a1b3d2
Create Date: 2026-06-15 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a7d9c1e3f5b4"
down_revision: Union[str, Sequence[str], None] = "f5c7e9a1b3d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "journey_template_step",
        sa.Column("default_responsible_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_template_step_default_responsible_agent",
        "journey_template_step",
        "agent",
        ["default_responsible_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_template_step_default_responsible_agent",
        "journey_template_step",
        type_="foreignkey",
    )
    op.drop_column("journey_template_step", "default_responsible_agent_id")
