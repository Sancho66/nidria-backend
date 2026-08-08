"""Seat-type rules (lot lecteur 08/08, verrou rôle 08/08) shared across domains.

A READER seat is a read-only presence, and THE SEAT CAPS THE ROLE:

- a reader wears exactly ONE role — the system `viewer` — and nothing
  else: not a custom role, not a clone, whatever its permission set
  (v1 doctrine, arbitrage 08/08: « seul viewer, la règle est simple et
  incontestable » — a custom read-only role waits for a real demand).
  The refusal lives at the SINGLE role-assignment door
  (RolesManager.set_member_role) and at the reader invitation; the
  seat-type flip does both gestures at once (manager→reader FORCES
  viewer, reader→manager frees the role choice);
- a reader is NEVER a designated actor — case owner, step responsible or
  validator, journey-template default or participant: every designation
  point calls `assert_not_reader_actor` on the TARGET row.

Both rules reason by seat_type, never by trusting a role NAME alone —
`assert_reader_role_locked` matches the one true system row (is_system,
agency_id NULL, name `viewer`), which agency clones can never satisfy.
"""

from shared.models.agent import Agent
from shared.models.rbac import Role
from src.core.enums import SeatType
from src.core.exceptions import ValidationError

# The ONE role a reader seat may wear: the shared system viewer.
READER_ROLE_NAME = "viewer"


def is_the_system_viewer(role: Role) -> bool:
    return bool(role.is_system) and role.agency_id is None and role.name == READER_ROLE_NAME


def assert_reader_role_locked(role: Role) -> None:
    """422 unless the role is THE system viewer — the seat caps the role
    (contournement fermé 08/08): direct assignment, custom role or clone,
    every elevation path answers the same named refusal."""
    if not is_the_system_viewer(role):
        raise ValidationError(
            "This member occupies a reader seat: switch them to a manager "
            "seat to change their role.",
            code="seat.reader_role_locked",
        )


def assert_not_reader_actor(target: Agent, *, designation: str) -> None:
    """Refuse a READER as a designated actor. `designation` names the
    gesture in the error params (owner, responsible, validator,
    template_default, participant)."""
    if target.seat_type == SeatType.READER.value:
        raise ValidationError(
            "A reader seat cannot be designated as an actor.",
            code="seat.reader_cannot_be_actor",
            params={"designation": designation, "agent_id": str(target.id)},
        )
