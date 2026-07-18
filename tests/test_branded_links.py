"""White-label links: every client email lands on the BRANDED space
(?agency=<slug>) — activation, new-case, step mails (requirement request /
reopen), comment notifications. Token flows untouched: only the URLs."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.invitation import CaseInvitation
from shared.models.rbac import Role
from src.core import email
from src.core.email import space_link
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _slug(db_session: AsyncSession, agency_id: uuid.UUID) -> str:
    agency = await db_session.get(Agency, agency_id)
    assert agency is not None
    return agency.slug


def _case_payload(email_addr: str, journey_template_id: str) -> dict[str, str]:
    return {
        "first_name": "Jean",
        "last_name": "Martin",
        "email": email_addr,
        "origin_country": "FR",
        "dest_country": "PY",
        "journey_template_id": journey_template_id,
    }


async def test_activation_and_new_case_links_carry_slug(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    slug = await _slug(db_session, admin.agency_id)
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]

    # New expat → activation mail, branded activation link.
    created = await client.post(
        "/cases", headers=headers, json=_case_payload("brand-new@example.com", tid)
    )
    assert created.status_code == 201, created.text
    invitation = (
        await db_session.execute(
            select(CaseInvitation).where(CaseInvitation.case_id == uuid.UUID(created.json()["id"]))
        )
    ).scalar_one()
    sent = next(m for m in email.outbox if m.to == "brand-new@example.com")
    expected = f"/space/activate/{invitation.token}?agency={slug}"
    assert expected in sent.body
    assert sent.html is not None and expected in sent.html

    # Existing ACTIVATED expat → "new case" mail, branded login link.
    existing = await make_expat_user(email="already-here@example.com")
    email.outbox.clear()
    created = await client.post("/cases", headers=headers, json=_case_payload(existing.email, tid))
    assert created.status_code == 201
    sent = next(m for m in email.outbox if m.to == existing.email)
    assert f"/space/login?agency={slug}" in sent.body


async def test_step_mail_carries_slug(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The requirement_request mail (step activation) — same site serves
    the reopen mail."""
    headers = agent_headers(admin)
    slug = await _slug(db_session, admin.agency_id)
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    sid = (await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S"})).json()[
        "id"
    ]
    r = await client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=headers,
        json={"kind": "document", "reference": "Preuve", "scope": "principal"},
    )
    assert r.status_code == 201

    expat = await make_expat_user(email="stepmail@example.com")
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    email.outbox.clear()
    # L'assignation envoie le KICKOFF (anti-burst J1) et ouvre la fenetre :
    # le lien brande se verifie sur lui — le demarrage qui suit est absorbe.
    pid = (
        await client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()[0]["id"]
    kickoff = next(m for m in email.outbox if m.to == expat.email)
    assert f"/space?agency={slug}" in kickoff.body
    email.outbox.clear()
    started = await client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=headers, json={"status": "in_progress"}
    )
    assert started.status_code == 200
    assert not [m for m in email.outbox if m.to == expat.email]  # la fenetre absorbe


async def test_comment_mail_carries_slug(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    slug = await _slug(db_session, admin.agency_id)
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S"})
    expat = await make_expat_user(email="commentmail@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = (
        await client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()[0]["id"]

    email.outbox.clear()
    posted = await client.post(
        f"/cases/{case.id}/steps/{pid}/comments", headers=headers, json={"body": "bonjour"}
    )
    assert posted.status_code == 201, posted.text
    sent = next(m for m in email.outbox if m.to == expat.email)
    assert f"/space?agency={slug}" in sent.body


def test_space_link_encodes_slug_cleanly() -> None:
    assert (
        space_link("https://app.x", "/space/login", "agence-lyon")
        == "https://app.x/space/login?agency=agence-lyon"
    )
    # Defensive encoding, whatever a future slug holds.
    assert (
        space_link("https://app.x", "/space", "é space/x")
        == "https://app.x/space?agency=%C3%A9%20space%2Fx"
    )
    # No slug in hand → the naked URL, unchanged behaviour.
    assert space_link("https://app.x", "/space/login", None) == "https://app.x/space/login"
