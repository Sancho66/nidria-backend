"""add step validator — "Action validée par" (refonte completion_mode)

Adds the validator structure, symmetric to the responsible: a TYPE on the
template (journey_template_step) + the precise person on the instance
(case_step_progress). Supersedes completion_mode, which is KEPT for the
whole transition (rollback-safe fallback) and dropped in a LATER wave.

Migration plan (additive + reversible, zero loss):
  1. add columns NULLABLE first;
  2. set-based, idempotent, deterministic backfill FROM completion_mode
     (auto → none, agency_validation → agent; agent_id left NULL =
     "the agency in general" / "no one" — no historical designation);
  3. SET NOT NULL + server_default 'agent' (parity with the former
     'agency_validation' default for new steps);
  4. add the validator CHECK (after backfill, so existing rows pass).
completion_mode is never touched → downgrade restores 100%.

Runs at boot (start.sh) on prod data: the backfill covers EVERY existing
journey_template_step and case_step_progress (no orphan left without a
validated_by_type), and is re-runnable without harm (plain UPDATE of a
column derived purely from completion_mode).

Revision ID: b2e9d6a4c8f1
Revises: a1d4f8c2e6b9
Create Date: 2026-06-18 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b2e9d6a4c8f1"
down_revision: Union[str, Sequence[str], None] = "a1d4f8c2e6b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VALIDATED_CHECK = (
    "(validated_by_type IN ('none', 'expat') AND validated_by_agent_id IS NULL)"
    " OR (validated_by_type = 'agent')"
    " OR (validated_by_type = 'external' AND validated_by_agent_id IS NOT NULL)"
)


def upgrade() -> None:
    # 1. Columns, NULLABLE first (so the backfill can run before NOT NULL).
    op.add_column(
        "journey_template_step",
        sa.Column("default_validated_by_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "journey_template_step",
        sa.Column("default_validated_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_journey_template_step_default_validated_by_agent_id",
        "journey_template_step",
        "agent",
        ["default_validated_by_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "case_step_progress",
        sa.Column("validated_by_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "case_step_progress",
        sa.Column("validated_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_case_step_progress_validated_by_agent_id",
        "case_step_progress",
        "agent",
        ["validated_by_agent_id"],
        ["id"],
    )

    # 2. Deterministic backfill FROM completion_mode. Covers EVERY row;
    #    agent_id stays NULL (no historical designation).
    op.execute(
        "UPDATE journey_template_step"
        " SET default_validated_by_type ="
        " CASE completion_mode"
        " WHEN 'auto' THEN 'none'"
        " WHEN 'agency_validation' THEN 'agent'"
        " ELSE 'agent' END"
    )
    op.execute(
        "UPDATE case_step_progress p"
        " SET validated_by_type ="
        " CASE (SELECT s.completion_mode FROM journey_template_step s"
        "       WHERE s.id = p.template_step_id)"
        " WHEN 'auto' THEN 'none'"
        " WHEN 'agency_validation' THEN 'agent'"
        " ELSE 'agent' END"
    )

    # 3. NOT NULL + server_default 'agent' (new steps/instances default to
    #    "the agency validates" = the former 'agency_validation' default).
    op.alter_column(
        "journey_template_step",
        "default_validated_by_type",
        existing_type=sa.String(length=20),
        nullable=False,
        server_default=sa.text("'agent'"),
    )
    op.alter_column(
        "case_step_progress",
        "validated_by_type",
        existing_type=sa.String(length=20),
        nullable=False,
        server_default=sa.text("'agent'"),
    )

    # 4. Validator CHECK (after backfill — looser than the responsible one).
    op.create_check_constraint(
        "validated_by_type_matches_fk", "case_step_progress", _VALIDATED_CHECK
    )


def downgrade() -> None:
    op.drop_constraint("validated_by_type_matches_fk", "case_step_progress", type_="check")
    op.drop_constraint(
        "fk_case_step_progress_validated_by_agent_id", "case_step_progress", type_="foreignkey"
    )
    op.drop_column("case_step_progress", "validated_by_agent_id")
    op.drop_column("case_step_progress", "validated_by_type")
    op.drop_constraint(
        "fk_journey_template_step_default_validated_by_agent_id",
        "journey_template_step",
        type_="foreignkey",
    )
    op.drop_column("journey_template_step", "default_validated_by_agent_id")
    op.drop_column("journey_template_step", "default_validated_by_type")
    # completion_mode untouched → full restore.
