"""The Email keys are granted by the platform only (decision of Alexandre,
08/10/2026, replacing the spec's N3).

- Nobody holds `email.*` by default, the agency admin included: an admin is
  refused every /email/* route (403 `permission.denied`) until a superadmin
  grants the keys to its role, and again once they are withdrawn.
- The platform's routes (`agency.create`, superadmin only): GET
  /agencies/{agency_id}/email-module/roles lists the roles the agency hands
  out, as the agency sees them, with their active wearers and their Email
  keys; PUT .../roles/{role_id} sets exactly the given keys. A shared system
  role is never written: the agency's copy-on-write clone carries the grant
  (made if needed, its wearers moved onto it), another agency is untouched.
  The platform role, an external role and another agency's role answer like
  an unknown role. Allowed while the module is off.
- No agency door adds or removes an Email key, whoever acts (an operator in
  its home agency included): creating a role with one, or editing a matrix
  to add or remove one, is refused (`role.platform_managed`, params `keys`);
  a duplicate leaves them behind; a copy-on-write clone keeps the platform's
  grant and starts without one otherwise. Assignment and invitation hand out
  a role whole, within the standard ceiling, Email keys included.
- The boot never hands an Email key to a clone, the seed never gives them
  back to the admin, and a platform grant survives the boot.
- A superadmin who "enters" an agency acts with that agency's admin's rights:
  no Email key until the platform granted them to the admin role.

Every request of a test shares ONE session, where production opens one per
request: `_request_ended` rolls back what a refused request had flushed.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.invitation import AgentInvitation
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role, RolePermission
from src.core.rbac.baseline import (
    EXTERNAL_PERMISSIONS,
    PLATFORM_PERMISSIONS,
    SYSTEM_ROLE_MATRIX,
    collect_bindings,
    seed_rbac_baseline,
)
from src.core.rbac.permissions import Permission
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.email_plugin import (
    GrantEmail,
    MakeAgencyEmailModule,
    MakeEmailAdmin,
    email_env,
)
from tests.plugins.rbac_plugin import MakeRole

pytestmark = pytest.mark.usefixtures("rbac_baseline")

# Written out, sorted: the test does not lean on the constant it guards.
EMAIL_KEYS = [
    "email.enroll",
    "email.manage",
    "email.pause",
    "email.remove",
    "email.sync",
    "email.view",
]
PLATFORM_KEYS = {p.value for p in PLATFORM_PERMISSIONS}
ROLE_NOT_FOUND = {"detail": "Role not found.", "code": "role.not_found", "params": {}}


def _matrix(name: str) -> set[str]:
    return {p.value for p in SYSTEM_ROLE_MATRIX[name]}


def _refused_by_the_platform(*keys: str) -> dict[str, Any]:
    return {
        "detail": f"Only the platform grants or withdraws: {', '.join(keys)}.",
        "code": "role.platform_managed",
        "params": {"keys": list(keys)},
    }


@dataclass
class Office:
    """An agency with the Email module on, an admin (the system admin role)
    and a case manager, neither holding an Email key. Plain values only."""

    agency_id: uuid.UUID
    admin_id: uuid.UUID
    case_manager_id: uuid.UUID
    admin: dict[str, str]


MakeOffice = Callable[..., Awaitable[Office]]


@pytest_asyncio.fixture
async def role_ids(system_roles: dict[str, Role]) -> dict[str, uuid.UUID]:
    """The system roles' ids, read once: a test may expire the session."""
    return {name: role.id for name, role in system_roles.items()}


@pytest_asyncio.fixture
async def perm_ids(rbac_baseline: None, db_session: AsyncSession) -> dict[str, str]:
    rows = (await db_session.execute(select(PermissionRow))).scalars().all()
    return {row.key: str(row.id) for row in rows}


@pytest_asyncio.fixture
async def operator(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """A platform operator (the system superadmin) in its home agency."""
    return await make_agent(role=system_roles["superadmin"])


@pytest_asyncio.fixture
async def root(operator: Agent, agent_headers: AuthHeaders) -> dict[str, str]:
    return agent_headers(operator)


@pytest_asyncio.fixture
async def make_office(
    email_module_on: None,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    make_agency_email_module: MakeAgencyEmailModule,
) -> MakeOffice:
    async def _make(name: str = "Relocation Lisboa") -> Office:
        # A paying agency: its invitations are never capped by the trial.
        agency = await make_agency(name=name, plan="agence", converted_at=datetime.now(UTC))
        admin = await make_agent(agency_id=agency.id, role=system_roles["admin"])
        case_manager = await make_agent(agency_id=agency.id, role=system_roles["case_manager"])
        await make_agency_email_module(agency_id=agency.id)
        return Office(agency.id, admin.id, case_manager.id, agent_headers(admin))

    return _make


def _roles_url(agency_id: uuid.UUID) -> str:
    return f"/agencies/{agency_id}/email-module/roles"


async def _grant(
    client: AsyncClient,
    headers: dict[str, str],
    agency_id: uuid.UUID,
    role_id: uuid.UUID,
    keys: list[str],
) -> Response:
    return await client.put(
        f"{_roles_url(agency_id)}/{role_id}", headers=headers, json={"permissions": keys}
    )


def _row(listing: Response, name: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = listing.json()
    return next(row for row in rows if row["name"] == name)


async def _keys_of(db: AsyncSession, role_id: uuid.UUID) -> set[str]:
    stmt = (
        select(PermissionRow.key)
        .join(RolePermission, RolePermission.permission_id == PermissionRow.id)
        .where(RolePermission.role_id == role_id)
    )
    return set((await db.execute(stmt)).scalars())


async def _role_of(db: AsyncSession, agent_id: uuid.UUID) -> uuid.UUID:
    return (await db.execute(select(Agent.role_id).where(Agent.id == agent_id))).scalar_one()


async def _email_keys_of(client: AsyncClient, headers: dict[str, str]) -> list[str]:
    me = await client.get("/auth/agent/me", headers=headers)
    assert me.status_code == 200, me.text
    return sorted(k for k in me.json()["effective_permissions"] if k.startswith("email."))


async def _request_ended(db: AsyncSession) -> None:
    """A refused request's own session ends without a commit (get_db): what
    it had flushed (a copy-on-write clone) never lands."""
    await db.rollback()


# --- nobody holds them until the platform grants them -------------------------------


async def test_an_agency_admin_reaches_the_module_only_once_the_platform_grants_it(
    client: AsyncClient,
    make_office: MakeOffice,
    root: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    office = await make_office()
    assert await _email_keys_of(client, office.admin) == []
    # The module is on for the agency, yet its admin is refused.
    for method, path, permission in (
        ("GET", "/email/meta", "email.view"),
        ("GET", "/email/campaigns", "email.view"),
        ("POST", "/email/campaigns", "email.manage"),
    ):
        body = {"name": "Spring"} if method == "POST" else None
        response = await client.request(method, path, headers=office.admin, json=body)
        assert response.status_code == 403, (path, response.text)
        assert response.json() == {
            "detail": "Missing permission.",
            "code": "permission.denied",
            "params": {"permission": permission},
        }

    granted = await _grant(client, root, office.agency_id, role_ids["admin"], EMAIL_KEYS)
    assert granted.status_code == 200, granted.text
    assert await _email_keys_of(client, office.admin) == EMAIL_KEYS
    meta = await client.get("/email/meta", headers=office.admin)
    assert meta.status_code == 200, meta.text
    created = await client.post("/email/campaigns", headers=office.admin, json={"name": "Spring"})
    assert created.status_code == 201, created.text

    withdrawn = await _grant(client, root, office.agency_id, role_ids["admin"], [])
    assert withdrawn.status_code == 200, withdrawn.text
    assert await _email_keys_of(client, office.admin) == []
    refused = await client.get("/email/meta", headers=office.admin)
    assert refused.status_code == 403
    assert refused.json()["params"] == {"permission": "email.view"}


async def test_the_grant_is_allowed_while_the_module_is_off(
    client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    root: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    """Rights are prepared before the switch: they open nothing until then."""
    agency = await make_agency()
    headers = agent_headers(await make_agent(agency_id=agency.id, role=system_roles["admin"]))
    granted = await _grant(client, root, agency.id, role_ids["admin"], EMAIL_KEYS)
    assert granted.status_code == 200, granted.text
    assert _row(granted, "admin")["email_permissions"] == EMAIL_KEYS
    with email_env(EMAIL_MODULE_ENABLED="true"):
        meta = await client.get("/email/meta", headers=headers)
    assert meta.status_code == 409, meta.text
    assert meta.json()["code"] == "email.module_disabled"


# --- the platform's routes ---------------------------------------------------------


async def test_the_list_shows_the_roles_the_agency_hands_out(
    client: AsyncClient,
    make_office: MakeOffice,
    make_agency: MakeAgency,
    make_role: MakeRole,
    root: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    """Exactly the agency's own role list: its internal system roles (the
    platform role and the external ones left out) and its own roles, never
    another agency's; each with its wearers in THIS agency."""
    office = await make_office()
    own = await make_role(permissions=[Permission.CASE_VIEW], agency_id=office.agency_id)
    await make_role(permissions=[Permission.CASE_VIEW], agency_id=(await make_agency()).id)
    listing = await client.get(_roles_url(office.agency_id), headers=root)
    assert listing.status_code == 200, listing.text
    rows = {row["id"]: row for row in listing.json()}
    internal = ("admin", "case_manager", "member", "viewer")
    assert set(rows) == {str(role_ids[name]) for name in internal} | {str(own.id)}
    assert rows[str(role_ids["admin"])] == {
        "id": str(role_ids["admin"]),
        "name": "admin",
        "is_system": True,
        "cloned_from_role_id": None,
        "active_agents": 1,
        "email_permissions": [],
    }
    assert rows[str(role_ids["case_manager"])]["active_agents"] == 1
    assert rows[str(role_ids["member"])]["active_agents"] == 0
    assert rows[str(own.id)]["is_system"] is False
    assert all(row["email_permissions"] == [] for row in rows.values())
    agency_view = await client.get("/agencies/me/roles", headers=office.admin)
    assert [row["id"] for row in listing.json()] == [row["id"] for row in agency_view.json()]


async def test_a_grant_on_a_system_role_lands_on_this_agencys_clone_only(
    client: AsyncClient,
    db_session: AsyncSession,
    make_office: MakeOffice,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    root: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    office = await make_office()
    other = await make_office("Expat Porto")
    # A deactivated admin of the agency: moved onto the clone, not counted.
    gone = await make_agent(
        agency_id=office.agency_id, role=system_roles["admin"], deactivated_at=datetime.now(UTC)
    )
    gone_id = gone.id

    granted = await _grant(
        client, root, office.agency_id, role_ids["admin"], ["email.view", "email.manage"]
    )
    assert granted.status_code == 200, granted.text
    admin_row = _row(granted, "admin")
    clone_id = uuid.UUID(admin_row["id"])
    assert clone_id != role_ids["admin"]
    assert admin_row == {
        "id": str(clone_id),
        "name": "admin",
        "is_system": False,
        "cloned_from_role_id": str(role_ids["admin"]),
        "active_agents": 1,
        "email_permissions": ["email.manage", "email.view"],
    }
    # The clone masks the system role in the agency's list.
    assert str(role_ids["admin"]) not in {row["id"] for row in granted.json()}
    # The clone: the system admin's matrix plus the grant, nothing else.
    assert await _keys_of(db_session, clone_id) == _matrix("admin") | {"email.view", "email.manage"}
    # The agency's wearers of the system role were moved onto it.
    assert await _role_of(db_session, office.admin_id) == clone_id
    assert await _role_of(db_session, gone_id) == clone_id
    assert await _role_of(db_session, office.case_manager_id) == role_ids["case_manager"]

    # The shared system role is untouched, and so is the other agency.
    assert await _keys_of(db_session, role_ids["admin"]) == _matrix("admin")
    assert await _role_of(db_session, other.admin_id) == role_ids["admin"]
    seen = await client.get(_roles_url(other.agency_id), headers=root)
    assert _row(seen, "admin")["id"] == str(role_ids["admin"])
    assert _row(seen, "admin")["email_permissions"] == []
    assert await _email_keys_of(client, office.admin) == ["email.manage", "email.view"]
    assert await _email_keys_of(client, other.admin) == []

    # Granting again reuses the clone.
    again = await _grant(client, root, office.agency_id, role_ids["admin"], EMAIL_KEYS)
    assert again.status_code == 200, again.text
    assert _row(again, "admin")["id"] == str(clone_id)
    assert _row(again, "admin")["email_permissions"] == EMAIL_KEYS
    clones = await db_session.execute(
        select(Role.id).where(
            Role.agency_id == office.agency_id, Role.cloned_from_role_id == role_ids["admin"]
        )
    )
    assert clones.scalars().all() == [clone_id]


async def test_the_grant_is_exact_and_touches_nothing_else(
    client: AsyncClient,
    db_session: AsyncSession,
    make_office: MakeOffice,
    make_role: MakeRole,
    root: dict[str, str],
) -> None:
    """On one of the agency's own roles, written in place: exactly the keys
    sent (a repeated one counts once, none withdraws them all), its other
    permissions untouched."""
    office = await make_office()
    own = await make_role(
        permissions=[Permission.CASE_VIEW, Permission.DOCUMENT_VALIDATE],
        agency_id=office.agency_id,
    )
    own_id = own.id
    for keys in (EMAIL_KEYS, ["email.view", "email.sync", "email.view"], []):
        response = await _grant(client, root, office.agency_id, own_id, keys)
        assert response.status_code == 200, response.text
        row = next(r for r in response.json() if r["id"] == str(own_id))
        assert row["email_permissions"] == sorted(set(keys))
        assert await _keys_of(db_session, own_id) == {"case.view", "document.validate", *keys}


async def test_only_email_keys_are_granted_there(
    client: AsyncClient,
    db_session: AsyncSession,
    make_office: MakeOffice,
    root: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    office = await make_office()
    url = f"{_roles_url(office.agency_id)}/{role_ids['admin']}"
    for body in (
        {"permissions": ["case.edit"]},
        {"permissions": ["email.view", "agency.create"]},
        {"permissions": ["email.unknown"]},
        {},
    ):
        response = await client.put(url, headers=root, json=body)
        assert response.status_code == 422, (body, response.text)
    clones = await db_session.execute(select(Role.id).where(Role.agency_id == office.agency_id))
    assert clones.scalars().all() == []


async def test_the_platform_role_an_external_role_and_another_agencys_are_unknown(
    client: AsyncClient,
    db_session: AsyncSession,
    make_office: MakeOffice,
    make_agency: MakeAgency,
    make_role: MakeRole,
    root: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    """They answer exactly like a random id, and nothing moves."""
    office = await make_office()
    elsewhere = (await make_agency()).id
    foreign = await make_role(permissions=[Permission.CASE_VIEW], agency_id=elsewhere)
    foreign_clone = await make_role(
        permissions=[Permission.CASE_VIEW],
        agency_id=elsewhere,
        name="member",
        cloned_from_role_id=role_ids["member"],
    )
    targets = (
        uuid.uuid4(),
        role_ids["superadmin"],
        role_ids["external_lawyer"],
        foreign.id,
        foreign_clone.id,
    )
    for role_id in targets:
        response = await _grant(client, root, office.agency_id, role_id, EMAIL_KEYS)
        assert response.status_code == 404, (role_id, response.text)
        assert response.json() == ROLE_NOT_FOUND
    made = await db_session.execute(select(Role.id).where(Role.agency_id == office.agency_id))
    assert made.scalars().all() == []
    external = {p.value for p in EXTERNAL_PERMISSIONS}
    assert await _keys_of(db_session, role_ids["external_lawyer"]) == external
    assert await _keys_of(db_session, foreign.id) == {"case.view"}
    assert await _keys_of(db_session, foreign_clone.id) == {"case.view"}


async def test_an_unknown_agency_is_a_404(
    client: AsyncClient, root: dict[str, str], role_ids: dict[str, uuid.UUID]
) -> None:
    ghost = uuid.uuid4()
    for method, url, body in (
        ("GET", _roles_url(ghost), None),
        ("PUT", f"{_roles_url(ghost)}/{role_ids['admin']}", {"permissions": []}),
    ):
        response = await client.request(method, url, headers=root, json=body)
        assert response.status_code == 404, (method, response.text)
        assert response.json()["code"] == "agency.not_found"


async def test_a_custom_role_bearing_the_system_name_blocks_the_clone(
    client: AsyncClient,
    make_office: MakeOffice,
    make_role: MakeRole,
    root: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    office = await make_office()
    await make_role(permissions=[], agency_id=office.agency_id, name="member")
    response = await _grant(client, root, office.agency_id, role_ids["member"], EMAIL_KEYS)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "role.clone_name_taken"


@pytest.mark.parametrize("role_name", ["admin", "case_manager", "member", "viewer"])
async def test_every_agency_role_is_refused_the_platform_routes(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    role_ids: dict[str, uuid.UUID],
    role_name: str,
) -> None:
    """Even on its own agency: the routes are the platform's."""
    agent = await make_agent(role=system_roles[role_name])
    agency_id = agent.agency_id
    headers = agent_headers(agent)
    for method, url, body in (
        ("GET", _roles_url(agency_id), None),
        ("PUT", f"{_roles_url(agency_id)}/{role_ids[role_name]}", {"permissions": EMAIL_KEYS}),
    ):
        response = await client.request(method, url, headers=headers, json=body)
        assert response.status_code == 403, (role_name, method, response.text)
        assert response.json()["params"] == {"permission": "agency.create"}
    made = await db_session.execute(select(Role.id).where(Role.agency_id == agency_id))
    assert made.scalars().all() == []


async def test_an_admin_granted_the_email_keys_still_cannot_grant_them(
    client: AsyncClient,
    make_email_admin: MakeEmailAdmin,
    agent_headers: AuthHeaders,
    role_ids: dict[str, uuid.UUID],
) -> None:
    admin = await make_email_admin()
    headers = agent_headers(admin)
    for role_name in ("admin", "case_manager"):
        url = f"{_roles_url(admin.agency_id)}/{role_ids[role_name]}"
        response = await client.put(url, headers=headers, json={"permissions": EMAIL_KEYS})
        assert response.status_code == 403, response.text
        assert response.json()["params"] == {"permission": "agency.create"}


# --- the agency doors ----------------------------------------------------------------


@pytest_asyncio.fixture(params=["admin", "granted_admin", "operator"])
async def actor(
    request: pytest.FixtureRequest,
    make_agent: MakeAgent,
    make_email_admin: MakeEmailAdmin,
    system_roles: dict[str, Role],
) -> Agent:
    """Whoever acts through an agency flow: an admin without the Email keys,
    an admin the platform granted them, an operator in its home agency
    (who holds them)."""
    if request.param == "granted_admin":
        return await make_email_admin()
    role = system_roles["superadmin" if request.param == "operator" else "admin"]
    return await make_agent(role=role)


async def test_no_agency_door_adds_an_email_key(
    client: AsyncClient,
    db_session: AsyncSession,
    actor: Agent,
    make_role: MakeRole,
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    """Not a new role, not a matrix edit, whoever acts: one answer, before
    the ceiling."""
    agency_id, headers = actor.agency_id, agent_headers(actor)
    own = await make_role(permissions=[Permission.CASE_VIEW], agency_id=agency_id)
    asked = [perm_ids["case.view"], perm_ids["email.view"]]
    create = await client.post(
        "/agencies/me/roles",
        headers=headers,
        json={"name": "Prospection", "permission_ids": asked},
    )
    edit = await client.put(
        f"/agencies/me/roles/{own.id}/permissions",
        headers=headers,
        json={"permission_ids": asked},
    )
    for response in (create, edit):
        assert response.status_code == 403, response.text
        assert response.json() == _refused_by_the_platform("email.view")
    assert await _keys_of(db_session, own.id) == {"case.view"}
    named = await db_session.execute(
        select(Role.id).where(Role.agency_id == agency_id, Role.name == "Prospection")
    )
    assert named.scalars().all() == []


async def test_no_agency_door_withdraws_an_email_key(
    client: AsyncClient,
    db_session: AsyncSession,
    actor: Agent,
    make_role: MakeRole,
    grant_email: GrantEmail,
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    """A matrix edit leaves the platform's grant as it is. Kept as it is, the
    rest of the matrix stays the agency's: the keys kept are not the actor's
    to hand out, so an admin without them edits the role all the same."""
    agency_id, headers = actor.agency_id, agent_headers(actor)
    own = await make_role(permissions=[Permission.CASE_VIEW], agency_id=agency_id)
    own_id = own.id
    await grant_email(agency_id=agency_id, role_id=own_id, keys=["email.view", "email.sync"])
    url = f"/agencies/me/roles/{own_id}/permissions"

    def ids(*keys: str) -> list[str]:
        return [perm_ids[key] for key in keys]

    for asked, changed in (
        (ids("case.view", "email.view"), ("email.sync",)),
        (ids("case.view", "email.view", "email.manage"), ("email.manage", "email.sync")),
        (ids("case.view"), ("email.sync", "email.view")),
    ):
        response = await client.put(url, headers=headers, json={"permission_ids": asked})
        assert response.status_code == 403, response.text
        assert response.json() == _refused_by_the_platform(*changed)
    assert await _keys_of(db_session, own_id) == {"case.view", "email.view", "email.sync"}

    kept = await client.put(
        url,
        headers=headers,
        json={"permission_ids": ids("case.view", "case.edit", "email.view", "email.sync")},
    )
    assert kept.status_code == 200, kept.text
    keys = {"case.view", "case.edit", "email.view", "email.sync"}
    assert {p["key"] for p in kept.json()["permissions"]} == keys
    assert await _keys_of(db_session, own_id) == keys


async def test_a_duplicate_leaves_the_email_keys_behind(
    client: AsyncClient,
    db_session: AsyncSession,
    actor: Agent,
    make_role: MakeRole,
    grant_email: GrantEmail,
    agent_headers: AuthHeaders,
) -> None:
    """The platform granted them to the source, not to a new role: the copy
    carries the rest, within the actor's ceiling, and the source keeps its
    grant."""
    agency_id, headers = actor.agency_id, agent_headers(actor)
    source = await make_role(permissions=[Permission.CASE_VIEW], agency_id=agency_id)
    source_id = source.id
    await grant_email(agency_id=agency_id, role_id=source_id)
    duplicate = await client.post(
        f"/agencies/me/roles/{source_id}/duplicate", headers=headers, json={"name": "Copie"}
    )
    assert duplicate.status_code == 201, duplicate.text
    assert {p["key"] for p in duplicate.json()["permissions"]} == {"case.view"}
    assert await _keys_of(db_session, uuid.UUID(duplicate.json()["id"])) == {"case.view"}
    assert await _keys_of(db_session, source_id) == {"case.view", *EMAIL_KEYS}


async def test_a_first_edit_of_a_system_role_adds_no_email_key(
    client: AsyncClient,
    db_session: AsyncSession,
    make_office: MakeOffice,
    perm_ids: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    """The copy-on-write clone it would make starts without them: asking for
    one is adding it."""
    office = await make_office()
    member = [perm_ids[key] for key in sorted(_matrix("member"))]
    response = await client.put(
        f"/agencies/me/roles/{role_ids['member']}/permissions",
        headers=office.admin,
        json={"permission_ids": [*member, perm_ids["email.view"]]},
    )
    assert response.status_code == 403, response.text
    assert response.json() == _refused_by_the_platform("email.view")
    await _request_ended(db_session)
    made = await db_session.execute(select(Role.id).where(Role.agency_id == office.agency_id))
    assert made.scalars().all() == []


async def test_the_agencys_clone_keeps_the_platform_grant(
    client: AsyncClient,
    db_session: AsyncSession,
    make_office: MakeOffice,
    root: dict[str, str],
    perm_ids: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    """Once the platform granted keys to a system role, the agency's clone
    carries them: renaming the system role lands on that clone, and the
    agency edits its matrix around the grant."""
    office = await make_office()
    granted = await _grant(
        client, root, office.agency_id, role_ids["case_manager"], ["email.enroll", "email.view"]
    )
    assert granted.status_code == 200, granted.text
    clone_id = _row(granted, "case_manager")["id"]
    rename = await client.patch(
        f"/agencies/me/roles/{role_ids['case_manager']}",
        headers=office.admin,
        json={"name": "Chargé de dossier"},
    )
    assert rename.status_code == 200, rename.text
    assert rename.json()["id"] == clone_id
    assert {"email.enroll", "email.view"} <= {p["key"] for p in rename.json()["permissions"]}

    keys = (_matrix("case_manager") - {"cost.manage"}) | {"email.enroll", "email.view"}
    edit = await client.put(
        f"/agencies/me/roles/{clone_id}/permissions",
        headers=office.admin,
        json={"permission_ids": [perm_ids[key] for key in sorted(keys)]},
    )
    assert edit.status_code == 200, edit.text
    assert await _keys_of(db_session, uuid.UUID(clone_id)) == keys


async def test_a_fresh_clone_starts_without_an_email_key(
    client: AsyncClient,
    db_session: AsyncSession,
    make_office: MakeOffice,
    perm_ids: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    """Even from a system role carrying one (a stray row, or a database the
    cleanup migration has not reached yet): the platform granted nothing to
    this agency for that role."""
    office = await make_office()
    db_session.add(
        RolePermission(role_id=role_ids["member"], permission_id=uuid.UUID(perm_ids["email.view"]))
    )
    await db_session.commit()
    rename = await client.patch(
        f"/agencies/me/roles/{role_ids['member']}",
        headers=office.admin,
        json={"name": "Collaborateur"},
    )
    assert rename.status_code == 200, rename.text
    assert rename.json()["cloned_from_role_id"] == str(role_ids["member"])
    assert {p["key"] for p in rename.json()["permissions"]} == _matrix("member")


async def test_assignment_and_invitation_hand_out_a_role_within_the_ceiling(
    client: AsyncClient,
    db_session: AsyncSession,
    make_office: MakeOffice,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    root: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    """The platform decides which roles carry the Email keys; the agency
    decides who wears which role, within its ceiling, those keys included."""
    office = await make_office()
    colleague = await make_agent(agency_id=office.agency_id, role=system_roles["member"])
    colleague_id = colleague.id
    granted = await _grant(client, root, office.agency_id, role_ids["case_manager"], EMAIL_KEYS)
    assert granted.status_code == 200, granted.text
    carrier = _row(granted, "case_manager")["id"]

    async def assign() -> Response:
        return await client.put(
            f"/agencies/me/members/{colleague_id}/role",
            headers=office.admin,
            json={"role_id": carrier},
        )

    async def invite() -> Response:
        return await client.post(
            "/agencies/me/invitations",
            headers=office.admin,
            json={"email": f"new-{uuid.uuid4().hex[:8]}@example.com", "role_id": carrier},
        )

    # The admin holds no Email key: it hands out no role carrying one.
    for response in (await assign(), await invite()):
        assert response.status_code == 403, response.text
        assert response.json() == {
            "detail": f"Beyond your permission ceiling: {', '.join(EMAIL_KEYS)}.",
            "code": "role.beyond_ceiling",
            "params": {},
        }
    assert await _role_of(db_session, colleague_id) == role_ids["member"]
    invitations = await db_session.execute(select(AgentInvitation.id))
    assert invitations.scalars().all() == []

    # Once the platform granted them to the admin role too, it does.
    assert (await _grant(client, root, office.agency_id, role_ids["admin"], EMAIL_KEYS)).is_success
    assigned = await assign()
    assert assigned.status_code == 200, assigned.text
    assert await _role_of(db_session, colleague_id) == uuid.UUID(carrier)
    invited = await invite()
    assert invited.status_code == 201, invited.text


# --- the boot, the impersonation -------------------------------------------------------


async def test_the_boot_never_hands_an_email_key_out_and_keeps_the_platforms(
    db_session: AsyncSession,
    make_office: MakeOffice,
    make_role: MakeRole,
    grant_email: GrantEmail,
    role_ids: dict[str, uuid.UUID],
) -> None:
    """A clone of superadmin (the hole's clone, after the cleanups) whose last
    matrix decision predates every permission: its origin holds the Email
    keys, the boot hands it none, nor a platform key, yet fills case.view
    (the birth-date rule still runs). A platform grant survives the boot,
    and the seed never gives the keys back to the system admin."""
    office = await make_office()
    operators = await make_role(
        permissions=[Permission.CASE_EDIT],
        agency_id=office.agency_id,
        name="Opérateurs",
        cloned_from_role_id=role_ids["superadmin"],
    )
    operators_id = operators.id
    granted_id = await grant_email(agency_id=office.agency_id)
    await db_session.execute(
        update(Role)
        .where(Role.id.in_([operators_id, granted_id]))
        .values(permissions_reviewed_at=datetime(2020, 1, 1, tzinfo=UTC))
    )
    await db_session.commit()

    await seed_rbac_baseline(db_session, bindings=collect_bindings())  # the next boot
    db_session.expire_all()
    keys = await _keys_of(db_session, operators_id)
    assert keys.isdisjoint(EMAIL_KEYS)
    assert keys.isdisjoint(PLATFORM_KEYS)
    assert "case.view" in keys
    assert await _keys_of(db_session, granted_id) >= set(EMAIL_KEYS)
    assert (await _keys_of(db_session, role_ids["admin"])).isdisjoint(EMAIL_KEYS)
    assert await _keys_of(db_session, role_ids["superadmin"]) >= set(EMAIL_KEYS)


async def test_entering_an_agency_gives_its_admins_email_keys_and_no_more(
    client: AsyncClient,
    make_office: MakeOffice,
    root: dict[str, str],
    role_ids: dict[str, uuid.UUID],
) -> None:
    """The operator's "Enter" impersonates the agency's admin: the mask holds
    the admin's rights, so no Email key until the platform granted them to
    the admin role, and never the platform's own routes."""
    office = await make_office()
    entered = await client.post(f"/agencies/{office.agency_id}/enter", headers=root)
    assert entered.status_code == 200, entered.text
    mask = {"Authorization": f"Bearer {entered.json()['access_token']}"}
    me = await client.get("/auth/agent/me", headers=mask)
    assert me.json()["id"] == str(office.admin_id)
    assert await _email_keys_of(client, mask) == []
    refused = await client.get("/email/meta", headers=mask)
    assert refused.status_code == 403, refused.text
    assert refused.json()["params"] == {"permission": "email.view"}
    under_mask = await _grant(client, mask, office.agency_id, role_ids["admin"], EMAIL_KEYS)
    assert under_mask.status_code == 403, under_mask.text
    assert under_mask.json()["params"] == {"permission": "agency.create"}

    granted = await _grant(client, root, office.agency_id, role_ids["admin"], ["email.view"])
    assert granted.status_code == 200, granted.text
    assert await _email_keys_of(client, mask) == ["email.view"]
    meta = await client.get("/email/meta", headers=mask)
    assert meta.status_code == 200, meta.text
