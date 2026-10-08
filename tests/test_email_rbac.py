"""The Email module's place in the RBAC engine.

- The catalogue carries exactly six `email.*` keys, category `email`.
- The default matrix (spec 4.2, the "right to activate"): admin and
  superadmin hold the six; case_manager, member and viewer hold none;
  external roles never. The keys are written out literally here, so this
  test does not lean on the constant it guards.
- A copy-on-write clone of the admin frozen before the release receives them
  at the next boot; a clone of case_manager never does.
- The platform routes are bound `agency.create` (superadmin only), the boot
  check passes with them, and fails when one loses its binding.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import ProtectedResource, Role, RolePermission
from src.core.enums import Audience
from src.core.rbac.baseline import (
    EXTERNAL_ROLE_NAMES,
    collect_bindings,
    seed_rbac_baseline,
)
from src.core.rbac.integrity import StartupError, assert_all_routes_bound
from src.core.rbac.permissions import Permission
from src.email_campaigns.email_admin_router import BINDINGS as EMAIL_ADMIN_BINDINGS
from src.roles.roles_manager import RolesManager
from tests.plugins.agent_plugin import MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

EMAIL_KEYS = {
    "email.view",
    "email.manage",
    "email.enroll",
    "email.pause",
    "email.remove",
    "email.sync",
}
PLATFORM_ROUTES = {
    ("GET", "/agencies/{agency_id}/email-module"),
    ("PATCH", "/agencies/{agency_id}/email-module"),
    ("PUT", "/agencies/{agency_id}/email-module/team"),
    ("DELETE", "/agencies/{agency_id}/email-module/team"),
}
_FROZEN_PAST = datetime(2020, 1, 1, tzinfo=UTC)


async def _keys_of(db: AsyncSession, role_id: uuid.UUID) -> set[str]:
    stmt = (
        select(PermissionRow.key)
        .join(RolePermission, RolePermission.permission_id == PermissionRow.id)
        .where(RolePermission.role_id == role_id)
    )
    return set((await db.execute(stmt)).scalars())


# --- catalogue and default matrix ----------------------------------------------------------


async def test_the_catalogue_has_the_six_email_keys_in_their_category(
    db_session: AsyncSession,
) -> None:
    assert {p.value for p in Permission if p.value.startswith("email.")} == EMAIL_KEYS
    rows = (
        await db_session.execute(
            select(PermissionRow.key, PermissionRow.category).where(
                PermissionRow.key.in_(EMAIL_KEYS)
            )
        )
    ).all()
    assert {key for key, _ in rows} == EMAIL_KEYS
    assert {category for _, category in rows} == {"email"}


async def test_only_admin_and_superadmin_hold_the_email_keys_by_default(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    for name in ("admin", "superadmin"):
        assert await _keys_of(db_session, system_roles[name].id) >= EMAIL_KEYS, name
    for name in ("case_manager", "member", "viewer", *EXTERNAL_ROLE_NAMES):
        held = await _keys_of(db_session, system_roles[name].id)
        assert EMAIL_KEYS.isdisjoint(held), f"{name} holds {EMAIL_KEYS & held}"


# --- copy-on-write clones -------------------------------------------------------------------


async def _clone(db: AsyncSession, actor_id: uuid.UUID, origin: Role, name: str) -> uuid.UUID:
    actor = (
        await db.execute(
            select(Agent)
            .where(Agent.id == actor_id)
            .options(selectinload(Agent.role).selectinload(Role.permissions))
        )
    ).scalar_one()
    clone = await RolesManager(db).rename_role(actor, origin.id, name)
    return clone.id


async def _freeze_before_the_email_release(db: AsyncSession, clone_id: uuid.UUID) -> None:
    """The clone as it was before this release: no email.* row, and its last
    matrix decision in the past (before the keys were born in this DB)."""
    email_ids = select(PermissionRow.id).where(PermissionRow.key.in_(EMAIL_KEYS))
    await db.execute(
        delete(RolePermission).where(
            RolePermission.role_id == clone_id, RolePermission.permission_id.in_(email_ids)
        )
    )
    await db.execute(
        update(Role)
        .where(Role.id == clone_id)
        .values(
            created_at=_FROZEN_PAST, updated_at=_FROZEN_PAST, permissions_reviewed_at=_FROZEN_PAST
        )
    )
    await db.commit()


async def test_an_admin_clone_receives_the_email_keys_at_the_next_boot(
    db_session: AsyncSession, make_agent: MakeAgent, system_roles: dict[str, Role]
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    clone_id = await _clone(db_session, admin.id, system_roles["admin"], "Admin (perso)")
    await _freeze_before_the_email_release(db_session, clone_id)
    assert EMAIL_KEYS.isdisjoint(await _keys_of(db_session, clone_id))  # the gap

    await seed_rbac_baseline(db_session, bindings=collect_bindings())  # the deployment
    db_session.expire_all()
    assert await _keys_of(db_session, clone_id) >= EMAIL_KEYS


async def test_a_case_manager_clone_never_receives_them(
    db_session: AsyncSession, make_agent: MakeAgent, system_roles: dict[str, Role]
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    clone_id = await _clone(
        db_session, admin.id, system_roles["case_manager"], "Gestionnaire (perso)"
    )
    await _freeze_before_the_email_release(db_session, clone_id)

    await seed_rbac_baseline(db_session, bindings=collect_bindings())
    db_session.expire_all()
    assert EMAIL_KEYS.isdisjoint(await _keys_of(db_session, clone_id))


# --- bindings and the boot check -----------------------------------------------------------


def test_the_platform_routes_are_superadmin_only() -> None:
    assert {(b.method, b.path) for b in EMAIL_ADMIN_BINDINGS} == PLATFORM_ROUTES
    for binding in EMAIL_ADMIN_BINDINGS:
        assert binding.audience == Audience.AGENT
        assert binding.permission == Permission.AGENCY_CREATE
    collected = {(b.method, b.path) for b in collect_bindings()}
    assert collected >= PLATFORM_ROUTES


async def test_the_boot_check_passes_with_the_email_routes(db_session: AsyncSession) -> None:
    from src.main import app

    declared = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", None) or ()
    }
    assert declared >= PLATFORM_ROUTES  # the router is really included
    await assert_all_routes_bound(app, db_session)  # must not raise


@pytest.mark.parametrize(("method", "path"), sorted(PLATFORM_ROUTES))
async def test_the_boot_check_fails_when_an_email_route_loses_its_binding(
    db_session: AsyncSession, method: str, path: str
) -> None:
    from src.main import app

    await db_session.execute(
        delete(ProtectedResource).where(
            ProtectedResource.method == method, ProtectedResource.route == path
        )
    )
    await db_session.commit()
    with pytest.raises(StartupError, match="email-module"):
        await assert_all_routes_bound(app, db_session)
