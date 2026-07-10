"""Copy-on-write clones vs newborn permissions (Alexandre's prod finding): a
clone frozen before a permission was born never received it — ignorance, not a
decision. The seed (which runs at EVERY deployment via start.sh → scripts/seed.py
→ seed_rbac_baseline) now closes that gap, additively, while an EXPLICIT agency
removal is never overridden: set_role_permissions stamps role.updated_at, and
the propagation only adds permissions born strictly after the clone's last
decision (creation or matrix edit). These tests are the merge condition."""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role, RolePermission
from src.core.enums import Audience
from src.core.rbac.baseline import collect_bindings, seed_rbac_baseline
from src.core.security import create_access_token
from src.roles.roles_manager import RolesManager
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase

pytestmark = pytest.mark.usefixtures("rbac_baseline")

_FROZEN_PAST = datetime(2020, 1, 1, tzinfo=UTC)


async def _boot(db: AsyncSession) -> None:
    """One deployment boot: exactly what start.sh's seed run executes."""
    await seed_rbac_baseline(db, bindings=collect_bindings())
    db.expire_all()


async def _load_actor(db: AsyncSession, agent_id: uuid.UUID) -> Agent:
    """Agent with the eager role→permissions chain (mirrors get_current_agent),
    so manager calls never lazy-load in async."""
    stmt = (
        select(Agent)
        .where(Agent.id == agent_id)
        .options(selectinload(Agent.role).selectinload(Role.permissions))
    )
    return (await db.execute(stmt)).scalar_one()


async def _backdate(db: AsyncSession, role_id: uuid.UUID, when: datetime) -> None:
    """Simulate a clone frozen in the past (before a permission's birth):
    creation AND last matrix decision both land at `when`."""
    await db.execute(
        update(Role)
        .where(Role.id == role_id)
        .values(created_at=when, updated_at=when, permissions_reviewed_at=when)
    )
    await db.commit()


async def _perm_id(db: AsyncSession, key: str) -> uuid.UUID:
    return (await db.execute(select(PermissionRow.id).where(PermissionRow.key == key))).scalar_one()


async def _clone_keys(db: AsyncSession, role_id: uuid.UUID) -> set[str]:
    stmt = (
        select(PermissionRow.key)
        .join(RolePermission, RolePermission.permission_id == PermissionRow.id)
        .where(RolePermission.role_id == role_id)
    )
    return set((await db.execute(stmt)).scalars())


async def _matrix_snapshot(db: AsyncSession) -> set[tuple[uuid.UUID, uuid.UUID]]:
    rows = (await db.execute(select(RolePermission))).scalars().all()
    return {(rp.role_id, rp.permission_id) for rp in rows}


async def _make_clone(db: AsyncSession, actor: Agent, system_role: Role, name: str) -> uuid.UUID:
    """The agency personalizes a system role → copy-on-write clone (full
    matrix copied, the agency's agents rebound). Returns the clone's PLAIN id:
    _boot() expires the session, so tests must never hold ORM objects across
    a boot (an expired attribute would lazy-load synchronously)."""
    loaded = await _load_actor(db, actor.id)
    clone = await RolesManager(db).rename_role(loaded, system_role.id, name)
    return clone.id


async def _birth_new_permission(db: AsyncSession, system_role_id: uuid.UUID, key: str) -> None:
    """Simulate a later release: a new catalogue permission (insert-only sync
    would create it exactly like this) granted to the system role by the seed."""
    perm = PermissionRow(key=key, label=key, category=key.split(".")[0])
    db.add(perm)
    await db.flush()
    db.add(RolePermission(role_id=system_role_id, permission_id=perm.id))
    await db.commit()


# --- 1. a clone created BEFORE a new permission receives it at the next seed --------


async def test_clone_created_before_new_permission_receives_it(
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    clone_id = await _make_clone(db_session, admin, system_roles["admin"], "Admin (perso)")
    await _backdate(db_session, clone_id, _FROZEN_PAST)  # frozen before the birth
    await _birth_new_permission(db_session, system_roles["admin"].id, "zz.newborn")

    assert "zz.newborn" not in await _clone_keys(db_session, clone_id)  # the gap
    await _boot(db_session)  # the next deployment
    assert "zz.newborn" in await _clone_keys(db_session, clone_id)  # closed


# --- 2. an EXPLICIT agency removal is never overridden (the war test) ---------------


async def test_explicitly_removed_permission_is_never_readded(
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    admin_id = admin.id
    clone_id = await _make_clone(db_session, admin, system_roles["admin"], "Admin (perso)")
    await _backdate(db_session, clone_id, _FROZEN_PAST)
    await _birth_new_permission(db_session, system_roles["admin"].id, "zz.newborn")
    await _boot(db_session)
    assert "zz.newborn" in await _clone_keys(db_session, clone_id)  # received once

    # The agency SEES it and removes it — a decision, stamped by the matrix PUT.
    keep_ids = [
        await _perm_id(db_session, key)
        for key in await _clone_keys(db_session, clone_id)
        if key != "zz.newborn"
    ]
    actor = await _load_actor(db_session, admin_id)  # rebound to the clone
    await RolesManager(db_session).set_role_permissions(actor, clone_id, keep_ids)
    assert "zz.newborn" not in await _clone_keys(db_session, clone_id)

    # Two more boots: the removal STICKS — no seed-vs-agency war, ever.
    await _boot(db_session)
    assert "zz.newborn" not in await _clone_keys(db_session, clone_id)
    await _boot(db_session)
    assert "zz.newborn" not in await _clone_keys(db_session, clone_id)


# --- 2bis. a RENAME is NOT a matrix decision: it never shields a newborn ------------


async def test_rename_after_birth_does_not_block_the_catch_up(
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    """The reason permissions_reviewed_at is a DEDICATED column: renaming the
    clone AFTER a permission's birth (which bumps updated_at) must not read as
    a matrix decision — the permission is still filled at the next boot."""
    admin = await make_agent(role=system_roles["admin"])
    admin_id = admin.id
    clone_id = await _make_clone(db_session, admin, system_roles["admin"], "Admin (perso)")
    await _backdate(db_session, clone_id, _FROZEN_PAST)
    await _birth_new_permission(db_session, system_roles["admin"].id, "zz.newborn")

    # Rename AFTER the birth — updated_at bumps to now, reviewed_at must not.
    actor = await _load_actor(db_session, admin_id)
    await RolesManager(db_session).rename_role(actor, clone_id, "Admin (re-perso)")
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(Role.updated_at, Role.permissions_reviewed_at).where(Role.id == clone_id)
        )
    ).one()
    assert row.updated_at > _FROZEN_PAST  # the rename DID bump updated_at...
    assert row.permissions_reviewed_at == _FROZEN_PAST  # ...but not the decision

    await _boot(db_session)
    assert "zz.newborn" in await _clone_keys(db_session, clone_id)  # still filled


# --- 3. idempotence: the second boot changes nothing --------------------------------


async def test_second_boot_changes_nothing(
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    clone_id = await _make_clone(db_session, admin, system_roles["admin"], "Admin (perso)")
    await _backdate(db_session, clone_id, _FROZEN_PAST)
    await _birth_new_permission(db_session, system_roles["admin"].id, "zz.newborn")

    await _boot(db_session)  # first boot does the catch-up...
    first = await _matrix_snapshot(db_session)
    assert "zz.newborn" in await _clone_keys(db_session, clone_id)  # it DID act
    await _boot(db_session)  # ...the second is a strict no-op
    assert await _matrix_snapshot(db_session) == first


# --- 4. the seed only ADDS: agency grants survive, old removals stay removed --------


async def test_seed_never_removes_anything(
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    clone_id = await _make_clone(db_session, admin, system_roles["viewer"], "Viewer (perso)")

    # An agency GRANT the origin does not carry, and a permission that EXISTED
    # at clone creation, dropped afterwards (born < decision → never re-added).
    extra = await _perm_id(db_session, "case.edit")
    db_session.add(RolePermission(role_id=clone_id, permission_id=extra))
    dropped = await _perm_id(db_session, "case.view")
    await db_session.execute(
        delete(RolePermission).where(
            RolePermission.role_id == clone_id, RolePermission.permission_id == dropped
        )
    )
    await db_session.commit()

    before = await _matrix_snapshot(db_session)
    await _boot(db_session)
    after = await _matrix_snapshot(db_session)
    assert before <= after  # strictly additive — NOTHING was removed, anywhere
    keys = await _clone_keys(db_session, clone_id)
    assert "case.edit" in keys  # the agency grant survives
    assert "case.view" not in keys  # the old removal is respected


# --- 5. the real case: a pre-v0.47 admin clone gets cost.* and SEES the costs -------


async def test_pre_cost_admin_clone_receives_cost_permissions_and_sees_costs(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Alexandre's scenario, replayed: an agency personalized its admin role
    BEFORE the cost permissions were born → the admin no longer sees the costs
    (403, empty planned section). One boot later the clone has caught up, and
    under impersonation the costs section appears."""
    admin = await make_agent(role=system_roles["admin"])
    admin_id = admin.id  # plain ids only across boots (expire_all)
    await db_session.execute(
        update(Agency).where(Agency.id == admin.agency_id).values(currency="EUR")
    )
    await db_session.commit()
    h = agent_headers(admin)

    # Costs exist BEFORE the role was personalized: a planned cost on a journey,
    # instantiated on a case.
    tid = (await client.post("/journeys", headers=h, json={"name": "T"})).json()["id"]
    sid = (await client.post(f"/journeys/{tid}/steps", headers=h, json={"name": "Step"})).json()[
        "id"
    ]
    planned = await client.post(
        f"/journeys/{tid}/steps/{sid}/planned-costs",
        headers=h,
        json={"amount": "120.00", "label": "Timbre"},
    )
    assert planned.status_code == 201, planned.text
    case = await make_client_case(agency_id=admin.agency_id)
    case_id = case.id
    assign = await client.post(
        f"/cases/{case_id}/journey", headers=h, json={"journey_template_id": str(tid)}
    )
    assert assign.status_code == 201, assign.text

    # The agency personalizes its admin role — a clone born BEFORE cost.* (we
    # freeze it in the past and strip the two cost permissions, exactly the
    # state a pre-v0.44 clone would be in).
    clone_id = await _make_clone(db_session, admin, system_roles["admin"], "Admin (perso)")
    cost_ids = [
        await _perm_id(db_session, "cost.view"),
        await _perm_id(db_session, "cost.manage"),
    ]
    await db_session.execute(
        delete(RolePermission).where(
            RolePermission.role_id == clone_id, RolePermission.permission_id.in_(cost_ids)
        )
    )
    await db_session.commit()
    await _backdate(db_session, clone_id, _FROZEN_PAST)

    # Sanity of the simulated state: the admin WEARS the clone, which lacks cost.*.
    db_session.expire_all()
    worn = (
        await db_session.execute(select(Agent.role_id).where(Agent.id == admin_id))
    ).scalar_one()
    assert worn == clone_id, (worn, clone_id)
    assert "cost.view" not in await _clone_keys(db_session, clone_id)

    # BEFORE the boot: Alexandre's bug, reproduced — costs 403, section hidden.
    assert (await client.get(f"/cases/{case_id}/costs", headers=h)).status_code == 403
    detail = (await client.get(f"/journeys/{tid}", headers=h)).json()
    assert detail["steps"][0]["planned_costs"] == []  # gate manager: blind

    await _boot(db_session)  # the deployment that ships the fix

    # AFTER: the clone caught up — the admin sees the costs again...
    assert "cost.view" in await _clone_keys(db_session, clone_id)
    assert "cost.manage" in await _clone_keys(db_session, clone_id)
    costs = await client.get(f"/cases/{case_id}/costs", headers=h)
    assert costs.status_code == 200, costs.text
    assert costs.json()["lines"][0]["label"] == "Timbre"
    detail = (await client.get(f"/journeys/{tid}", headers=h)).json()
    assert len(detail["steps"][0]["planned_costs"]) == 1  # the section appears

    # ...and so does Alexandre UNDER IMPERSONATION (the target's permissions).
    # Reload the role: the boot expired the fixture's ORM objects.
    superadmin_role = (
        await db_session.execute(
            select(Role).where(Role.name == "superadmin", Role.is_system.is_(True))
        )
    ).scalar_one()
    superadmin = await make_agent(role=superadmin_role, email="root@platform.io")
    impersonation = {
        "Authorization": "Bearer "
        + create_access_token(
            str(admin_id), Audience.AGENT, extra_claims={"impersonator_id": str(superadmin.id)}
        )
    }
    seen = await client.get(f"/cases/{case_id}/costs", headers=impersonation)
    assert seen.status_code == 200, seen.text
    assert seen.json()["lines"][0]["label"] == "Timbre"
