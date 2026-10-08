"""The platform's grant of the Email keys, agency by agency and role by role
(decision of Alexandre, 08/10/2026): the bodies of
`GET /agencies/{agency_id}/email-module/roles` and
`PUT /agencies/{agency_id}/email-module/roles/{role_id}`."""

import uuid
from typing import Literal

from pydantic import BaseModel, field_validator

from src.core.rbac.baseline import EMAIL_PERMISSIONS
from src.core.rbac.permissions import Permission

_EMAIL_KEYS = frozenset(p.value for p in EMAIL_PERMISSIONS)


class EmailModuleRoleRead(BaseModel):
    """A role the agency hands out to its members, as the agency sees it (a
    system role through the agency's copy-on-write clone once it has one),
    and the Email keys the platform granted it."""

    id: uuid.UUID
    name: str
    is_system: bool
    #: Set: this is the agency's copy-on-write clone of that system role.
    cloned_from_role_id: uuid.UUID | None
    #: The agency's internal agents wearing it, the deactivated ones left out.
    active_agents: int
    #: The `email.*` keys it carries, sorted. Empty: its wearers do not
    #: reach the module.
    email_permissions: list[Permission]
    #: False: the platform never grants it the Email keys (its PUT answers
    #: 409 `email.role_not_grantable`), for `not_grantable_reason`.
    grantable: bool
    #: `viewer_seat`: the system viewer, the one role a reader seat wears, or
    #: a clone of it. A reader stays read-only.
    not_grantable_reason: Literal["viewer_seat"] | None


class EmailModuleRolePermissionsUpdate(BaseModel):
    """The `email.*` keys the role carries from now on, exactly: a subset of
    the six (a repeated key counts once), empty to withdraw them all. Any
    other key is refused (422)."""

    permissions: list[Permission]

    @field_validator("permissions")
    @classmethod
    def _email_keys_only(cls, value: list[Permission]) -> list[Permission]:
        others = sorted({p.value for p in value} - _EMAIL_KEYS)
        if others:
            raise ValueError(f"only email.* keys are granted here, not {', '.join(others)}")
        return sorted(set(value))
