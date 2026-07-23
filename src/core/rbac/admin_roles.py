"""Single source of truth for 'is this THE agency admin?'.

An agency admin wears the SYSTEM 'admin' role, OR its copy-on-write clone
(an agency that edits the admin role rebinds its agents onto a clone whose
`cloned_from_role_id` points back to the system 'admin' — they are still the
admin). `consent_gate.is_agency_admin` AND the agency-switcher impersonation
(`get_an_admin_of_agency`) BOTH consume the ONE clause below, so the two can
never disagree about who the admin is. Do not inline this rule anywhere —
extend it here.
"""

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from shared.models.rbac import Role

SYSTEM_ADMIN_ROLE_NAME = "admin"


def is_admin_role_clause(role: type[Role] = Role) -> ColumnElement[bool]:
    """SQL predicate: does `role` denote the agency-admin role — the system
    'admin', OR a copy-on-write clone whose origin is the system 'admin'?

    `role` is the mapped `Role` class (default) or an alias already present
    in the surrounding query. The CoW origin is resolved with a correlated
    EXISTS, so callers add NO extra join — they drop the clause straight into
    a WHERE that already references `role`.
    """
    origin = aliased(Role)
    is_system_admin = and_(role.is_system.is_(True), role.name == SYSTEM_ADMIN_ROLE_NAME)
    is_clone_of_system_admin = (
        select(origin.id)
        .where(
            origin.id == role.cloned_from_role_id,
            origin.is_system.is_(True),
            origin.name == SYSTEM_ADMIN_ROLE_NAME,
        )
        .exists()
    )
    return or_(is_system_admin, is_clone_of_system_admin)
