"""Ticket Nicolas 2026-07-26 — l'email d'accès des personnes ajoutées.

PREUVE (phase A) : l'email PART déjà, vers LA personne ajoutée, sur les deux
gestes backend qui créent un accès — POST person avec email, et PATCH email
sur une personne sans compte (le geste Arthur). Ce fichier épingle ce que le
code faisait déjà sans témoin : aucun test outbox ne couvrait ces envois.

Le chemin « création initiale du dossier avec plusieurs personnes » passe par
le MÊME endpoint (la modale enchaîne POST /persons par membre) mais n'envoie
jamais d'email de membre : le formulaire ne collecte pas d'adresse — la
personne naît sans compte, il n'y a littéralement rien à envoyer (témoin 3).
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
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


@pytest_asyncio.fixture
async def case(
    admin: Agent, make_client_case: MakeClientCase, make_expat_user: MakeExpatUser
) -> ClientCase:
    principal = await make_expat_user(activated=True, email="principal-acc@example.com")
    return await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )


async def _member_token(db: AsyncSession, case_id: object, member_email: str) -> str:
    return (
        await db.execute(
            select(CaseInvitation.token).where(
                CaseInvitation.case_id == case_id, CaseInvitation.email == member_email
            )
        )
    ).scalar_one()


# --- (1) POST person AVEC email : le mail part vers LA personne, pas le principal ----


async def test_adding_a_person_with_email_mails_that_person(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    r = await client.post(
        f"/cases/{case.id}/persons",
        headers=agent_headers(admin),
        json={"full_name": "Ana Sancho", "relationship": "spouse", "email": "ana@example.com"},
    )
    assert r.status_code == 201, r.text
    body = r.json()

    # UN mail, à l'adresse de la personne ajoutée — jamais au principal.
    assert [m.to for m in email.outbox] == ["ana@example.com"]
    # Compte NEUF → mail d'ACTIVATION portant le token de SON invitation.
    token = await _member_token(db_session, case.id, "ana@example.com")
    assert f"/space/activate/{token}" in email.outbox[0].html

    # NID-23 s'applique à elle comme au principal : état dérivé + renvoi.
    assert body["activated"] is False
    assert body["client_space_state"] == "pending"
    assert body["invitation_expires_at"] is not None


# --- (2) PATCH email sur une personne sans compte (geste Arthur) : idem --------------


async def test_giving_an_email_after_the_fact_mails_that_person(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    # La forme exacte de la modale de création : name + relationship, PAS
    # d'email — la personne naît sans compte, aucun mail (témoin chemin a).
    created = await client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={"full_name": "Leo Sancho", "relationship": "son"},
    )
    assert created.status_code == 201
    assert email.outbox == []
    assert created.json()["client_space_state"] is None  # pas de compte du tout

    patched = await client.patch(
        f"/cases/{case.id}/persons/{created.json()['id']}",
        headers=headers,
        json={"email": "leo@example.com"},
    )
    assert patched.status_code == 200, patched.text
    assert [m.to for m in email.outbox] == ["leo@example.com"]
    token = await _member_token(db_session, case.id, "leo@example.com")
    assert f"/space/activate/{token}" in email.outbox[0].html
    assert patched.json()["client_space_state"] == "pending"


# --- (3) témoins négatifs : jamais de doublon, jamais de token sur un compte actif ---


async def test_no_duplicate_and_no_activation_token_for_an_active_account(
    client: AsyncClient,
    admin: Agent,
    case: ClientCase,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
) -> None:
    headers = agent_headers(admin)
    # Personne déjà titulaire d'un compte ACTIVÉ ailleurs : elle reçoit
    # « un dossier vous attend » (lien login), jamais un lien d'activation
    # (un token de set-password sur un compte vivant = vecteur de takeover).
    await make_expat_user(activated=True, email="deja-actif@example.com")
    r = await client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={
            "full_name": "Deja Actif",
            "relationship": "associate",
            "email": "deja-actif@example.com",
        },
    )
    assert r.status_code == 201, r.text
    assert [m.to for m in email.outbox] == ["deja-actif@example.com"]
    assert "/space/activate/" not in email.outbox[0].html
    assert "/space/login" in email.outbox[0].html
    assert r.json()["client_space_state"] == "active"

    # PATCH du MÊME email sur cette personne : no-op propre, zéro mail.
    email.outbox.clear()
    patched = await client.patch(
        f"/cases/{case.id}/persons/{r.json()['id']}",
        headers=headers,
        json={"email": "deja-actif@example.com"},
    )
    assert patched.status_code == 200, patched.text
    assert email.outbox == []


# --- (4) langue du mail membre (décision 27/07) --------------------------------------


async def test_new_member_account_speaks_the_agency_language(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """Compte membre NEUF : la personne n'a pas de langue connue → le mail
    part dans la default_language de l'agence (avant : « fr » système, une
    agence anglophone invitait ses membres en français). Compte EXISTANT :
    SA langue tient, l'agence n'écrase rien."""
    from shared.models.agency import Agency
    from shared.models.expat_user import ExpatUser

    agency = Agency(name="EN Agency", slug="en-agency-lang", default_language="en")
    db_session.add(agency)
    await db_session.commit()
    admin = await make_agent(agency_id=agency.id, role=system_roles["admin"])
    principal = await make_expat_user(activated=True)
    case = await make_client_case(agency_id=agency.id, principal_expat_user_id=principal.id)

    # Compte neuf → default_language de l'agence (en), sur le compte ET le mail.
    r = await client.post(
        f"/cases/{case.id}/persons",
        headers=agent_headers(admin),
        json={
            "full_name": "New Member",
            "relationship": "associate",
            "email": "new-en@example.com",
        },
    )
    assert r.status_code == 201, r.text
    member = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "new-en@example.com"))
    ).scalar_one()
    assert member.preferred_lang == "en"
    assert 'html lang="en"' in email.outbox[-1].html

    # Compte existant (ru) → SA langue tient, malgré l'agence en anglais.
    await make_expat_user(activated=True, email="existing-ru@example.com", preferred_lang="ru")
    email.outbox.clear()
    r2 = await client.post(
        f"/cases/{case.id}/persons",
        headers=agent_headers(admin),
        json={
            "full_name": "Existing Ru",
            "relationship": "associate",
            "email": "existing-ru@example.com",
        },
    )
    assert r2.status_code == 201, r2.text
    assert 'html lang="ru"' in email.outbox[0].html
