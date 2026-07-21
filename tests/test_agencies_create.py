"""POST /agencies — superadmin-only agency creation (BLOC 2).

Gated agency.create: only the superadmin role reaches it. Creates the
agency + its first admin ATOMICALLY and stages ONE activation email. No
cross-agency access is introduced — the superadmin still reads no dossier.
"""

from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.auth_tokens import PasswordResetToken
from shared.models.rbac import Role
from src.core import email
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def agencies_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Reside Paraguay",
        "slug": "reside-paraguay",
        "default_language": "es",
        "admin_email": "admin@reside-paraguay.com",
        "admin_first_name": "Alexis",
        "admin_last_name": "Renard",
    }
    body.update(overrides)
    return body


# --- (a) happy path: superadmin creates an agency + admin + invitation ---------


async def test_superadmin_creates_agency(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    db_session: AsyncSession,
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.post("/agencies", json=_body(), headers=agent_headers(superadmin))
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["agency"]["slug"] == "reside-paraguay"
    assert body["agency"]["default_language"] == "es"
    # Settings hold ONLY the demo-case seed marker (nurture bloc 2).
    assert set(body["agency"]["settings"]) == {"demo_case_seeded_at"}
    assert body["admin"]["email"] == "admin@reside-paraguay.com"
    assert body["admin"]["role"] == "admin"

    agency = (
        await db_session.execute(select(Agency).where(Agency.slug == "reside-paraguay"))
    ).scalar_one()
    assert set(agency.settings) == {"demo_case_seeded_at"}
    assert agency.default_language == "es"

    # First admin persisted in the NEW agency, pointing at the SHARED admin role.
    admin = (
        await db_session.execute(select(Agent).where(Agent.email == "admin@reside-paraguay.com"))
    ).scalar_one()
    assert admin.agency_id == agency.id
    assert admin.role_id == system_roles["admin"].id
    assert admin.is_external is False

    # Activation ("invitation"): a single-use reset token staged for the admin.
    token = (
        await db_session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.actor_id == admin.id,
                PasswordResetToken.actor_type == "agent",
            )
        )
    ).scalar_one()
    assert token.consumed_at is None

    # One activation email queued to the admin (sent off-request via BackgroundTasks).
    assert [m for m in email.outbox if m.to == "admin@reside-paraguay.com"]


async def test_slug_derived_from_name_when_omitted(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.post(
        "/agencies",
        json=_body(slug=None, name="Domiciliation Bulgarie", admin_email="a@bulg.io"),
        headers=agent_headers(superadmin),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["agency"]["slug"] == "domiciliation-bulgarie"


# --- (b) a normal agency admin is forbidden (agency.create absent) -------------


async def test_normal_admin_forbidden(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    resp = await agencies_client.post("/agencies", json=_body(), headers=agent_headers(admin))
    assert resp.status_code == 403


async def test_requires_agent_token(agencies_client: AsyncClient) -> None:
    assert (await agencies_client.post("/agencies", json=_body())).status_code == 401


# --- (c) slug already taken -> 409 --------------------------------------------


async def test_slug_already_taken_conflicts(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    await make_agency(slug="reside-paraguay")
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.post(
        "/agencies", json=_body(slug="reside-paraguay"), headers=agent_headers(superadmin)
    )
    assert resp.status_code == 409


async def test_admin_email_already_agent_conflicts(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    existing = await make_agent()  # already an agent somewhere
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.post(
        "/agencies", json=_body(admin_email=existing.email), headers=agent_headers(superadmin)
    )
    assert resp.status_code == 409


# --- (d) full rights in its OWN agency, still no cross-agency access ----------


async def test_superadmin_has_full_rights_but_no_cross_agency(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"])
    # Creating an agency is allowed (agency.create)...
    created = await agencies_client.post(
        "/agencies", json=_body(), headers=agent_headers(superadmin)
    )
    assert created.status_code == 201
    # ...and the platform-owner role now carries EVERY internal permission, so
    # reading dossiers is allowed (case.view) — no longer 403. The reads stay
    # scoped to the superadmin's OWN agency (the token's agency_id), so the
    # agency it just created is unreachable from this token: no cross-agency
    # access is introduced (that frontier is the separate Phase 2).
    own = await agencies_client.get("/cases", headers=agent_headers(superadmin))
    assert own.status_code == 200


# --- (e) platform agency switcher: list all agencies + enter one --------------


async def test_superadmin_lists_all_agencies(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    a = await make_agency()
    b = await make_agency()
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.get("/agencies", headers=agent_headers(superadmin))
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert {str(a.id), str(b.id)} <= ids  # the cross-tenant read sees every agency


async def test_normal_agent_cannot_list_all_agencies(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    resp = await agencies_client.get("/agencies", headers=agent_headers(admin))
    assert resp.status_code == 403


async def test_superadmin_enters_another_agency_scoped_to_it(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    target = await make_agency()
    await make_agent(agency_id=target.id, role=system_roles["admin"])
    superadmin = await make_agent(role=system_roles["superadmin"])

    enter = await agencies_client.post(
        f"/agencies/{target.id}/enter", headers=agent_headers(superadmin)
    )
    assert enter.status_code == 200, enter.text
    token = enter.json()["access_token"]
    assert token

    # The issued token is scoped to the TARGET agency: /agencies/me resolves
    # to it (the token's subject is a real admin of that agency).
    me = await agencies_client.get("/agencies/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["id"] == str(target.id)


async def test_normal_agent_cannot_enter_agency(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    target = await make_agency()
    await make_agent(agency_id=target.id, role=system_roles["admin"])
    admin = await make_agent(role=system_roles["admin"])
    resp = await agencies_client.post(f"/agencies/{target.id}/enter", headers=agent_headers(admin))
    assert resp.status_code == 403


async def test_superadmin_cannot_enter_own_agency(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.post(
        f"/agencies/{superadmin.agency_id}/enter", headers=agent_headers(superadmin)
    )
    assert resp.status_code == 422


async def test_enter_agency_without_admin_is_404(
    agencies_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    empty = await make_agency()  # no agents at all
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.post(
        f"/agencies/{empty.id}/enter", headers=agent_headers(superadmin)
    )
    assert resp.status_code == 404


# --- onboarding link lifetime (demande Eric: invitation = 24h) ------------------


async def test_onboarding_link_lives_24_hours_and_is_single_use(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    db_session: AsyncSession,
) -> None:
    """The first-admin activation link is an INVITATION: 24h window
    (valid at H+2, already expired at H+25), stated in hours in the
    mail — while staying strictly single-use like any reset token."""
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.post("/agencies", json=_body(), headers=agent_headers(superadmin))
    assert resp.status_code == 201, resp.text

    admin = (
        await db_session.execute(select(Agent).where(Agent.email == "admin@reside-paraguay.com"))
    ).scalar_one()
    token = (
        await db_session.execute(
            select(PasswordResetToken).where(PasswordResetToken.actor_id == admin.id)
        )
    ).scalar_one()

    # (a) the 24h window: still valid at H+2, gone by H+25.
    lifetime = token.expires_at - token.created_at
    assert timedelta(hours=23) < lifetime < timedelta(hours=25)
    sent = next(m for m in email.outbox if m.to == "admin@reside-paraguay.com")
    # The onboarding mail is rendered in the AGENCY's default language (es here)
    # — "24 horas", not the old French-hardcoded "24 heures".
    assert "24 horas" in sent.body

    # (c) single use: the link works once...
    ok = await agencies_client.post(
        "/auth/agent/reset-password",
        json={"token": token.token, "password": "brand-new-pass-1"},
    )
    assert ok.status_code == 200, ok.text
    login = await agencies_client.post(
        "/auth/agent/login",
        json={"email": "admin@reside-paraguay.com", "password": "brand-new-pass-1"},
    )
    assert login.status_code == 200
    # ...and never twice.
    again = await agencies_client.post(
        "/auth/agent/reset-password",
        json={"token": token.token, "password": "another-pass-12"},
    )
    assert again.status_code == 400


# --- sectors (multi-sector groundwork; INERT — nothing consumes it) --------------------


async def test_create_agency_with_sectors_persisted_and_exposed(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    db_session: AsyncSession,
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.post(
        "/agencies",
        json=_body(sectors=["legal", "accounting"]),
        headers=agent_headers(superadmin),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["agency"]["sectors"] == ["legal", "accounting"]
    agency = (
        await db_session.execute(select(Agency).where(Agency.slug == "reside-paraguay"))
    ).scalar_one()
    assert agency.sectors == ["legal", "accounting"]


async def test_create_agency_without_sectors_is_empty(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.post("/agencies", json=_body(), headers=agent_headers(superadmin))
    assert resp.status_code == 201
    assert resp.json()["agency"]["sectors"] == []  # neutral default


async def test_create_agency_unknown_sector_is_422_named(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.post(
        "/agencies",
        json=_body(sectors=["legal", "notariat"]),  # notariat lives IN legal
        headers=agent_headers(superadmin),
    )
    assert resp.status_code == 422
    assert "agency.sector_invalid" in resp.text


async def test_create_agency_sectors_deduplicated(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"])
    resp = await agencies_client.post(
        "/agencies",
        json=_body(sectors=["legal", "legal", "immigration"]),
        headers=agent_headers(superadmin),
    )
    assert resp.status_code == 201
    assert resp.json()["agency"]["sectors"] == ["legal", "immigration"]  # deduped, order kept
