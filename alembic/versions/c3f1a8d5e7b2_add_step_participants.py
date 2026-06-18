"""add step participants — "Action à réaliser par" 1 → N (responsible refonte)

Additive ONLY (zero data touched): a TEMPLATE table (journey_step_participant)
and an INSTANCE table (case_step_participant), each with a polymorphic-person
CHECK calqué sur le responsable. Snapshot model: the instance rows are
copied from the template at journey assignment (apply_journey / backfill_step).

Does NOT touch the validator (validated_by_*) nor the gating, nor the existing
responsible columns. No backfill here — the existing responsible → participant
conversion is a separate, reviewed step.

Revision ID: c3f1a8d5e7b2
Revises: b2e9d6a4c8f1
Create Date: 2026-06-18 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3f1a8d5e7b2"
down_revision: Union[str, Sequence[str], None] = "b2e9d6a4c8f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # TEMPLATE participants — person ∈ {expat, agent}; an external_contact is
    # case-scoped and cannot exist at the template (same limit as
    # default_responsible_*).
    op.create_table(
        "journey_step_participant",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["step_id"], ["journey_template_step.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(type = 'expat' AND agent_id IS NULL)"
            " OR (type = 'agent' AND agent_id IS NOT NULL)",
            name="participant_template_type_matches_fk",
        ),
    )
    op.create_index(
        op.f("ix_journey_step_participant_step_id"),
        "journey_step_participant",
        ["step_id"],
        unique=False,
    )

    # INSTANCE participants — full 3-way polymorphism calqué sur le
    # responsable d'instance (agent / expat / external_contact). The two
    # person FKs are NO ACTION (same rationale as case_step_progress).
    op.create_table(
        "case_step_participant",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_step_progress_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["case_step_progress_id"], ["case_step_progress.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
        sa.ForeignKeyConstraint(["external_id"], ["external_contact.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(type = 'agent' AND agent_id IS NOT NULL AND external_id IS NULL)"
            " OR (type = 'expat' AND agent_id IS NULL AND external_id IS NULL)"
            " OR (type = 'external' AND external_id IS NOT NULL AND agent_id IS NULL)",
            name="participant_instance_type_matches_fk",
        ),
    )
    op.create_index(
        op.f("ix_case_step_participant_case_step_progress_id"),
        "case_step_participant",
        ["case_step_progress_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_case_step_participant_case_step_progress_id"),
        table_name="case_step_participant",
    )
    op.drop_table("case_step_participant")
    op.drop_index(
        op.f("ix_journey_step_participant_step_id"), table_name="journey_step_participant"
    )
    op.drop_table("journey_step_participant")
