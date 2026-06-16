"""add journey_section + nullable section_id on field tables (sections, vague A)

Purely additive: a new journey_section table + a NULLABLE section_id on
both creation-field tables (SET NULL on section delete). Existing fields
become section_id IS NULL = the default "unsectioned" bucket — no
backfill, no data migration, the product keeps working flat.

Revision ID: d1a4f7c2e9b6
Revises: c9f1a3e5d7b2
Create Date: 2026-06-16 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d1a4f7c2e9b6"
down_revision: Union[str, Sequence[str], None] = "c9f1a3e5d7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "journey_section",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["template_id"], ["journey_template.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_journey_section_template_id"), "journey_section", ["template_id"], unique=False
    )

    # NULLABLE section_id on both field tables — existing rows land NULL.
    for table in ("journey_template_field", "journey_template_case_field"):
        op.add_column(
            table, sa.Column("section_id", postgresql.UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            f"fk_{table}_section_id",
            table,
            "journey_section",
            ["section_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            op.f(f"ix_{table}_section_id"), table, ["section_id"], unique=False
        )


def downgrade() -> None:
    for table in ("journey_template_field", "journey_template_case_field"):
        op.drop_index(op.f(f"ix_{table}_section_id"), table_name=table)
        op.drop_constraint(f"fk_{table}_section_id", table, type_="foreignkey")
        op.drop_column(table, "section_id")
    op.drop_index(op.f("ix_journey_section_template_id"), table_name="journey_section")
    op.drop_table("journey_section")
