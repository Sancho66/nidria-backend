import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role
from src.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from src.core.rbac.enforcement import effective_permissions
from src.core.rbac.permissions import Permission
from src.roles.roles_repository import RolesRepository

_SYSTEM_ROLE_LOCKED = "System roles are shared across agencies and cannot be modified."


class RolesManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = RolesRepository(db)

    # --- guards ----------------------------------------------------------------------

    def _assert_within_ceiling(self, actor: Agent, requested_keys: Iterable[str]) -> None:
        """Delegation ceiling: nobody hands out a permission they do not
        hold — neither directly in a matrix (create/edit/duplicate) nor
        indirectly through role assignment."""
        missing = sorted(set(requested_keys) - effective_permissions(actor))
        if missing:
            raise ForbiddenError(f"Beyond your permission ceiling: {', '.join(missing)}.")

    async def _get_custom_role_in_agency(self, actor: Agent, role_id: uuid.UUID) -> Role:
        """System role → explicit 403; foreign custom role → 404 (no
        cross-agency existence leak). Order matters: system roles have
        agency_id NULL and would 404 under a plain agency filter."""
        role = await self.repo.get_role_with_permissions(role_id)
        if role is None:
            raise NotFoundError("Role not found.")
        if role.is_system:
            raise ForbiddenError(_SYSTEM_ROLE_LOCKED)
        if role.agency_id != actor.agency_id:
            raise NotFoundError("Role not found.")
        return role

    async def _resolve_permissions(
        self, permission_ids: Sequence[uuid.UUID]
    ) -> list[PermissionRow]:
        unique_ids = list(dict.fromkeys(permission_ids))
        rows = await self.repo.get_permissions_by_ids(unique_ids)
        unknown = sorted(str(i) for i in set(unique_ids) - {row.id for row in rows})
        if unknown:
            raise ValidationError(f"Unknown permission ids: {', '.join(unknown)}.")
        return rows

    async def _assert_name_free(self, agency_id: uuid.UUID, name: str) -> None:
        if await self.repo.get_role_by_name(agency_id, name) is not None:
            raise ConflictError(f"A role named {name!r} already exists in this agency.")

    async def _assert_agency_keeps_manager(
        self,
        agency_id: uuid.UUID,
        *,
        reassigned_agent: tuple[uuid.UUID, set[str]] | None = None,
        edited_role: tuple[uuid.UUID, set[str]] | None = None,
    ) -> None:
        """Anti-lockout (system integrity, not business preference):
        with no platform superadmin, an agency that loses its last
        agent.manage holder is unrecoverable. The capability is what
        counts, not the role — a custom role can carry it.

        Simulates the post-mutation state: `reassigned_agent` replaces
        one agent's whole role set (PUT members/{id}/roles);
        `edited_role` replaces one custom role's matrix for everyone
        holding it (PUT roles/{id}/permissions) — the vector where the
        sole manager can strip the capability from their OWN role.
        On the assignment path the caller necessarily holds
        agent.manage today (binding) and cannot self-modify, but the
        guard is the invariant, not an analysis of current callers —
        it must survive impersonation or any future relaxation."""
        agents = await self.repo.list_agents_with_permissions(agency_id)
        for agent in agents:
            if reassigned_agent is not None and agent.id == reassigned_agent[0]:
                keys = set(reassigned_agent[1])
            else:
                keys = {
                    perm.key
                    for role in agent.roles
                    if edited_role is None or role.id != edited_role[0]
                    for perm in role.permissions
                }
                if edited_role is not None and any(
                    role.id == edited_role[0] for role in agent.roles
                ):
                    keys |= edited_role[1]
            if Permission.AGENT_MANAGE.value in keys:
                return
        raise ConflictError(
            "This operation would leave the agency without any manager "
            "(no agent holding agent.manage)."
        )

    # --- catalogue ---------------------------------------------------------------------

    async def list_permissions(self) -> list[PermissionRow]:
        return await self.repo.list_permissions()

    # --- custom roles --------------------------------------------------------------------

    async def create_role(
        self, actor: Agent, name: str, permission_ids: Sequence[uuid.UUID]
    ) -> Role:
        permissions = await self._resolve_permissions(permission_ids)
        self._assert_within_ceiling(actor, (p.key for p in permissions))
        await self._assert_name_free(actor.agency_id, name)
        role = self.repo.add_role(actor.agency_id, name)
        await self.db.flush()
        await self.repo.replace_role_permissions(role.id, [p.id for p in permissions])
        await self.db.commit()
        return await self._reload(role.id)

    async def rename_role(self, actor: Agent, role_id: uuid.UUID, name: str) -> Role:
        role = await self._get_custom_role_in_agency(actor, role_id)
        if name != role.name:
            await self._assert_name_free(actor.agency_id, name)
            role.name = name
            await self.db.commit()
        return await self._reload(role.id)

    async def set_role_permissions(
        self, actor: Agent, role_id: uuid.UUID, permission_ids: Sequence[uuid.UUID]
    ) -> Role:
        role = await self._get_custom_role_in_agency(actor, role_id)
        permissions = await self._resolve_permissions(permission_ids)
        self._assert_within_ceiling(actor, (p.key for p in permissions))
        new_keys = {p.key for p in permissions}
        await self._assert_agency_keeps_manager(actor.agency_id, edited_role=(role.id, new_keys))
        await self.repo.replace_role_permissions(role.id, [p.id for p in permissions])
        await self.db.commit()
        # Explicit refresh: with expire_on_commit=False the identity map
        # would hand back the stale pre-edit collection.
        await self.db.refresh(role, ["permissions"])
        return role

    async def delete_role(self, actor: Agent, role_id: uuid.UUID) -> None:
        role = await self._get_custom_role_in_agency(actor, role_id)
        assigned = await self.repo.count_role_assignments(role.id)
        if assigned:
            raise ConflictError(f"Role is assigned to {assigned} agent(s).")
        await self.repo.delete_role(role)
        await self.db.commit()

    async def duplicate_role(self, actor: Agent, role_id: uuid.UUID, name: str) -> Role:
        """The 'start from a system role' path: clones the matrix into
        a CUSTOM role of the actor's agency. Subject to the ceiling —
        otherwise duplicating an oversized role is the copy bypass."""
        source = await self.repo.get_role_with_permissions(role_id)
        if source is None or (not source.is_system and source.agency_id != actor.agency_id):
            raise NotFoundError("Role not found.")
        self._assert_within_ceiling(actor, (p.key for p in source.permissions))
        await self._assert_name_free(actor.agency_id, name)
        clone = self.repo.add_role(actor.agency_id, name)
        await self.db.flush()
        await self.repo.replace_role_permissions(clone.id, [p.id for p in source.permissions])
        await self.db.commit()
        return await self._reload(clone.id)

    async def _reload(self, role_id: uuid.UUID) -> Role:
        role = await self.repo.get_role_with_permissions(role_id)
        assert role is not None
        return role

    # --- member role assignment --------------------------------------------------------

    async def set_member_roles(
        self, actor: Agent, agent_id: uuid.UUID, role_ids: Sequence[uuid.UUID]
    ) -> Agent:
        if agent_id == actor.id:
            raise ForbiddenError("You cannot modify your own roles.")
        target = await self.repo.get_agent_in_agency(actor.agency_id, agent_id)
        if target is None:
            raise NotFoundError("Agent not found.")

        unique_ids = list(dict.fromkeys(role_ids))
        roles = await self.repo.get_roles_with_permissions(unique_ids)
        unknown = sorted(str(i) for i in set(unique_ids) - {role.id for role in roles})
        if unknown:
            raise ValidationError(f"Unknown role ids: {', '.join(unknown)}.")
        foreign = sorted(
            str(role.id)
            for role in roles
            if not role.is_system and role.agency_id != actor.agency_id
        )
        if foreign:
            raise ValidationError(f"Roles do not belong to this agency: {', '.join(foreign)}.")

        new_keys = {p.key for role in roles for p in role.permissions}
        self._assert_within_ceiling(actor, new_keys)
        await self._assert_agency_keeps_manager(
            actor.agency_id, reassigned_agent=(target.id, new_keys)
        )

        await self.repo.replace_agent_roles(target.id, [role.id for role in roles])
        await self.db.commit()
        # Explicit refresh: with expire_on_commit=False the identity map
        # would hand back the stale pre-edit collection.
        await self.db.refresh(target, ["roles"])
        return target
