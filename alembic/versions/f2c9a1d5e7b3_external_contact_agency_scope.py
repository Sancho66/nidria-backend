"""external_contact agency scope + designation + reminder 'agent' recipient
(owner escalation).

Additive, cleanly reversible. `agency_id` backfilled from the case (every
existing contact has a case → its agency is known, no data loss). `case_id`
becomes nullable (agency-directory contacts have none). `agent_id`
DESIGNATES a login account later (the contact is never transformed).
reminder allows recipient_type='agent' for the owner escalation.

(No template external anchor: externals are named at the CASE level via
set_responsible → responsible_external_id — the template default responsible
is internal-only by design.)

Revision ID: f2c9a1d5e7b3
Revises: a1d3f5b7c9e2
Create Date: 2026-07-09 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f2c9a1d5e7b3"
down_revision: Union[str, Sequence[str], None] = "a1d3f5b7c9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK_NEW = (
    "(recipient_type = 'expat' AND recipient_external_id IS NULL)"
    " OR (recipient_type = 'external' AND recipient_external_id IS NOT NULL)"
    " OR (recipient_type = 'agent' AND recipient_external_id IS NULL)"
)
_CHECK_OLD = (
    "(recipient_type = 'expat' AND recipient_external_id IS NULL)"
    " OR (recipient_type = 'external' AND recipient_external_id IS NOT NULL)"
)
_PARTICIPANT_CHECK_NEW = (
    "(type = 'agent' AND external_id IS NULL)"
    " OR (type = 'expat' AND agent_id IS NULL AND external_id IS NULL)"
    " OR (type = 'external' AND external_id IS NOT NULL AND agent_id IS NULL)"
)
_PARTICIPANT_CHECK_OLD = "(type = 'expat' AND agent_id IS NULL) OR (type = 'agent')"


def upgrade() -> None:
    # --- external_contact: agency scope (backfill from the case) ---------------
    op.add_column(
        "external_contact", sa.Column("agency_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.execute(
        "UPDATE external_contact ec SET agency_id = cc.agency_id "
        "FROM client_case cc WHERE cc.id = ec.case_id"
    )
    op.alter_column("external_contact", "agency_id", nullable=False)
    op.create_foreign_key(
        "fk_external_contact_agency",
        "external_contact",
        "agency",
        ["agency_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_external_contact_agency_id", "external_contact", ["agency_id"])
    # case_id becomes nullable — agency-directory contacts carry none.
    op.alter_column(
        "external_contact",
        "case_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    # --- external_contact: designated login account ----------------------------
    op.add_column(
        "external_contact", sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_external_contact_agent",
        "external_contact",
        "agent",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_external_contact_agent_id", "external_contact", ["agent_id"])
    # One directory entry per (agency, lower(name)) — case_id NULL only.
    op.create_index(
        "uq_external_contact_directory_name",
        "external_contact",
        ["agency_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("case_id IS NULL"),
    )
    # --- journey_step_participant: external participant (named provider) --------
    op.add_column(
        "journey_step_participant",
        sa.Column("external_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_journey_step_participant_external",
        "journey_step_participant",
        "external_contact",
        ["external_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(
        "participant_template_type_matches_fk", "journey_step_participant", type_="check"
    )
    op.create_check_constraint(
        "participant_template_type_matches_fk", "journey_step_participant", _PARTICIPANT_CHECK_NEW
    )
    # --- unification: 1 directory external_contact per existing is_external Agent
    # (agent_id set, name from profile). Additive, no repointing of existing
    # assignments. Idempotent via NOT EXISTS. 0 rows on a fresh DB.
    op.execute(
        "INSERT INTO external_contact "
        "(id, agency_id, case_id, agent_id, name, type, created_at, updated_at) "
        "SELECT gen_random_uuid(), a.agency_id, NULL, a.id, "
        "trim(a.first_name || ' ' || a.last_name), 'other', now(), now() "
        "FROM agent a WHERE a.is_external = true "
        "AND NOT EXISTS (SELECT 1 FROM external_contact ec WHERE ec.agent_id = a.id)"
    )
    # --- reminder: allow recipient_type='agent' (owner escalation) -------------
    op.drop_constraint("recipient_type_matches_fk", "reminder", type_="check")
    op.create_check_constraint("recipient_type_matches_fk", "reminder", _CHECK_NEW)


def downgrade() -> None:
    op.drop_constraint("recipient_type_matches_fk", "reminder", type_="check")
    op.create_check_constraint("recipient_type_matches_fk", "reminder", _CHECK_OLD)
    # journey_step_participant external participant → back to {expat, agent}.
    op.drop_constraint(
        "participant_template_type_matches_fk", "journey_step_participant", type_="check"
    )
    op.create_check_constraint(
        "participant_template_type_matches_fk", "journey_step_participant", _PARTICIPANT_CHECK_OLD
    )
    op.drop_constraint(
        "fk_journey_step_participant_external", "journey_step_participant", type_="foreignkey"
    )
    op.drop_column("journey_step_participant", "external_id")
    op.drop_index("uq_external_contact_directory_name", table_name="external_contact")
    op.drop_constraint("fk_external_contact_agent", "external_contact", type_="foreignkey")
    op.drop_index("ix_external_contact_agent_id", table_name="external_contact")
    op.drop_column("external_contact", "agent_id")
    # Restore case_id NOT NULL (safe on a fresh DB; prod downgrade drops the
    # feature and would require no directory rows to exist).
    op.alter_column(
        "external_contact",
        "case_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_constraint("fk_external_contact_agency", "external_contact", type_="foreignkey")
    op.drop_index("ix_external_contact_agency_id", table_name="external_contact")
    op.drop_column("external_contact", "agency_id")
