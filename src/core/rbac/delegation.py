"""What an agency flow may resolve and hand out: the platform barrier and the
delegation ceiling, in one place.

Every agency door that resolves a role id or grants permissions (the role
screens, the member role, the invitation, the impersonation target) reads
this module, so no two doors can disagree.

The PLATFORM layer, the `superadmin` system role and PLATFORM_PERMISSIONS
(baseline.py), belongs to Nidria's operators and is granted only by the seed
(scripts/seed.py, scripts/migrate_superadmins.py). No agency flow reaches it,
whoever acts, an operator working in its home agency included:

- the platform role is recognised by its IDENTITY, a system row with no
  agency and a reserved name (like `seats.is_the_system_viewer`), never by a
  name alone: an agency role named "superadmin" is an ordinary role, and a
  clone of the platform role is an agency row like any other;
- the delegation ceiling never holds a platform permission: no matrix, clone,
  duplicate, assignment or invitation of an agency flow puts one on a role.

Closed on 08/10/2026: an agency admin who knew the id of the superadmin role
renamed it (the copy-on-write clone carried every platform permission into
the agency), invited someone with the clone (no ceiling there, and a check on
the role name only) and the invitee reached /jobs.
"""

import uuid
from collections.abc import Iterable

from shared.models.agent import Agent
from shared.models.rbac import Role
from src.core.exceptions import ForbiddenError
from src.core.rbac.baseline import PLATFORM_PERMISSION_KEYS, PLATFORM_ROLE_NAMES
from src.core.rbac.enforcement import effective_permissions


def is_platform_role(role: Role) -> bool:
    """THE platform role row: a system role, with no agency, whose name is
    reserved. Never true for an agency row, whatever its name or origin."""
    return bool(role.is_system) and role.agency_id is None and role.name in PLATFORM_ROLE_NAMES


def is_reachable_role(role: Role, agency_id: uuid.UUID) -> bool:
    """May an agency flow of `agency_id` resolve this role at all? A shared
    system role or the agency's own row; never another agency's role, never
    the platform role. Each door answers a miss exactly like an unknown id,
    so no door confirms that the platform role exists."""
    if is_platform_role(role):
        return False
    return bool(role.is_system) or role.agency_id == agency_id


def delegation_ceiling(actor: Agent) -> set[str]:
    """The permission keys `actor` may hand out through an agency flow: the
    ones they hold, minus the platform ones."""
    return effective_permissions(actor) - PLATFORM_PERMISSION_KEYS


def assert_within_ceiling(actor: Agent, requested_keys: Iterable[str]) -> None:
    """Delegation ceiling: nobody hands out a permission they do not hold,
    neither directly in a matrix (create, edit, duplicate) nor through a
    role (assignment, invitation), and no agency flow hands out a platform
    permission."""
    missing = sorted(set(requested_keys) - delegation_ceiling(actor))
    if missing:
        # The permission KEYS stay in the English detail (logs, tests):
        # they are internal identifiers, never interpolated on screen.
        raise ForbiddenError(
            f"Beyond your permission ceiling: {', '.join(missing)}.",
            code="role.beyond_ceiling",
        )
