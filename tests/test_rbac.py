"""RBAC engine battery — exercised end-to-end over HTTP through a mini
test app carrying the real global `enforce` dependency, plus direct
invocations of the boot-check functions (ASGITransport does not run the
lifespan; the boot check is exercised explicitly, never disabled)."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import ProtectedResource, Role, RolePermission
from src.core.database import get_db
from src.core.enums import Audience
from src.core.exceptions import register_exception_handlers
from src.core.rbac.baseline import (
    EXTERNAL_PERMISSIONS,
    EXTERNAL_ROLE_NAMES,
    PLATFORM_PERMISSIONS,
    RouteBinding,
    collect_bindings,
    seed_bindings,
)
from src.core.rbac.enforcement import enforce
from src.core.rbac.integrity import StartupError, assert_all_routes_bound
from src.core.rbac.permissions import Permission, sync_permissions
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.rbac_plugin import MakeRole

TEST_BINDINGS = [
    RouteBinding("GET", "/t/public", Audience.PUBLIC),
    RouteBinding("GET", "/t/agent", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding("GET", "/t/approve", Audience.AGENT, Permission.REMINDER_APPROVE),
    RouteBinding("GET", "/t/expat", Audience.EXPAT),
]
# /t/unbound is declared on the app but deliberately NOT in TEST_BINDINGS.
UNBOUND_BINDING = RouteBinding("GET", "/t/unbound", Audience.PUBLIC)


@pytest.fixture
def rbac_app() -> FastAPI:
    test_app = FastAPI(dependencies=[Depends(enforce)])
    register_exception_handlers(test_app)

    # GET+HEAD explicit: FastAPI APIRoutes do NOT auto-add HEAD (plain
    # Starlette Routes do) — a GET-only route 405s HEAD before routing.
    # This route exercises enforce's HEAD→GET binding normalization.
    @test_app.api_route("/t/public", methods=["GET", "HEAD"])
    async def public_route() -> dict[str, str]:
        return {"ok": "public"}

    @test_app.get("/t/agent")
    async def agent_route(request: Request) -> dict[str, str]:
        return {"actor_id": str(request.state.actor.id)}

    @test_app.get("/t/approve")
    async def approve_route() -> dict[str, str]:
        return {"ok": "approve"}

    @test_app.get("/t/expat")
    async def expat_route(request: Request) -> dict[str, str]:
        return {"actor_id": str(request.state.actor.id)}

    @test_app.get("/t/unbound")
    async def unbound_route() -> dict[str, str]:
        return {"ok": "unbound"}

    @test_app.get("/ping")
    async def ping() -> str:
        return "pong"

    return test_app


@pytest_asyncio.fixture
async def rbac_client(
    rbac_app: FastAPI, db_session: AsyncSession, rbac_baseline: None
) -> AsyncGenerator[AsyncClient, None]:
    await seed_bindings(db_session, TEST_BINDINGS)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    rbac_app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=rbac_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    rbac_app.dependency_overrides.clear()


# --- Deny by default ---------------------------------------------------------


async def test_unbound_route_returns_403(rbac_client: AsyncClient) -> None:
    response = await rbac_client.get("/t/unbound")
    assert response.status_code == 403


async def test_infra_whitelist_passes_without_binding(rbac_client: AsyncClient) -> None:
    response = await rbac_client.get("/ping")
    assert response.status_code == 200


# --- PUBLIC ------------------------------------------------------------------


async def test_public_binding_passes_without_token(rbac_client: AsyncClient) -> None:
    response = await rbac_client.get("/t/public")
    assert response.status_code == 200


async def test_head_is_normalized_to_get(rbac_client: AsyncClient) -> None:
    response = await rbac_client.head("/t/public")
    assert response.status_code == 200


# --- AGENT audience + matrix ---------------------------------------------------


async def test_agent_with_permission_passes(
    rbac_client: AsyncClient,
    make_agent: MakeAgent,
    make_role: MakeRole,
    agent_headers: AuthHeaders,
) -> None:
    role = await make_role(permissions=[Permission.CASE_VIEW])
    agent = await make_agent(role=role)
    response = await rbac_client.get("/t/agent", headers=agent_headers(agent))
    assert response.status_code == 200
    assert response.json() == {"actor_id": str(agent.id)}


async def test_agent_without_permission_403(
    rbac_client: AsyncClient,
    make_agent: MakeAgent,
    make_role: MakeRole,
    agent_headers: AuthHeaders,
) -> None:
    role = await make_role(permissions=[Permission.REMINDER_CREATE])
    agent = await make_agent(role=role)
    response = await rbac_client.get("/t/agent", headers=agent_headers(agent))
    assert response.status_code == 403


async def test_agent_missing_token_401(rbac_client: AsyncClient) -> None:
    response = await rbac_client.get("/t/agent")
    assert response.status_code == 401


async def test_single_role_permissions_are_exact(
    rbac_client: AsyncClient,
    make_agent: MakeAgent,
    make_role: MakeRole,
    agent_headers: AuthHeaders,
) -> None:
    """Single-role model: an agent's permissions are EXACTLY their
    role's matrix — nothing accumulates from anywhere else."""
    role_view = await make_role(permissions=[Permission.CASE_VIEW])
    role_both = await make_role(permissions=[Permission.CASE_VIEW, Permission.REMINDER_APPROVE])
    viewer = await make_agent(role=role_view)
    approver = await make_agent(role=role_both)

    assert (await rbac_client.get("/t/agent", headers=agent_headers(approver))).status_code == 200
    assert (await rbac_client.get("/t/approve", headers=agent_headers(approver))).status_code == 200
    assert (await rbac_client.get("/t/agent", headers=agent_headers(viewer))).status_code == 200
    assert (await rbac_client.get("/t/approve", headers=agent_headers(viewer))).status_code == 403


async def test_member_system_role_can_approve(
    rbac_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    # The Eloïse scenario: a plain member approves reminders.
    member = await make_agent(role=system_roles["member"])
    response = await rbac_client.get("/t/approve", headers=agent_headers(member))
    assert response.status_code == 200


# --- EXPAT audience (no matrix) ------------------------------------------------


async def test_expat_binding_with_expat_token_passes(
    rbac_client: AsyncClient,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user()
    response = await rbac_client.get("/t/expat", headers=expat_headers(expat))
    assert response.status_code == 200
    assert response.json() == {"actor_id": str(expat.id)}


# --- Cross-audience seals ------------------------------------------------------


async def test_expat_binding_rejects_agent_token(
    rbac_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    agent = await make_agent()
    response = await rbac_client.get("/t/expat", headers=agent_headers(agent))
    assert response.status_code == 401


async def test_agent_binding_rejects_expat_token(
    rbac_client: AsyncClient,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user()
    response = await rbac_client.get("/t/agent", headers=expat_headers(expat))
    assert response.status_code == 401


# --- System roles matrix --------------------------------------------------------


async def test_system_role_matrix_seeded_as_specified(
    system_roles: dict[str, Role], db_session: AsyncSession
) -> None:
    async def keys_of(role: Role) -> set[str]:
        stmt = (
            select(PermissionRow.key)
            .join(RolePermission, RolePermission.permission_id == PermissionRow.id)
            .where(RolePermission.role_id == role.id)
        )
        return set((await db_session.execute(stmt)).scalars())

    # The internal system roles (external system roles also exist now).
    assert {"admin", "case_manager", "member", "viewer", "superadmin"} <= set(system_roles)
    # admin = everything EXCEPT the external.* permissions (those belong
    # only to external roles — structural barrier, wave B) AND the
    # platform-scope permissions (those belong only to superadmin).
    external_keys = {p.value for p in EXTERNAL_PERMISSIONS}
    platform_keys = {p.value for p in PLATFORM_PERMISSIONS}
    admin = await keys_of(system_roles["admin"])
    assert admin == {p.value for p in Permission} - external_keys - platform_keys
    assert Permission.AGENCY_CREATE.value not in admin  # platform-only, never an agency admin
    case_manager = await keys_of(system_roles["case_manager"])
    assert Permission.AGENT_MANAGE.value not in case_manager
    assert Permission.ROLE_MANAGE.value not in case_manager
    assert Permission.NOTE_VIEW_CONFIDENTIAL.value not in case_manager
    assert Permission.AGENCY_CREATE.value not in case_manager  # platform-only
    assert external_keys.isdisjoint(case_manager)  # no external.* on an internal role
    member = await keys_of(system_roles["member"])
    assert Permission.REMINDER_APPROVE.value in member
    assert Permission.NOTE_VIEW_CONFIDENTIAL.value not in member
    assert await keys_of(system_roles["viewer"]) == {Permission.CASE_VIEW.value}
    # superadmin = the PLATFORM-OWNER role: EVERY internal permission PLUS
    # agency.create — i.e. all permissions except the external.* ones. It is
    # platform-reserved (not listable/assignable by agencies); still no
    # cross-agency access (enforce/repositories untouched — Phase 2).
    superadmin = await keys_of(system_roles["superadmin"])
    assert superadmin == {p.value for p in Permission} - external_keys
    assert Permission.AGENCY_CREATE.value in superadmin
    # The 6 external system roles hold EXACTLY the 3 external.* permissions
    # (wave B: permission ∧ scoping — every external route is assignment-scoped).
    for name in EXTERNAL_ROLE_NAMES:
        assert name in system_roles
        assert await keys_of(system_roles[name]) == external_keys


# --- Catalogue sync --------------------------------------------------------------


async def test_sync_permissions_idempotent_and_never_deletes(
    db_session: AsyncSession,
) -> None:
    await sync_permissions(db_session)
    first = set((await db_session.execute(select(PermissionRow.key))).scalars())
    assert first == {p.value for p in Permission}

    # A key unknown to the catalogue (e.g. seeded by a newer deploy)
    # must survive a re-sync.
    db_session.add(PermissionRow(key="custom.extra", label="Custom", category="custom"))
    await db_session.commit()
    await sync_permissions(db_session)
    after = set((await db_session.execute(select(PermissionRow.key))).scalars())
    assert after == first | {"custom.extra"}


# --- Boot check -------------------------------------------------------------------


async def test_boot_check_passes_when_all_routes_bound(
    rbac_app: FastAPI, db_session: AsyncSession, rbac_baseline: None
) -> None:
    await seed_bindings(db_session, [*TEST_BINDINGS, UNBOUND_BINDING])
    await assert_all_routes_bound(rbac_app, db_session)  # must not raise


async def test_real_app_every_route_is_bound(db_session: AsyncSession, rbac_baseline: None) -> None:
    """THE missing safety net: the boot check never runs against the
    REAL app in the suite (ASGITransport skips the lifespan), so a route
    shipped without a binding stays invisible to `make check` and only
    blows up at deploy. This runs the genuine boot-check function against
    the FULL real route table + the code-declared bindings (rbac_baseline
    seeds collect_bindings, exactly what the boot reconcile does). A new
    route added without a RouteBinding fails HERE, at commit time."""
    from src.main import app

    # Sanity: the fixture really seeds the code's full binding set.
    assert len(collect_bindings()) > 0
    await assert_all_routes_bound(app, db_session)  # must not raise


async def test_boot_check_fails_on_missing_binding(
    rbac_app: FastAPI, db_session: AsyncSession, rbac_baseline: None
) -> None:
    await seed_bindings(db_session, [*TEST_BINDINGS, UNBOUND_BINDING])
    await db_session.execute(delete(ProtectedResource).where(ProtectedResource.route == "/t/agent"))
    await db_session.commit()
    with pytest.raises(StartupError, match="/t/agent"):
        await assert_all_routes_bound(rbac_app, db_session)


async def test_boot_check_ignores_infra_whitelist(
    db_session: AsyncSession,
) -> None:
    infra_only = FastAPI()

    @infra_only.get("/ping")
    async def ping() -> str:
        return "pong"

    @infra_only.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Zero bindings in DB: only the whitelist keeps this from raising.
    await assert_all_routes_bound(infra_only, db_session)
