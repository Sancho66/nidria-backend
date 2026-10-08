import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role, RolePermission
from src.core.rbac.baseline import PLATFORM_PERMISSIONS

# Platform-scope permission keys (agency.create): real permissions that gate
# platform endpoints, but NEVER part of an agency's assignable surface — kept
# out of the catalogue so they can't appear in the role editor.
_PLATFORM_KEYS = [p.value for p in PLATFORM_PERMISSIONS]


class RolesRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- permissions ---------------------------------------------------------------

    async def list_permissions(self) -> list[PermissionRow]:
        # The agency-facing catalogue (GET /permissions, role editor): excludes
        # platform-scope permissions (agency.create) — held only by the
        # superadmin role, never assignable to an agency role.
        stmt = (
            select(PermissionRow)
            .where(PermissionRow.key.not_in(_PLATFORM_KEYS))
            .order_by(PermissionRow.category, PermissionRow.key)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_permissions_by_ids(
        self, permission_ids: Sequence[uuid.UUID]
    ) -> list[PermissionRow]:
        stmt = select(PermissionRow).where(PermissionRow.id.in_(permission_ids))
        return list((await self.db.execute(stmt)).scalars())

    async def get_permissions_by_keys(self, keys: Sequence[str]) -> list[PermissionRow]:
        stmt = select(PermissionRow).where(PermissionRow.key.in_(keys))
        return list((await self.db.execute(stmt)).scalars())

    async def keys_of_roles(
        self, role_ids: Sequence[uuid.UUID], keys: Iterable[str]
    ) -> dict[uuid.UUID, set[str]]:
        """{role_id: the ones of `keys` it carries}, for many roles at once."""
        stmt = (
            select(RolePermission.role_id, PermissionRow.key)
            .join(PermissionRow, PermissionRow.id == RolePermission.permission_id)
            .where(RolePermission.role_id.in_(role_ids), PermissionRow.key.in_(list(keys)))
        )
        held: dict[uuid.UUID, set[str]] = {role_id: set() for role_id in role_ids}
        for role_id, key in (await self.db.execute(stmt)).all():
            held[role_id].add(key)
        return held

    # --- roles ---------------------------------------------------------------------

    async def get_role_with_permissions(self, role_id: uuid.UUID) -> Role | None:
        stmt = select(Role).where(Role.id == role_id).options(selectinload(Role.permissions))
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_role_by_name(self, agency_id: uuid.UUID, name: str) -> Role | None:
        stmt = select(Role).where(Role.agency_id == agency_id, Role.name == name)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_clone_of(self, agency_id: uuid.UUID, system_role_id: uuid.UUID) -> Role | None:
        """The agency's copy-on-write clone of a system role, if any."""
        stmt = (
            select(Role)
            .where(Role.agency_id == agency_id, Role.cloned_from_role_id == system_role_id)
            .options(selectinload(Role.permissions))
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_role(
        self,
        agency_id: uuid.UUID,
        name: str,
        cloned_from_role_id: uuid.UUID | None = None,
    ) -> Role:
        role = Role(
            agency_id=agency_id,
            name=name,
            is_system=False,
            cloned_from_role_id=cloned_from_role_id,
        )
        self.db.add(role)
        return role

    async def replace_role_permissions(
        self, role_id: uuid.UUID, permission_ids: Sequence[uuid.UUID]
    ) -> None:
        await self.db.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
        for permission_id in permission_ids:
            self.db.add(RolePermission(role_id=role_id, permission_id=permission_id))

    async def count_role_assignments(self, role_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(Agent).where(Agent.role_id == role_id)
        return (await self.db.execute(stmt)).scalar_one()

    async def count_active_wearers(
        self, agency_id: uuid.UUID, role_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """{role_id: its wearers in THIS agency}: internal agents, the
        deactivated ones left out (a shared system role is worn in many
        agencies, only this one's count)."""
        stmt = (
            select(Agent.role_id, func.count())
            .where(
                Agent.agency_id == agency_id,
                Agent.role_id.in_(role_ids),
                Agent.is_external.is_(False),
                Agent.deactivated_at.is_(None),
            )
            .group_by(Agent.role_id)
        )
        counts: dict[uuid.UUID, int] = {role_id: 0 for role_id in role_ids}
        for role_id, count in (await self.db.execute(stmt)).all():
            counts[role_id] = count
        return counts

    async def delete_role(self, role: Role) -> None:
        await self.db.delete(role)

    async def rebind_agents(
        self, agency_id: uuid.UUID, from_role_id: uuid.UUID, to_role_id: uuid.UUID
    ) -> None:
        """Move every agent of THIS agency wearing `from_role_id` onto
        `to_role_id` (copy-on-write rebind / clone deletion). INTERNAL
        only — externals never wear internal/cloned roles."""
        await self.db.execute(
            update(Agent)
            .where(
                Agent.agency_id == agency_id,
                Agent.role_id == from_role_id,
                Agent.is_external.is_(False),
            )
            .values(role_id=to_role_id)
        )

    # --- members -------------------------------------------------------------------

    async def get_agent_in_agency(self, agency_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
        # INTERNAL only: role management (set_member_role) never targets
        # an external — they are managed through the external flow.
        stmt = (
            select(Agent)
            .where(
                Agent.id == agent_id,
                Agent.agency_id == agency_id,
                Agent.is_external.is_(False),
            )
            .options(selectinload(Agent.role).selectinload(Role.permissions))
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_agents_with_permissions(self, agency_id: uuid.UUID) -> list[Agent]:
        # INTERNAL only: the anti-lockout guard counts manager-capable
        # internal agents; an external (zero perms) must never count.
        stmt = (
            select(Agent)
            .where(
                Agent.agency_id == agency_id,
                Agent.is_external.is_(False),
                # A deactivated manager is NOT a capable manager: the
                # anti-lockout guard must not count them.
                Agent.deactivated_at.is_(None),
            )
            .options(selectinload(Agent.role).selectinload(Role.permissions))
        )
        return list((await self.db.execute(stmt)).scalars())
