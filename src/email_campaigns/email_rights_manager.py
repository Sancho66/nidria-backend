"""The platform's grant of the Email keys, agency by agency and role by role
(decision of Alexandre, 08/10/2026, replacing the spec's N3).

Nobody holds `email.*` by default, the agency admin included: the keys are
platform-managed (PLATFORM_MANAGED_PERMISSIONS, src/core/rbac/baseline.py)
and no agency flow adds or removes one (src/core/rbac/delegation.py). A
superadmin chooses, for one agency, which of its roles carry which keys
(routes gated `agency.create`, superadmin only):
- `roles_of`: the roles the agency hands out to its members, exactly the
  list of GET /agencies/me/roles (its internal system roles, the platform
  role left out, and its own roles; a system role through the agency's
  copy-on-write clone once it has one), with their active wearers in the
  agency and their Email keys;
- `set_role_permissions`: the role carries exactly these Email keys from
  now on, nothing else of it changes. A shared system role is never
  written: the agency's copy-on-write clone carries the grant
  (RolesManager.set_platform_managed_permissions), so no other agency is
  touched.

Allowed while the module is off for the agency: the rights are prepared
before the switch is turned on, and open nothing until it is.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from src.agencies.agencies_repository import AgenciesRepository
from src.core.exceptions import NotFoundError
from src.core.rbac.baseline import EMAIL_PERMISSIONS
from src.core.rbac.permissions import Permission
from src.email_campaigns.email_rights_schema import EmailModuleRoleRead
from src.roles.roles_manager import RolesManager
from src.roles.roles_repository import RolesRepository

_EMAIL_KEYS = tuple(p.value for p in EMAIL_PERMISSIONS)


class EmailRightsManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.agencies = AgenciesRepository(db)
        self.roles = RolesRepository(db)

    async def roles_of(self, agency_id: uuid.UUID) -> list[EmailModuleRoleRead]:
        await self._agency(agency_id)
        roles = await self.agencies.list_roles(agency_id)
        ids = [role.id for role in roles]
        wearers = await self.roles.count_active_wearers(agency_id, ids)
        held = await self.roles.keys_of_roles(ids, _EMAIL_KEYS)
        return [
            EmailModuleRoleRead(
                id=role.id,
                name=role.name,
                is_system=role.is_system,
                cloned_from_role_id=role.cloned_from_role_id,
                active_agents=wearers[role.id],
                email_permissions=sorted(Permission(key) for key in held[role.id]),
            )
            for role in roles
        ]

    async def set_role_permissions(
        self, agency_id: uuid.UUID, role_id: uuid.UUID, permissions: Iterable[Permission]
    ) -> list[EmailModuleRoleRead]:
        await self._agency(agency_id)
        await RolesManager(self.db).set_platform_managed_permissions(
            agency_id, role_id, (p.value for p in permissions)
        )
        return await self.roles_of(agency_id)

    async def _agency(self, agency_id: uuid.UUID) -> None:
        if await self.agencies.get_agency(agency_id) is None:
            raise NotFoundError("Agency not found.", code="agency.not_found")
