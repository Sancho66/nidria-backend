"""PATCH email d'une personne (call Nicolas 17/07) : le comportement par
ÉTAT — (a) sans invitation pendante : écriture simple ; (b) invitation
PENDING : l'ancienne meurt (token compris), la nouvelle part à la bonne
adresse (invitation_resent: true) ; (c) activée : 409 person.email_locked.
Anti-collision même dossier : 409 person.email_taken. La collision avec un
expat_user d'un AUTRE compte ne se gère pas au PATCH : le link-or-create
la résout comme à la création, et l'ACTIVATION suit le pattern existant."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.invitation import CaseInvitation
from shared.models.rbac import Role
from src.core import email
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _member_with_typo(
    client: AsyncClient, ah: dict, admin: Agent, make_client_case, make_expat_user
) -> tuple[str, str]:
    """Le cas Nicolas : membre ajouté avec un email FAUTÉ — compte créé
    (jamais activé), invitation pending partie dans le vide."""
    principal = await make_expat_user()
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    created = await client.post(
        f"/cases/{case.id}/persons",
        headers=ah,
        json={
            "full_name": "Igor Volkov",
            "relationship": "associe",
            "email": "igor.volkvo@typo.example",
        },
    )
    assert created.status_code == 201, created.text
    return str(case.id), created.json()["id"]


async def test_pending_typo_fix_resends_and_kills_old_token(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """(b) LE déblocage : corriger la faute invalide l'ancienne invitation
    (son token meurt), en renvoie une à la BONNE adresse, et le dit."""
    ah = agent_headers(admin)
    case_id, person_id = await _member_with_typo(
        client, ah, admin, make_client_case, make_expat_user
    )
    old_invitation = (
        await db_session.execute(
            select(CaseInvitation).where(CaseInvitation.email == "igor.volkvo@typo.example")
        )
    ).scalar_one()
    old_token = old_invitation.token
    email.outbox.clear()
    fixed = await client.patch(
        f"/cases/{case_id}/persons/{person_id}",
        headers=ah,
        json={"email": "igor.volkov@ok.example"},
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["invitation_resent"] is True
    # la nouvelle invitation part a la BONNE adresse
    assert [m.to for m in email.outbox] == ["igor.volkov@ok.example"]
    # l'ancien token est MORT (le pattern re-POST du signup)
    dead = await client.post(
        "/auth/expat/activate", json={"token": old_token, "password": "MotdepasseSolide1"}
    )
    assert dead.status_code == 400
    # le nouveau vit : l'activation par le nouveau token aboutit
    db_session.expire_all()
    new_invitation = (
        await db_session.execute(
            select(CaseInvitation).where(CaseInvitation.email == "igor.volkov@ok.example")
        )
    ).scalar_one()
    alive = await client.post(
        "/auth/expat/activate",
        json={"token": new_invitation.token, "password": "MotdepasseSolide1"},
    )
    assert alive.status_code == 200, alive.text


async def test_no_pending_invitation_simple_write_no_mail(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """(a) sans invitation pendante (expirée ici) : écriture simple —
    pas de renvoi, invitation_resent false."""
    ah = agent_headers(admin)
    case_id, person_id = await _member_with_typo(
        client, ah, admin, make_client_case, make_expat_user
    )
    await db_session.execute(
        update(CaseInvitation)
        .where(CaseInvitation.email == "igor.volkvo@typo.example")
        .values(status="expired")
    )
    await db_session.commit()
    email.outbox.clear()
    fixed = await client.patch(
        f"/cases/{case_id}/persons/{person_id}",
        headers=ah,
        json={"email": "igor.simple@ok.example"},
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["invitation_resent"] is False
    assert email.outbox == []  # ecriture simple, zero mail


async def test_activated_is_locked(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """(c) activée : 409 person.email_locked — le changement est à elle."""
    ah = agent_headers(admin)
    case_id, person_id = await _member_with_typo(
        client, ah, admin, make_client_case, make_expat_user
    )
    await db_session.execute(
        update(ExpatUser)
        .where(ExpatUser.email == "igor.volkvo@typo.example")
        .values(activated_at=datetime.now(UTC))
    )
    await db_session.commit()
    denied = await client.patch(
        f"/cases/{case_id}/persons/{person_id}",
        headers=ah,
        json={"email": "igor.volkov@ok.example"},
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "person.email_locked"


async def test_same_case_email_taken_409(
    client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case_id, person_id = await _member_with_typo(
        client, ah, admin, make_client_case, make_expat_user
    )
    second = await client.post(
        f"/cases/{case_id}/persons",
        headers=ah,
        json={"full_name": "Olga Volkova", "relationship": "epouse", "email": "olga@ok.example"},
    )
    assert second.status_code == 201
    denied = await client.patch(
        f"/cases/{case_id}/persons/{person_id}",
        headers=ah,
        json={"email": "olga@ok.example"},  # deja porte par Olga sur CE dossier
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "person.email_taken"


async def test_cross_account_collision_resolves_at_activation(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """La collision inter-comptes DOCUMENTÉE : le nouvel email appartient à
    un expat_user existant (activé, autre compte) — le PATCH passe (200),
    le link-or-create LIE ce compte (un login, N dossiers), et la personne
    voit le dossier depuis SON compte existant : le pattern de la création,
    résolu à l'activation, rien de spécial au PATCH."""
    ah = agent_headers(admin)
    case_id, person_id = await _member_with_typo(
        client, ah, admin, make_client_case, make_expat_user
    )
    existing = await make_expat_user(email="deja.la@ok.example")  # compte ACTIVE d'ailleurs
    email.outbox.clear()
    fixed = await client.patch(
        f"/cases/{case_id}/persons/{person_id}",
        headers=ah,
        json={"email": "deja.la@ok.example"},
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["invitation_resent"] is True
    # le mail est le "un dossier vous attend" (compte deja actif), a la bonne adresse
    assert [m.to for m in email.outbox] == ["deja.la@ok.example"]
    # et SON compte existant voit le dossier — la resolution par l'activation
    mine = (await client.get("/expat/cases", headers=expat_headers(existing))).json()
    assert case_id in {c["id"] for c in mine}


async def test_shared_unactivated_account_is_locked_too(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """(b') compte non activé mais PARTAGÉ (la même personne est membre
    d'un autre dossier) : corriger "son" email toucherait une identité
    partagée -> 409 email_locked, comme l'activée."""
    ah = agent_headers(admin)
    case_id, person_id = await _member_with_typo(
        client, ah, admin, make_client_case, make_expat_user
    )
    other_principal = await make_expat_user()
    other_case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=other_principal.id
    )
    linked = await client.post(
        f"/cases/{other_case.id}/persons",
        headers=ah,
        json={
            "full_name": "Igor ailleurs",
            "relationship": "associe",
            "email": "igor.volkvo@typo.example",
        },  # le MEME compte, autre dossier
    )
    assert linked.status_code == 201
    denied = await client.patch(
        f"/cases/{case_id}/persons/{person_id}",
        headers=ah,
        json={"email": "igor.volkov@ok.example"},
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "person.email_locked"
