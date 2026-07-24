"""Renvoi d'invitation à l'espace client + état du lien (NID-23, 24/07/2026).

Le trou comblé : aucun endpoint ne renvoyait l'invitation. Le seul levier
existant était de PATCHer la personne vers une AUTRE adresse — donc un client
dont le lien de 14 jours avait expiré était injoignable (et son
forgot-password reste muet tant qu'il n'est pas activé). 5 dossiers réels
étaient dans cet état en prod le 24/07.

Couvre : le renvoi sur lien expiré (token + expiry rotés, mail parti), le
refus nommé sur un espace déjà actif, la personne sans compte, le cooldown
anti-rafale, l'état du lien dans ses 3 cas (détail ET liste), et la garantie
qu'aucun mail ne part vers une adresse de démo.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.invitation import CaseInvitation
from shared.models.rbac import Role
from src.core import email
from src.core.email import demo_expat_email
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeCaseInvitation, MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _principal_person_id(db: AsyncSession, case_id: uuid.UUID) -> uuid.UUID:
    return (
        await db.execute(
            select(CasePerson.id).where(
                CasePerson.case_id == case_id, CasePerson.kind == "principal"
            )
        )
    ).scalar_one()


async def _invitations(db: AsyncSession, case_id: uuid.UUID) -> list[CaseInvitation]:
    return list(
        (
            await db.execute(
                select(CaseInvitation)
                .where(CaseInvitation.case_id == case_id)
                .order_by(CaseInvitation.created_at)
            )
        ).scalars()
    )


# --- (1) le geste : lien expiré -> nouveau token, nouvelle expiry, mail parti --------


async def test_resend_on_an_expired_link_rotates_the_token_and_sends(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> None:
    principal = await make_expat_user(activated=False, email="stuck@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    # L'état des 5 dossiers de prod : invitée il y a 17 jours, lien mort depuis 3.
    dead = await make_case_invitation(
        case=case,
        email=principal.email,
        created_at=datetime.now(UTC) - timedelta(days=17),
        expires_at=datetime.now(UTC) - timedelta(days=3),
    )
    person_id = await _principal_person_id(db_session, case.id)

    r = await client.post(
        f"/cases/{case.id}/persons/{person_id}/resend-invitation",
        headers=agent_headers(admin),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["invitation_resent"] is True
    assert body["client_space_state"] == "pending"  # le lien est de nouveau vivant
    assert datetime.fromisoformat(body["invitation_expires_at"]) > datetime.now(UTC)

    rows = await _invitations(db_session, case.id)
    assert len(rows) == 2
    old, new = rows
    assert old.id == dead.id
    assert old.status == "cancelled"  # l'ancien lien meurt avec son token
    assert new.status == "pending"
    assert new.token != old.token
    assert new.expires_at > datetime.now(UTC)
    assert new.email == principal.email  # MÊME adresse : c'est tout l'objet

    # Le mail est parti, avec le NOUVEAU token (l'ancien lien ne doit rien rouvrir).
    assert len(email.outbox) == 1
    sent = email.outbox[0]
    assert sent.to == principal.email
    assert new.token in sent.html
    assert old.token not in sent.html

    # Trace d'audit : le renvoi a son propre verbe, jamais "première invitation".
    types = (
        (
            await db_session.execute(
                select(ActivityLog.action_type).where(ActivityLog.case_id == case.id)
            )
        )
        .scalars()
        .all()
    )
    assert "case.invitation_resent" in types


# --- (2) refus nommé : l'espace est déjà actif --------------------------------------


async def test_resend_refused_when_the_space_is_already_active(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    principal = await make_expat_user(activated=True, email="active@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    person_id = await _principal_person_id(db_session, case.id)

    r = await client.post(
        f"/cases/{case.id}/persons/{person_id}/resend-invitation",
        headers=agent_headers(admin),
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "invitation.already_accepted"
    # Ni token émis, ni mail : réémettre sur un compte vivant serait un vecteur
    # de prise de contrôle par mail.
    assert await _invitations(db_session, case.id) == []
    assert email.outbox == []


async def test_resend_refused_on_a_person_without_account(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    principal = await make_expat_user(activated=False)
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    family = CasePerson(case_id=case.id, kind="family", full_name="Sans compte")
    db_session.add(family)
    await db_session.commit()

    r = await client.post(
        f"/cases/{case.id}/persons/{family.id}/resend-invitation",
        headers=agent_headers(admin),
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "person.no_account"
    assert email.outbox == []


# --- (3) garde-fou anti-rafale ------------------------------------------------------


async def test_two_resends_in_a_row_are_refused_by_the_cooldown(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> None:
    principal = await make_expat_user(activated=False, email="burst@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    await make_case_invitation(
        case=case,
        email=principal.email,
        created_at=datetime.now(UTC) - timedelta(days=20),
        expires_at=datetime.now(UTC) - timedelta(days=6),
    )
    person_id = await _principal_person_id(db_session, case.id)
    url = f"/cases/{case.id}/persons/{person_id}/resend-invitation"

    assert (await client.post(url, headers=agent_headers(admin))).status_code == 200
    second = await client.post(url, headers=agent_headers(admin))
    assert second.status_code == 429, second.text
    assert second.json()["code"] == "invitation.resend_too_soon"
    # Un seul mail, un seul token vivant : la rafale ne produit rien.
    assert len(email.outbox) == 1
    pending = [i for i in await _invitations(db_session, case.id) if i.status == "pending"]
    assert len(pending) == 1


# --- (4) l'état du lien, ses 3 cas, sur le détail ET la liste ------------------------


async def test_link_state_is_computed_in_the_three_cases(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> None:
    headers = agent_headers(admin)
    states: dict[str, ClientCase] = {}

    # (a) ACTIVE — le client a activé son espace.
    active_expat = await make_expat_user(activated=True, email="a-active@example.com")
    states["active"] = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=active_expat.id
    )
    # (b) PENDING — invitation vivante.
    pending_expat = await make_expat_user(activated=False, email="b-pending@example.com")
    states["pending"] = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=pending_expat.id
    )
    live = await make_case_invitation(
        case=states["pending"],
        email=pending_expat.email,
        expires_at=datetime.now(UTC) + timedelta(days=9),
    )
    # (c) EXPIRED — invitation pendante mais périmée.
    expired_expat = await make_expat_user(activated=False, email="c-expired@example.com")
    states["expired"] = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expired_expat.id
    )
    await make_case_invitation(
        case=states["expired"],
        email=expired_expat.email,
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )

    def principal_of(detail: dict) -> dict:
        return next(p for p in detail["persons"] if p["kind"] == "principal")

    # --- détail
    a = principal_of((await client.get(f"/cases/{states['active'].id}", headers=headers)).json())
    assert a["client_space_state"] == "active"
    assert a["activated_at"] is not None
    assert a["invitation_expires_at"] is None

    b = principal_of((await client.get(f"/cases/{states['pending'].id}", headers=headers)).json())
    assert b["client_space_state"] == "pending"
    assert b["activated_at"] is None
    assert datetime.fromisoformat(b["invitation_expires_at"]) == live.expires_at

    c = principal_of((await client.get(f"/cases/{states['expired'].id}", headers=headers)).json())
    assert c["client_space_state"] == "expired"
    assert c["invitation_expires_at"] is not None  # la date DIT depuis quand c'est mort

    # --- liste : la même règle, batchée pour toute la page
    listed = (await client.get("/cases", headers=headers)).json()["items"]
    by_id = {item["id"]: item["client_space_state"] for item in listed}
    for state, case in states.items():
        assert by_id[str(case.id)] == state


async def test_a_case_never_invited_reads_expired_not_pending(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    """Aucune ligne d'invitation = aucun lien utilisable : même verdict qu'un
    lien périmé (le seul chemin de retour est le renvoi), jamais 'en attente'."""
    principal = await make_expat_user(activated=False)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=principal.id)
    detail = (await client.get(f"/cases/{case.id}", headers=agent_headers(admin))).json()
    principal_row = next(p for p in detail["persons"] if p["kind"] == "principal")
    assert principal_row["client_space_state"] == "expired"
    assert principal_row["invitation_expires_at"] is None


# --- (5) jamais un mail vers une adresse de démo -------------------------------------


async def test_no_mail_ever_goes_to_a_demo_recipient(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> None:
    demo_address = demo_expat_email("acme-agency", 1)
    principal = await make_expat_user(activated=False, email=demo_address)
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, is_demo=True
    )
    await make_case_invitation(
        case=case,
        email=demo_address,
        created_at=datetime.now(UTC) - timedelta(days=30),
        expires_at=datetime.now(UTC) - timedelta(days=16),
    )
    person_id = await _principal_person_id(db_session, case.id)

    r = await client.post(
        f"/cases/{case.id}/persons/{person_id}/resend-invitation",
        headers=agent_headers(admin),
    )
    assert r.status_code == 200, r.text
    assert email.outbox == []  # supprimé AU PUITS : aucun chemin d'envoi n'y échappe


# --- (6) autorisation ---------------------------------------------------------------


async def test_resend_is_closed_to_an_expat_token(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    principal = await make_expat_user(activated=True)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=principal.id)
    person_id = await _principal_person_id(db_session, case.id)

    r = await client.post(
        f"/cases/{case.id}/persons/{person_id}/resend-invitation",
        headers=expat_headers(principal),
    )
    assert r.status_code == 401


async def test_resend_cannot_reach_another_agency_case(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agency: MakeAgency,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    foreign_principal = await make_expat_user(activated=False)
    foreign_case = await make_client_case(
        agency_id=(await make_agency()).id, principal_expat_user_id=foreign_principal.id
    )
    person_id = await _principal_person_id(db_session, foreign_case.id)

    r = await client.post(
        f"/cases/{foreign_case.id}/persons/{person_id}/resend-invitation",
        headers=agent_headers(admin),
    )
    assert r.status_code == 404
    assert email.outbox == []
