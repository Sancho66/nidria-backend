"""POST /agencies — superadmin-only agency creation (BLOC 2).

Gated agency.create: only the superadmin role reaches it. Creates the
agency + its first admin ATOMICALLY and stages ONE activation email. No
cross-agency access is introduced — the superadmin still reads no dossier.
"""

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
    assert body["agency"]["settings"] == {}
    assert body["admin"]["email"] == "admin@reside-paraguay.com"
    assert body["admin"]["role"] == "admin"

    # Agency persisted with empty settings.
    agency = (
        await db_session.execute(select(Agency).where(Agency.slug == "reside-paraguay"))
    ).scalar_one()
    assert agency.settings == {}
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


# --- (d) no cross-agency access is introduced ---------------------------------


async def test_superadmin_still_reads_no_dossier(
    agencies_client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"])
    # Creating an agency is allowed...
    created = await agencies_client.post(
        "/agencies", json=_body(), headers=agent_headers(superadmin)
    )
    assert created.status_code == 201
    # ...but the superadmin holds ONLY agency.create — reading dossiers is denied
    # (no case.view): neither cross-agency nor even its own. Frontier intact.
    denied = await agencies_client.get("/cases", headers=agent_headers(superadmin))
    assert denied.status_code == 403
