import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role, RolePermission


class RolesRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- permissions ---------------------------------------------------------------

    async def list_permissions(self) -> list[PermissionRow]:
        stmt = select(PermissionRow).order_by(PermissionRow.category, PermissionRow.key)
        return list((await self.db.execute(stmt)).scalars())

    async def get_permissions_by_ids(
        self, permission_ids: Sequence[uuid.UUID]
    ) -> list[PermissionRow]:
        stmt = select(PermissionRow).where(PermissionRow.id.in_(permission_ids))
        return list((await self.db.execute(stmt)).scalars())

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

    async def delete_role(self, role: Role) -> None:
        await self.db.delete(role)

    async def rebind_agents(
        self, agency_id: uuid.UUID, from_role_id: uuid.UUID, to_role_id: uuid.UUID
    ) -> None:
        """Move every agent of THIS agency wearing `from_role_id` onto
        `to_role_id` (copy-on-write rebind / clone deletion)."""
        await self.db.execute(
            update(Agent)
            .where(Agent.agency_id == agency_id, Agent.role_id == from_role_id)
            .values(role_id=to_role_id)
        )

    # --- members -------------------------------------------------------------------

    async def get_agent_in_agency(self, agency_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
        stmt = (
            select(Agent)
            .where(Agent.id == agent_id, Agent.agency_id == agency_id)
            .options(selectinload(Agent.role).selectinload(Role.permissions))
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_agents_with_permissions(self, agency_id: uuid.UUID) -> list[Agent]:
        stmt = (
            select(Agent)
            .where(Agent.agency_id == agency_id)
            .options(selectinload(Agent.role).selectinload(Role.permissions))
        )
        return list((await self.db.execute(stmt)).scalars())
