"""Agency + onboarding battery: me-based tenant scoping, agency.manage
gate, invitation hygiene (role validated at creation, 409s, cancel,
single-use accept) and the cross-tenant seals."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.invitation import AgentInvitation
from shared.models.rbac import Role
from src.core import email
from src.core.config import get_settings
from src.core.enums import InvitationStatus
from src.core.rbac.permissions import Permission
from tests.plugins.agency_plugin import MakeAgency, MakeAgentInvitation
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.rbac_plugin import MakeRole


@pytest.fixture
def agencies_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


# --- GET /agencies/me ----------------------------------------------------------


async def test_get_my_agency(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency(name="Reside Paraguay", slug="reside-paraguay")
    # No roles at all: /me is an identity endpoint, no permission needed.
    agent = await make_agent(agency_id=agency.id)
    response = await agencies_client.get("/agencies/me", headers=agent_headers(agent))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(agency.id)
    assert body["slug"] == "reside-paraguay"


async def test_get_my_agency_requires_token(agencies_client: AsyncClient) -> None:
    assert (await agencies_client.get("/agencies/me")).status_code == 401


async def test_agent_only_sees_own_agency(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    agency_a = await make_agency(name="Agency A")
    await make_agency(name="Agency B")
    agent_a = await make_agent(agency_id=agency_a.id)
    response = await agencies_client.get("/agencies/me", headers=agent_headers(agent_a))
    assert response.json()["name"] == "Agency A"


# --- PATCH /agencies/me -----------------------------------------------------------


async def test_patch_my_agency_as_admin(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency(name="Old Name", slug="stable-slug")
    admin = await make_agent(agency_id=agency.id, role=system_roles["admin"])
    response = await agencies_client.patch(
        "/agencies/me",
        headers=agent_headers(admin),
        json={"name": "New Name", "settings": {"timezone": "America/Asuncion"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "New Name"
    assert body["settings"] == {"timezone": "America/Asuncion"}
    # Slug is immutable — not even part of the update schema.
    assert body["slug"] == "stable-slug"


async def test_patch_my_agency_without_permission_403(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(role=system_roles["member"])
    response = await agencies_client.patch(
        "/agencies/me", headers=agent_headers(member), json={"name": "Hacked"}
    )
    assert response.status_code == 403


async def test_patch_does_not_leak_cross_tenant(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency_a = await make_agency(name="A")
    agency_b = await make_agency(name="B")
    admin_a = await make_agent(agency_id=agency_a.id, role=system_roles["admin"])
    await agencies_client.patch(
        "/agencies/me", headers=agent_headers(admin_a), json={"name": "A renamed"}
    )
    admin_b = await make_agent(agency_id=agency_b.id, role=system_roles["admin"])
    response = await agencies_client.get("/agencies/me", headers=agent_headers(admin_b))
    assert response.json()["name"] == "B"


# --- POST /agencies/me/invitations --------------------------------------------------


async def test_create_invitation_with_system_role(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    response = await agencies_client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        json={"email": "newagent@example.com", "role_id": str(system_roles["member"].id)},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["invited_by_agent_id"] == str(admin.id)
    assert len(email.outbox) == 1
    sent = email.outbox[0]
    assert sent.to == "newagent@example.com"
    # The accept link is built on frontend_url, in BOTH multipart parts.
    link_prefix = f"{get_settings().frontend_url}/accept-invitation/"
    assert link_prefix in sent.body
    assert sent.html is not None and link_prefix in sent.html
    assert "Accepter l&#x27;invitation" in sent.html


async def test_create_invitation_with_own_custom_role(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    custom = await make_role(permissions=[Permission.CASE_VIEW], agency_id=admin.agency_id)
    response = await agencies_client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        json={"email": "custom@example.com", "role_id": str(custom.id)},
    )
    assert response.status_code == 201


async def test_create_invitation_foreign_or_unknown_role_422(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    other_agency = await make_agency()
    foreign_role = await make_role(permissions=[Permission.CASE_VIEW], agency_id=other_agency.id)
    foreign = await agencies_client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        json={"email": "x@example.com", "role_id": str(foreign_role.id)},
    )
    assert foreign.status_code == 422

    unknown = await agencies_client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        json={"email": "x@example.com", "role_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert unknown.status_code == 422


# --- Platform-reserved superadmin role: never listed, invited, or assigned ----
# It carries every permission + agency.create, so an agency must never reach
# it through the role surface (escalation barrier).


async def test_superadmin_role_not_listed_to_agency(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    resp = await agencies_client.get("/agencies/me/roles", headers=agent_headers(admin))
    assert resp.status_code == 200
    assert "superadmin" not in {r["name"] for r in resp.json()}


async def test_cannot_invite_as_superadmin(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    resp = await agencies_client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        # Even with the id in hand, the platform role is opaque: 422, like a
        # foreign/unknown role (this flow has no permission ceiling).
        json={"email": "x@example.com", "role_id": str(system_roles["superadmin"].id)},
    )
    assert resp.status_code == 422


async def test_cannot_assign_superadmin_to_member(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    colleague = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    resp = await agencies_client.put(
        f"/agencies/me/members/{colleague.id}/role",
        headers=agent_headers(admin),
        json={"role_id": str(system_roles["superadmin"].id)},
    )
    assert resp.status_code == 422


async def test_create_invitation_email_already_agent_409(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    # Same agency.
    await make_agent(agency_id=admin.agency_id, email="taken@example.com")
    same_agency = await agencies_client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        json={"email": "taken@example.com", "role_id": str(system_roles["member"].id)},
    )
    assert same_agency.status_code == 409

    # OTHER agency: one human = one agent account = one agency at MVP
    # (table-unique email) → refused at creation too.
    other_agency = await make_agency()
    await make_agent(agency_id=other_agency.id, email="elsewhere@example.com")
    other = await agencies_client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        json={"email": "elsewhere@example.com", "role_id": str(system_roles["member"].id)},
    )
    assert other.status_code == 409


async def test_create_invitation_duplicate_pending_409(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    payload = {"email": "dup@example.com", "role_id": str(system_roles["member"].id)}
    first = await agencies_client.post(
        "/agencies/me/invitations", headers=agent_headers(admin), json=payload
    )
    assert first.status_code == 201
    second = await agencies_client.post(
        "/agencies/me/invitations", headers=agent_headers(admin), json=payload
    )
    assert second.status_code == 409


async def test_create_invitation_requires_agent_manage(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(role=system_roles["member"])
    response = await agencies_client.post(
        "/agencies/me/invitations",
        headers=agent_headers(member),
        json={"email": "x@example.com", "role_id": str(system_roles["member"].id)},
    )
    assert response.status_code == 403


# --- GET / DELETE invitations -----------------------------------------------------------


async def test_list_invitations_scoped_to_agency(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_agent_invitation: MakeAgentInvitation,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    other_agency = await make_agency()
    mine = await make_agent_invitation(agency_id=admin.agency_id, role_id=system_roles["member"].id)
    await make_agent_invitation(agency_id=other_agency.id, role_id=system_roles["member"].id)
    response = await agencies_client.get("/agencies/me/invitations", headers=agent_headers(admin))
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()]
    assert ids == [str(mine.id)]


async def test_cancel_invitation_then_accept_fails(
    agencies_client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    make_agent_invitation: MakeAgentInvitation,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    invitation = await make_agent_invitation(
        agency_id=admin.agency_id, role_id=system_roles["member"].id
    )
    response = await agencies_client.delete(
        f"/agencies/me/invitations/{invitation.id}", headers=agent_headers(admin)
    )
    assert response.status_code == 200
    row = await db_session.get(AgentInvitation, invitation.id)
    assert row is not None and row.status == InvitationStatus.CANCELLED

    accept = await agencies_client.post(
        "/agencies/invitations/accept",
        json={
            "token": invitation.token,
            "password": "password123",
            "first_name": "Too",
            "last_name": "Late",
        },
    )
    assert accept.status_code == 400


async def test_cancel_foreign_invitation_404(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_agent_invitation: MakeAgentInvitation,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    other_agency = await make_agency()
    foreign = await make_agent_invitation(
        agency_id=other_agency.id, role_id=system_roles["member"].id
    )
    response = await agencies_client.delete(
        f"/agencies/me/invitations/{foreign.id}", headers=agent_headers(admin)
    )
    assert response.status_code == 404


# --- POST /agencies/invitations/accept -----------------------------------------------------


async def test_accept_creates_agent_in_invitations_agency(
    agencies_client: AsyncClient,
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agent_invitation: MakeAgentInvitation,
    system_roles: dict[str, Role],
) -> None:
    agency_a = await make_agency(name="Agency A")
    invitation = await make_agent_invitation(
        agency_id=agency_a.id,
        role_id=system_roles["member"].id,
        email="recruit@example.com",
    )
    response = await agencies_client.post(
        "/agencies/invitations/accept",
        json={
            "token": invitation.token,
            "password": "password123",
            "first_name": "New",
            "last_name": "Recruit",
        },
    )
    assert response.status_code == 200
    tokens = response.json()

    # Created in the INVITATION's agency, with the invitation's role.
    me = await agencies_client.get(
        "/auth/agent/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.status_code == 200
    body = me.json()
    assert body["agency_id"] == str(agency_a.id)
    assert body["role"] == "member"
    assert "reminder.approve" in body["effective_permissions"]

    invitation_row = await db_session.get(AgentInvitation, invitation.id)
    assert invitation_row is not None
    assert invitation_row.status == InvitationStatus.ACCEPTED
    assert invitation_row.accepted_at is not None

    # Login works with the chosen password.
    login = await agencies_client.post(
        "/auth/agent/login",
        json={"email": "recruit@example.com", "password": "password123"},
    )
    assert login.status_code == 200


async def test_accept_invalid_expired_or_consumed_400(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent_invitation: MakeAgentInvitation,
    system_roles: dict[str, Role],
) -> None:
    payload = {"password": "password123", "first_name": "A", "last_name": "B"}
    unknown = await agencies_client.post(
        "/agencies/invitations/accept", json={"token": "unknown", **payload}
    )
    assert unknown.status_code == 400

    agency = await make_agency()
    expired = await make_agent_invitation(
        agency_id=agency.id,
        role_id=system_roles["member"].id,
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    response = await agencies_client.post(
        "/agencies/invitations/accept", json={"token": expired.token, **payload}
    )
    assert response.status_code == 400

    valid = await make_agent_invitation(agency_id=agency.id, role_id=system_roles["member"].id)
    first = await agencies_client.post(
        "/agencies/invitations/accept", json={"token": valid.token, **payload}
    )
    assert first.status_code == 200
    second = await agencies_client.post(
        "/agencies/invitations/accept", json={"token": valid.token, **payload}
    )
    assert second.status_code == 400


async def test_accept_when_email_became_agent_meanwhile_409(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_agent_invitation: MakeAgentInvitation,
    system_roles: dict[str, Role],
) -> None:
    agency = await make_agency()
    invitation = await make_agent_invitation(
        agency_id=agency.id,
        role_id=system_roles["member"].id,
        email="race@example.com",
    )
    await make_agent(agency_id=agency.id, email="race@example.com")
    response = await agencies_client.post(
        "/agencies/invitations/accept",
        json={
            "token": invitation.token,
            "password": "password123",
            "first_name": "Late",
            "last_name": "Comer",
        },
    )
    assert response.status_code == 409


# --- Additive system-role sync (step 7 adjustment) ----------------------------------------------


async def test_system_role_sync_is_additive(
    db_session: AsyncSession, system_roles: dict[str, Role], make_agent: MakeAgent
) -> None:
    """agency.manage (added to the catalogue this step) must reach the
    admin system role on re-seed — the additive sync at work."""
    from sqlalchemy import select

    from shared.models.rbac import Permission as PermissionRow
    from shared.models.rbac import RolePermission

    stmt = (
        select(PermissionRow.key)
        .join(RolePermission, RolePermission.permission_id == PermissionRow.id)
        .where(RolePermission.role_id == system_roles["admin"].id)
    )
    admin_keys = set((await db_session.execute(stmt)).scalars())
    assert Permission.AGENCY_MANAGE.value in admin_keys

    stmt = (
        select(PermissionRow.key)
        .join(RolePermission, RolePermission.permission_id == PermissionRow.id)
        .where(RolePermission.role_id == system_roles["case_manager"].id)
    )
    case_manager_keys = set((await db_session.execute(stmt)).scalars())
    assert Permission.AGENCY_MANAGE.value not in case_manager_keys


async def test_agent_rows_isolated_per_agency(
    db_session: AsyncSession, make_agency: MakeAgency, make_agent: MakeAgent
) -> None:
    agency_a = await make_agency()
    agency_b = await make_agency()
    agent = await make_agent(agency_id=agency_a.id)
    row = await db_session.get(Agent, agent.id)
    assert row is not None and row.agency_id == agency_a.id != agency_b.id


# --- GET /agencies/me/members ----------------------------------------------------


async def test_list_members_with_roles_sorted_by_name(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"], first_name="Zoe", last_name="Zima")
    colleague = await make_agent(
        agency_id=admin.agency_id,
        role=system_roles["member"],
        first_name="Ana",
        last_name="Abad",
    )
    response = await agencies_client.get("/agencies/me/members", headers=agent_headers(admin))
    assert response.status_code == 200
    body = response.json()
    assert [m["email"] for m in body] == [colleague.email, admin.email]
    by_id = {m["id"]: m for m in body}
    assert by_id[str(colleague.id)]["role"] == "member"
    assert by_id[str(colleague.id)]["role_id"] == str(system_roles["member"].id)
    assert by_id[str(admin.id)]["role"] == "admin"
    assert by_id[str(colleague.id)]["first_name"] == "Ana"
    assert by_id[str(colleague.id)]["last_name"] == "Abad"


async def test_list_members_scoped_to_own_agency(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    rbac_baseline: None,
) -> None:
    agent_a = await make_agent()
    agent_b = await make_agent()  # auto-creates its own agency
    response = await agencies_client.get("/agencies/me/members", headers=agent_headers(agent_a))
    assert response.status_code == 200
    emails = [m["email"] for m in response.json()]
    assert agent_a.email in emails
    assert agent_b.email not in emails


async def test_member_accesses_reference_lists_token_only(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(role=system_roles["member"])
    for path in ("/agencies/me/members", "/agencies/me/roles"):
        response = await agencies_client.get(path, headers=agent_headers(member))
        assert response.status_code == 200


# --- GET /agencies/me/roles ------------------------------------------------------


async def test_list_roles_system_plus_own_custom_only(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    make_agency: MakeAgency,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agent = await make_agent(role=system_roles["member"])
    custom = await make_role(name="visa-specialist", agency_id=agent.agency_id)
    other_agency = await make_agency()
    await make_role(name="foreign-role", agency_id=other_agency.id)

    response = await agencies_client.get("/agencies/me/roles", headers=agent_headers(agent))
    assert response.status_code == 200
    by_name = {r["name"]: r for r in response.json()}
    assert {"admin", "member", "viewer", "case_manager", "visa-specialist"} <= set(by_name)
    assert "foreign-role" not in by_name
    assert by_name["admin"]["is_system"] is True
    assert by_name["visa-specialist"]["is_system"] is False
    assert by_name["visa-specialist"]["id"] == str(custom.id)


async def test_reference_lists_reject_expat_token(
    agencies_client: AsyncClient,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user()
    for path in ("/agencies/me/members", "/agencies/me/roles"):
        response = await agencies_client.get(path, headers=expat_headers(expat))
        assert response.status_code == 401


# --- sectors : PATCH replacement + inertness invariant (2026-07-21) --------------------


async def test_patch_agency_sectors_full_replacement_and_clear(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency()
    admin = await make_agent(agency_id=agency.id, role=system_roles["admin"])
    headers = agent_headers(admin)

    posed = await agencies_client.patch(
        "/agencies/me", headers=headers, json={"sectors": ["legal"]}
    )
    assert posed.json()["sectors"] == ["legal"]

    # FULL replacement (not a merge).
    replaced = await agencies_client.patch(
        "/agencies/me", headers=headers, json={"sectors": ["immigration", "consulting"]}
    )
    assert replaced.json()["sectors"] == ["immigration", "consulting"]

    # Empty list = valid, back to neutral.
    cleared = await agencies_client.patch("/agencies/me", headers=headers, json={"sectors": []})
    assert cleared.json()["sectors"] == []

    # Unknown value → 422 named.
    bad = await agencies_client.patch(
        "/agencies/me", headers=headers, json={"sectors": ["banking"]}
    )
    assert bad.status_code == 422
    assert "agency.sector_invalid" in bad.text


async def test_sectors_are_inert_no_behaviour_change(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """INVARIANT: an agency WITH sectors and one WITHOUT behave identically
    on everything else — the field is stored, never consumed. Setting
    sectors changes nothing but the sectors field itself."""
    agency = await make_agency(name="Inert Co")
    admin = await make_agent(agency_id=agency.id, role=system_roles["admin"])
    headers = agent_headers(admin)

    before = (await agencies_client.get("/agencies/me", headers=headers)).json()
    patched = await agencies_client.patch(
        "/agencies/me", headers=headers, json={"sectors": ["wealth", "hr_mobility"]}
    )
    assert patched.json()["sectors"] == ["wealth", "hr_mobility"]
    # Same endpoint before/after → every OTHER field is untouched by sectors.
    after = (await agencies_client.get("/agencies/me", headers=headers)).json()
    assert after["sectors"] == ["wealth", "hr_mobility"]
    for key in before:
        if key != "sectors":
            assert after[key] == before[key], key


async def test_patch_foreign_agency_sectors_isolated(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """/agencies/me writes ONLY the caller's own agency — an admin of A
    never touches agency B's sectors (cross-tenant isolation)."""
    agency_a = await make_agency(name="A")
    agency_b = await make_agency(name="B")
    admin_a = await make_agent(agency_id=agency_a.id, role=system_roles["admin"])
    admin_b = await make_agent(agency_id=agency_b.id, role=system_roles["admin"])

    await agencies_client.patch(
        "/agencies/me", headers=agent_headers(admin_a), json={"sectors": ["legal"]}
    )
    b_view = (await agencies_client.get("/agencies/me", headers=agent_headers(admin_b))).json()
    assert b_view["sectors"] == []  # B untouched by A's PATCH


# --- sectors_onboarding_required flag (self-signup gate, 2026-07-21) -------------------


async def test_patch_sectors_clears_onboarding_flag_only_when_non_empty(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    """A self-signup-style flagged agency: posing >= 1 sector clears the
    flag (onboarding satisfied); posing [] leaves it TRUE (still must pick)."""
    from sqlalchemy import update as sa_update

    from shared.models.agency import Agency

    agency = await make_agency(name="Flagged Co")
    await db_session.execute(
        sa_update(Agency).where(Agency.id == agency.id).values(sectors_onboarding_required=True)
    )
    await db_session.commit()
    admin = await make_agent(agency_id=agency.id, role=system_roles["admin"])
    headers = agent_headers(admin)

    # Posing [] does NOT satisfy onboarding → flag stays TRUE.
    empty = await agencies_client.patch("/agencies/me", headers=headers, json={"sectors": []})
    assert empty.json()["sectors_onboarding_required"] is True

    # Posing >= 1 sector clears the flag.
    posed = await agencies_client.patch(
        "/agencies/me", headers=headers, json={"sectors": ["immigration"]}
    )
    assert posed.json()["sectors"] == ["immigration"]
    assert posed.json()["sectors_onboarding_required"] is False


async def test_existing_agency_never_flagged_migration_invariant(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """The critical invariant: an agency created the normal way (== every
    existing agency after the migration default false) is NEVER flagged."""
    agency = await make_agency(name="Existing Co")
    admin = await make_agent(agency_id=agency.id, role=system_roles["admin"])
    me = (await agencies_client.get("/agencies/me", headers=agent_headers(admin))).json()
    assert me["sectors_onboarding_required"] is False  # never bothered
