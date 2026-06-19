"""participant "agency in general" — relax the agent CHECK to allow agent_id NULL

A participant of type='agent' with agent_id NULL now means "the agency in
general" (no named member), symmetric to validated_by_type='agent' + agent_id
NULL. Relaxes the two participant CHECK constraints (template + instance).
Additive: every existing row already satisfies the looser predicate, so no
data migration. Fully reversible (downgrade re-tightens — safe only while no
agent-without-agent_id row exists).

Revision ID: f3b8d1e4a9c2
Revises: e6c2f9a1b3d7
Create Date: 2026-06-19 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3b8d1e4a9c2"
down_revision: str | Sequence[str] | None = "e6c2f9a1b3d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Template participants: agent_id becomes optional for type='agent'.
    op.drop_constraint(
        "participant_template_type_matches_fk", "journey_step_participant", type_="check"
    )
    op.create_check_constraint(
        "participant_template_type_matches_fk",
        "journey_step_participant",
        "(type = 'expat' AND agent_id IS NULL) OR (type = 'agent')",
    )
    # Instance participants: same relaxation on the agent branch.
    op.drop_constraint(
        "participant_instance_type_matches_fk", "case_step_participant", type_="check"
    )
    op.create_check_constraint(
        "participant_instance_type_matches_fk",
        "case_step_participant",
        "(type = 'agent' AND external_id IS NULL)"
        " OR (type = 'expat' AND agent_id IS NULL AND external_id IS NULL)"
        " OR (type = 'external' AND external_id IS NOT NULL AND agent_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "participant_instance_type_matches_fk", "case_step_participant", type_="check"
    )
    op.create_check_constraint(
        "participant_instance_type_matches_fk",
        "case_step_participant",
        "(type = 'agent' AND agent_id IS NOT NULL AND external_id IS NULL)"
        " OR (type = 'expat' AND agent_id IS NULL AND external_id IS NULL)"
        " OR (type = 'external' AND external_id IS NOT NULL AND agent_id IS NULL)",
    )
    op.drop_constraint(
        "participant_template_type_matches_fk", "journey_step_participant", type_="check"
    )
    op.create_check_constraint(
        "participant_template_type_matches_fk",
        "journey_step_participant",
        "(type = 'expat' AND agent_id IS NULL) OR (type = 'agent' AND agent_id IS NOT NULL)",
    )
