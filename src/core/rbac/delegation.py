"""What an agency flow may resolve and hand out: the platform barrier, the
platform-managed keys and the delegation ceiling, in one place.

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

The PLATFORM-MANAGED keys (PLATFORM_MANAGED_PERMISSIONS, the Email module's
`email.*`, decision of Alexandre on 08/10/2026) sit between the two: an
agency role may carry them, but only the platform puts them on it or takes
them off (the superadmin routes of src/email_campaigns/email_admin_router.py,
through RolesManager.set_platform_managed_permissions). In the agency flows,
whoever acts (an operator in its home agency included):

- a new role starts without them: creating one with a platform-managed key
  is refused;
- a matrix edit leaves them exactly as they are: adding or removing one is
  refused, and the ones kept are not the actor's to hand out, so they stay
  out of the ceiling;
- a copy never carries them: a copy-on-write clone starts without them (the
  platform's grant for a system role lives on the agency's clone, so a clone
  still to be made carries none), and neither does a duplicate;
- an assignment or an invitation hands out a role whole: the ceiling counts
  them like any key. The platform decides which roles carry them; the
  agency decides who wears which role, within its ceiling.

The refusal is ONE code, `role.platform_managed`, params `keys`: the
platform-managed keys the request would add or remove.
"""

import uuid
from collections.abc import Iterable

from shared.models.agent import Agent
from shared.models.rbac import Role
from src.core.exceptions import ForbiddenError
from src.core.rbac.baseline import (
    PLATFORM_MANAGED_PERMISSION_KEYS,
    PLATFORM_PERMISSION_KEYS,
    PLATFORM_ROLE_NAMES,
)
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


def platform_managed(keys: Iterable[str]) -> set[str]:
    """The platform-managed keys among `keys`."""
    return set(keys) & PLATFORM_MANAGED_PERMISSION_KEYS


def assert_platform_managed_unchanged(current: Iterable[str], requested: Iterable[str]) -> None:
    """An agency flow neither adds nor removes a platform-managed key: the
    role's `requested` matrix carries exactly the ones it carries now
    (`current`; none for a role still to be made). Checked before the
    ceiling, so the answer is the same whoever acts."""
    changed = sorted(platform_managed(current) ^ platform_managed(requested))
    if changed:
        # The keys ride in params (permission keys, never ids), like the
        # one of permission.denied, for the front's labels.
        raise ForbiddenError(
            f"Only the platform grants or withdraws: {', '.join(changed)}.",
            code="role.platform_managed",
            params={"keys": changed},
        )
