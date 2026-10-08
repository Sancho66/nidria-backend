import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role
from src.core.enums import SeatType
from src.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from src.core.rbac.baseline import PLATFORM_MANAGED_PERMISSION_KEYS, PLATFORM_PERMISSION_KEYS
from src.core.rbac.delegation import (
    assert_platform_managed_unchanged,
    assert_within_ceiling,
    is_platform_role,
    is_reachable_role,
    platform_managed,
)
from src.core.rbac.enforcement import effective_permissions
from src.core.rbac.permissions import Permission
from src.core.seats import assert_reader_role_locked
from src.roles.roles_repository import RolesRepository

_SYSTEM_ROLE_LOCKED = "System roles are shared across agencies and cannot be deleted."
# Every role lookup that misses (unknown, foreign custom, platform-reserved)
# answers ONE code, `role.not_found`, whatever the door — written literally
# at each raise so the served-codes scan (AST, literals only) sees it.


class RolesManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = RolesRepository(db)

    # --- guards ----------------------------------------------------------------------
    # The delegation ceiling and the platform barrier live in
    # src/core/rbac/delegation.py, shared with the invitation door.

    async def _resolve_permissions(
        self, permission_ids: Sequence[uuid.UUID]
    ) -> list[PermissionRow]:
        unique_ids = list(dict.fromkeys(permission_ids))
        rows = await self.repo.get_permissions_by_ids(unique_ids)
        unknown = sorted(str(i) for i in set(unique_ids) - {row.id for row in rows})
        if unknown:
            # Category code on purpose: the ids come from GET /permissions and
            # the catalogue is insert-only — only a hand-built request misses.
            raise ValidationError(f"Unknown permission ids: {', '.join(unknown)}.")
        return rows

    async def _assert_name_free(self, agency_id: uuid.UUID, name: str) -> None:
        if await self.repo.get_role_by_name(agency_id, name) is not None:
            raise ConflictError(
                f"A role named {name!r} already exists in this agency.",
                code="role.name_taken",
                params={"name": name},
            )

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
        counts, not the role.

        Simulates the post-mutation state on the SINGLE-role model:
        `reassigned_agent` swaps one agent's role (PUT member role);
        `edited_role` swaps the permission set of one role for all its
        wearers (matrix edit, copy-on-write rebind, clone deletion)."""
        agents = await self.repo.list_agents_with_permissions(agency_id)
        for agent in agents:
            if reassigned_agent is not None and agent.id == reassigned_agent[0]:
                keys = set(reassigned_agent[1])
            elif edited_role is not None and agent.role_id == edited_role[0]:
                keys = set(edited_role[1])
            else:
                keys = effective_permissions(agent)
            if Permission.AGENT_MANAGE.value in keys:
                return
        # Raised under the ROLES code; the member flows that reuse this
        # guard (deactivation, seat flip) may re-code it for their own screen.
        raise ConflictError(
            "This operation would leave the agency without any manager "
            "(no agent holding agent.manage).",
            code="role.last_manager",
        )

    # --- copy-on-write -----------------------------------------------------------------

    async def _clone_for_edit(self, agency_id: uuid.UUID, system_role: Role) -> Role:
        """Copy-on-write: editing a system role never touches it. The
        agency gets (or reuses) a CUSTOM clone — same name, same matrix,
        `cloned_from_role_id` set — and every agent of THIS agency
        wearing the system role is rebound to the clone. The rebind
        copies the exact permission set, so it can never trip the
        anti-lockout guard by itself. Shared by the agency flows and the
        platform's grant of the platform-managed keys."""
        existing = await self.repo.get_clone_of(agency_id, system_role.id)
        if existing is not None:
            return existing
        conflicting = await self.repo.get_role_by_name(agency_id, system_role.name)
        if conflicting is not None:
            # No name param: a system role's name is a technical key
            # ("case_manager") the screen labels itself.
            raise ConflictError(
                f"A custom role named {system_role.name!r} already exists in this "
                "agency and is not a clone of the system role.",
                code="role.clone_name_taken",
            )
        clone = self.repo.add_role(agency_id, system_role.name, cloned_from_role_id=system_role.id)
        await self.db.flush()
        # A clone is an agency row: it never copies a platform permission,
        # whatever its origin holds (defense in depth: the platform role
        # itself never gets here, _editable_role answers it a 404). Nor a
        # platform-managed one: the platform's grant for a system role lives
        # on the agency's clone, so a clone still to be made carries none.
        await self.repo.replace_role_permissions(
            clone.id,
            [
                p.id
                for p in system_role.permissions
                if p.key not in PLATFORM_PERMISSION_KEYS
                and p.key not in PLATFORM_MANAGED_PERMISSION_KEYS
            ],
        )
        await self.repo.rebind_agents(agency_id, system_role.id, clone.id)
        await self.db.flush()
        await self.db.refresh(clone, ["permissions"])
        return clone

    async def _editable_role(self, actor: Agent, role_id: uuid.UUID) -> Role:
        """Resolve a role for mutation: a system role yields its
        copy-on-write clone; a foreign custom role or the platform role is
        a 404 (no existence leak). Cloning the platform role was the first
        step of the escalation closed on 08/10/2026."""
        role = await self.repo.get_role_with_permissions(role_id)
        if role is None or not is_reachable_role(role, actor.agency_id):
            raise NotFoundError("Role not found.", code="role.not_found")
        if role.is_system:
            return await self._clone_for_edit(actor.agency_id, role)
        return role

    # --- catalogue ---------------------------------------------------------------------

    async def list_permissions(self) -> list[PermissionRow]:
        return await self.repo.list_permissions()

    # --- roles ---------------------------------------------------------------------------

    async def get_role(self, actor: Agent, role_id: uuid.UUID) -> Role:
        """Read mirror of the mutations: system role OR own custom;
        a foreign custom role or the platform role is a 404, same rule
        everywhere."""
        role = await self.repo.get_role_with_permissions(role_id)
        if role is None or not is_reachable_role(role, actor.agency_id):
            raise NotFoundError("Role not found.", code="role.not_found")
        return role

    async def create_role(
        self, actor: Agent, name: str, permission_ids: Sequence[uuid.UUID]
    ) -> Role:
        permissions = await self._resolve_permissions(permission_ids)
        keys = {p.key for p in permissions}
        # A new role starts without platform-managed keys: asking for one is
        # adding it.
        assert_platform_managed_unchanged((), keys)
        assert_within_ceiling(actor, keys)
        await self._assert_name_free(actor.agency_id, name)
        role = self.repo.add_role(actor.agency_id, name)
        await self.db.flush()
        await self.repo.replace_role_permissions(role.id, [p.id for p in permissions])
        await self.db.commit()
        await self.db.refresh(role, ["permissions"])
        return role

    async def rename_role(self, actor: Agent, role_id: uuid.UUID, name: str) -> Role:
        role = await self._editable_role(actor, role_id)
        if name != role.name:
            await self._assert_name_free(actor.agency_id, name)
            role.name = name
        await self.db.commit()
        await self.db.refresh(role, ["permissions"])
        return role

    async def set_role_permissions(
        self, actor: Agent, role_id: uuid.UUID, permission_ids: Sequence[uuid.UUID]
    ) -> Role:
        role = await self._editable_role(actor, role_id)
        permissions = await self._resolve_permissions(permission_ids)
        new_keys = {p.key for p in permissions}
        # The platform-managed keys stay exactly as the role carries them
        # (none on a clone this edit just made). The ones kept are the
        # platform's grant, not the actor's to hand out: out of the ceiling.
        assert_platform_managed_unchanged((p.key for p in role.permissions), new_keys)
        assert_within_ceiling(actor, new_keys - platform_managed(new_keys))
        await self._assert_agency_keeps_manager(actor.agency_id, edited_role=(role.id, new_keys))
        await self.repo.replace_role_permissions(role.id, [p.id for p in permissions])
        # This matrix write IS the agency's decision moment — and the ONLY
        # gesture that stamps it (a rename never does). The seed's clone
        # catch-up (propagate_new_permissions_to_clones) only adds permissions
        # born AFTER this timestamp: an explicit removal is never overridden.
        role.permissions_reviewed_at = datetime.now(UTC)
        await self.db.commit()
        # Explicit refresh: with expire_on_commit=False the identity map
        # would hand back the stale pre-edit collection.
        await self.db.refresh(role, ["permissions"])
        return role

    async def delete_role(self, actor: Agent, role_id: uuid.UUID) -> None:
        role = await self.repo.get_role_with_permissions(role_id)
        if role is None or not is_reachable_role(role, actor.agency_id):
            raise NotFoundError("Role not found.", code="role.not_found")
        if role.is_system:
            raise ForbiddenError(_SYSTEM_ROLE_LOCKED, code="role.system_locked")

        if role.cloned_from_role_id is not None:
            # Deleting a clone = un-masking: wearers fall back to the
            # original system role (its matrix), anti-lockout permitting.
            origin = await self.repo.get_role_with_permissions(role.cloned_from_role_id)
            if origin is None:
                # Category code on purpose: system roles are never deleted
                # (and the FK is SET NULL) — an integrity net, not a screen.
                raise ConflictError("Origin system role of this clone no longer exists.")
            # Except a clone of the platform role (the escalation closed on
            # 08/10/2026 minted some): un-masking it would put its wearers ON
            # the platform role. It is deleted like a plain custom role below.
            if not is_platform_role(origin):
                origin_keys = {p.key for p in origin.permissions}
                await self._assert_agency_keeps_manager(
                    actor.agency_id, edited_role=(role.id, origin_keys)
                )
                await self.repo.rebind_agents(actor.agency_id, role.id, origin.id)
                await self.repo.delete_role(role)
                await self.db.commit()
                return

        assigned = await self.repo.count_role_assignments(role.id)
        if assigned:
            raise ConflictError(
                f"Role is assigned to {assigned} agent(s).",
                code="role.in_use",
                params={"name": role.name, "count": assigned},
            )
        await self.repo.delete_role(role)
        await self.db.commit()

    async def duplicate_role(self, actor: Agent, role_id: uuid.UUID, name: str) -> Role:
        """The explicit 'start from a role' path: clones the matrix into
        a CUSTOM role of the actor's agency, with NO copy-on-write link
        (a duplicate never masks its source). Subject to the ceiling —
        otherwise duplicating an oversized role is the copy bypass. The
        copy leaves the platform-managed keys behind: the platform granted
        them to the source, not to a new role."""
        source = await self.repo.get_role_with_permissions(role_id)
        if source is None or not is_reachable_role(source, actor.agency_id):
            raise NotFoundError("Role not found.", code="role.not_found")
        copied = [p for p in source.permissions if p.key not in PLATFORM_MANAGED_PERMISSION_KEYS]
        assert_within_ceiling(actor, (p.key for p in copied))
        await self._assert_name_free(actor.agency_id, name)
        duplicate = self.repo.add_role(actor.agency_id, name)
        await self.db.flush()
        await self.repo.replace_role_permissions(duplicate.id, [p.id for p in copied])
        await self.db.commit()
        await self.db.refresh(duplicate, ["permissions"])
        return duplicate

    # --- member role assignment --------------------------------------------------------

    async def set_member_role(self, actor: Agent, agent_id: uuid.UUID, role_id: uuid.UUID) -> Agent:
        if agent_id == actor.id:
            raise ForbiddenError("You cannot modify your own role.", code="role.own_role_locked")
        # get_agent_in_agency excludes externals → an external target is
        # already a 404 here (they're managed via the external flow).
        target = await self.repo.get_agent_in_agency(actor.agency_id, agent_id)
        if target is None:
            raise NotFoundError("Agent not found.", code="member.not_found")

        role = await self.repo.get_role_with_permissions(role_id)
        if role is None or not is_reachable_role(role, actor.agency_id):
            # One answer for every role out of reach: unknown, another
            # agency's custom role, or the platform role (superadmin), which
            # only the seed grants: never assignable through the UI, not even
            # by a superadmin, and recognised by identity, never by name.
            raise ValidationError(
                "Role does not exist or does not belong to this agency.", code="role.not_found"
            )
        if role.is_external:
            # An external (provider) role is never assignable via the
            # internal member-role flow.
            raise ValidationError(
                "External roles cannot be assigned to internal members.",
                code="role.external_not_assignable",
            )
        if role.is_system:
            # Masking holds for assignment too: an agent must never wear
            # a role that GET /roles no longer lists for their agency.
            clone = await self.repo.get_clone_of(actor.agency_id, role.id)
            if clone is not None:
                # The clone id stays in the detail only (never a param).
                raise ConflictError(
                    f"This system role is masked by the agency clone {clone.id} — "
                    "assign the clone instead.",
                    code="role.masked_by_clone",
                )

        new_keys = {p.key for p in role.permissions}
        # The whole role, its platform-managed keys included: the platform
        # decides which roles carry them, the agency who wears which role,
        # within its own ceiling.
        assert_within_ceiling(actor, new_keys)
        await self._assert_agency_keeps_manager(
            actor.agency_id, reassigned_agent=(target.id, new_keys)
        )
        if target.seat_type == SeatType.READER.value:
            # THE SEAT CAPS THE ROLE (verrou 08/08): a reader wears the
            # system viewer and nothing else — direct assignment, custom
            # role or clone, every elevation path dies HERE, the single
            # role-assignment door. Widening = flip the seat first.
            assert_reader_role_locked(role)

        target.role_id = role.id
        await self.db.commit()
        # Explicit refresh: the role relationship must reflect the swap.
        await self.db.refresh(target, ["role"])
        return target

    # --- the platform's grant of the platform-managed keys ------------------------------

    async def set_platform_managed_permissions(
        self, agency_id: uuid.UUID, role_id: uuid.UUID, keys: Iterable[str]
    ) -> Role:
        """The platform's door (superadmin routes only, never an agency
        flow): from now on the role carries EXACTLY `keys` among the
        platform-managed keys, its other permissions untouched.

        A role the agency hands out: a shared internal system role, or the
        agency's own row. A shared system role is never written (every
        agency would be touched): the agency's copy-on-write clone carries
        the grant, the existing one or one made like an agency edit makes
        it, the agency's wearers of the system role rebound (409
        `role.clone_name_taken` when a custom role of the agency already
        bears the system role's name). The platform role, an external role
        or another agency's role answers like an unknown role (404
        `role.not_found`). `permissions_reviewed_at` is not bumped: a grant
        is not the agency's matrix decision."""
        wanted = set(keys)
        outside = sorted(wanted - PLATFORM_MANAGED_PERMISSION_KEYS)
        if outside:
            # The routes accept the platform-managed keys only (422 there):
            # an integrity net for another caller.
            raise ValidationError(f"Not platform-managed: {', '.join(outside)}.")
        role = await self.repo.get_role_with_permissions(role_id)
        if role is None or not is_reachable_role(role, agency_id) or role.is_external:
            raise NotFoundError("Role not found.", code="role.not_found")
        if role.is_system:
            role = await self._clone_for_edit(agency_id, role)
        granted = await self.repo.get_permissions_by_keys(sorted(wanted))
        if len(granted) != len(wanted):
            # Category code on purpose: the catalogue is synced at every boot
            # (insert-only), so a missing key is an integrity net, not a screen.
            raise ConflictError("A platform-managed permission is missing from the catalogue.")
        kept = [p.id for p in role.permissions if p.key not in PLATFORM_MANAGED_PERMISSION_KEYS]
        await self.repo.replace_role_permissions(role.id, [*kept, *(p.id for p in granted)])
        await self.db.commit()
        # Explicit refresh: with expire_on_commit=False the identity map
        # would hand back the stale pre-grant collection.
        await self.db.refresh(role, ["permissions"])
        return role
