"""Seat-type rules (lot lecteur 08/08) shared across domains.

A READER seat is a read-only presence. The RBAC matrix already refuses it
every write (its role stays read-only-capable); this module carries the
two STRUCTURAL rules no matrix can express:

- a reader is NEVER a designated actor — case owner, step responsible or
  validator, journey-template default or participant: every designation
  point calls `assert_not_reader_actor` on the TARGET row;
- a reader's role must be READ-ONLY-CAPABLE: its permission set stays
  within the keys that gate no write binding (the personal `/views`
  writes under case.view are the assumed exception — arbitrage 07/08).

Both rules reason by seat_type and CAPABILITY, never by role name — role
names are agency-editable and system roles have copy-on-write clones.
"""

from shared.models.agent import Agent
from src.core.enums import SeatType
from src.core.exceptions import ValidationError
from src.core.rbac.permissions import Permission

# The only permission keys that gate NO write binding (audit 07/08):
# note.view_confidential and cost.view are manager-level READ filters;
# case.view gates reads plus the assumed personal /views writes.
READ_ONLY_PERMISSION_KEYS: frozenset[str] = frozenset(
    {
        Permission.CASE_VIEW.value,
        Permission.NOTE_VIEW_CONFIDENTIAL.value,
        Permission.COST_VIEW.value,
    }
)


def role_is_read_only(permission_keys: set[str]) -> bool:
    """True when the role could never reach a write endpoint."""
    return permission_keys <= READ_ONLY_PERMISSION_KEYS


def assert_reader_role_read_only(permission_keys: set[str]) -> None:
    """422 when a role about to be worn by a READER can write."""
    if not role_is_read_only(permission_keys):
        raise ValidationError(
            "A reader seat requires a read-only role.",
            code="seat.reader_role_not_read_only",
            params={"write_permissions": sorted(permission_keys - READ_ONLY_PERMISSION_KEYS)},
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
