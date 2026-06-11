import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from shared.models.rbac import AgentRole, Role, RolePermission
from shared.models.rbac import Permission as PermissionRow


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

    async def get_roles_with_permissions(self, role_ids: Sequence[uuid.UUID]) -> list[Role]:
        stmt = select(Role).where(Role.id.in_(role_ids)).options(selectinload(Role.permissions))
        return list((await self.db.execute(stmt)).scalars())

    async def get_role_by_name(self, agency_id: uuid.UUID, name: str) -> Role | None:
        stmt = select(Role).where(Role.agency_id == agency_id, Role.name == name)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_role(self, agency_id: uuid.UUID, name: str) -> Role:
        role = Role(agency_id=agency_id, name=name, is_system=False)
        self.db.add(role)
        return role

    async def replace_role_permissions(
        self, role_id: uuid.UUID, permission_ids: Sequence[uuid.UUID]
    ) -> None:
        await self.db.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
        for permission_id in permission_ids:
            self.db.add(RolePermission(role_id=role_id, permission_id=permission_id))

    async def count_role_assignments(self, role_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(AgentRole).where(AgentRole.role_id == role_id)
        return (await self.db.execute(stmt)).scalar_one()

    async def delete_role(self, role: Role) -> None:
        await self.db.delete(role)

    # --- members -------------------------------------------------------------------

    async def get_agent_in_agency(self, agency_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
        stmt = (
            select(Agent)
            .where(Agent.id == agent_id, Agent.agency_id == agency_id)
            .options(selectinload(Agent.roles).selectinload(Role.permissions))
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_agents_with_permissions(self, agency_id: uuid.UUID) -> list[Agent]:
        stmt = (
            select(Agent)
            .where(Agent.agency_id == agency_id)
            .options(selectinload(Agent.roles).selectinload(Role.permissions))
        )
        return list((await self.db.execute(stmt)).scalars())

    async def replace_agent_roles(self, agent_id: uuid.UUID, role_ids: Sequence[uuid.UUID]) -> None:
        await self.db.execute(delete(AgentRole).where(AgentRole.agent_id == agent_id))
        for role_id in role_ids:
            self.db.add(AgentRole(agent_id=agent_id, role_id=role_id))
