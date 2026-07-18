"""Demi-lot anti-burst (décision 2026-07-18, retour Nicolas) : le kickoff
UNIQUE à l'assignation (la liste groupée par étape), la fenêtre 30 min par
(dossier, destinataire) sur les mails d'activation ET les commentaires, le
fil de l'eau qui reste unitaire, et des fenêtres jamais partagées entre
destinataires."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.notification_window import NotificationWindow
from shared.models.rbac import Role
from src.core import email
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")

KICKOFF_SUBJECT = "votre parcours démarre"
REQUEST_SUBJECT = "informations sont attendues"


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"], first_name="Marie", last_name="Conseil")


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="burst-client@example.com")


def _mails_to(to: str) -> list[email.OutboxEmail]:
    return [m for m in email.outbox if m.to == to]


async def _template_two_steps(client: AsyncClient, headers: dict[str, str]) -> tuple[str, str, str]:
    """Deux étapes SANS prérequis, chacune avec une exigence concrète."""
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    ids = []
    for name in ("Dossier initial", "Justificatifs"):
        sid = await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": name})
        ids.append(sid.json()["id"])
        r = await client.post(
            f"/journeys/{tid}/steps/{ids[-1]}/requirements",
            headers=headers,
            json={"kind": "document", "reference": f"Piece {name}", "scope": "principal"},
        )
        assert r.status_code == 201
    return tid, ids[0], ids[1]


async def _expire_window(db: AsyncSession, case_id) -> None:
    await db.execute(
        update(NotificationWindow)
        .where(NotificationWindow.case_id == case_id)
        .values(last_sent_at=datetime.now(UTC) - timedelta(minutes=31))
    )
    await db.commit()


async def test_assignment_sends_one_kickoff_with_the_list(
    client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """L'assignation multi-étapes = UN email, la liste groupée par étape ;
    les démarrages qui suivent dans la fenêtre n'ajoutent RIEN."""
    ah = agent_headers(admin)
    tid, _, _ = await _template_two_steps(client, ah)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    email.outbox.clear()
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    mails = _mails_to(expat.email)
    assert len(mails) == 1  # UN envoi au lieu de N
    assert KICKOFF_SUBJECT in mails[0].subject
    assert "Dossier initial" in mails[0].body and "Justificatifs" in mails[0].body

    # Le burst réel : l'agent démarre les 2 étapes dans la foulée → zéro mail.
    email.outbox.clear()
    for s in steps:
        r = await client.patch(
            f"/cases/{case.id}/steps/{s['id']}", headers=ah, json={"status": "in_progress"}
        )
        assert r.status_code == 200
    assert _mails_to(expat.email) == []


async def test_isolated_activation_stays_unitary(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Le fil de l'eau : une activation hors fenêtre = l'email unitaire
    existant (requirement_request), inchangé."""
    ah = agent_headers(admin)
    tid, s1, _ = await _template_two_steps(client, ah)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    await _expire_window(db_session, case.id)  # la fenêtre du kickoff est passée
    email.outbox.clear()
    r = await client.patch(
        f"/cases/{case.id}/steps/{steps[0]['id']}", headers=ah, json={"status": "in_progress"}
    )
    assert r.status_code == 200
    mails = _mails_to(expat.email)
    assert len(mails) == 1
    assert REQUEST_SUBJECT in mails[0].subject  # l'unitaire, pas le kickoff


async def test_two_comments_two_steps_five_minutes_one_mail(
    client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """La fenêtre commentaires est par DOSSIER : deux étapes commentées à
    5 min d'écart = UN email."""
    ah = agent_headers(admin)
    tid, _, _ = await _template_two_steps(client, ah)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    email.outbox.clear()
    for s in steps:  # un commentaire sur CHAQUE étape, coup sur coup
        r = await client.post(
            f"/cases/{case.id}/steps/{s['id']}/comments", headers=ah, json={"body": "hello"}
        )
        assert r.status_code in (200, 201)
    assert len(_mails_to(expat.email)) == 1  # une seule notification


async def test_windows_are_per_recipient(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """Deux destinataires ne partagent JAMAIS une fenêtre : le mail au
    client n'absorbe pas celui de l'owner (et réciproquement) — la clé est
    (dossier, email destinataire, catégorie)."""
    ah = agent_headers(admin)
    tid, _, _ = await _template_two_steps(client, ah)
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    email.outbox.clear()
    # agent → client (ouvre la fenêtre du CLIENT sur ce dossier)
    await client.post(
        f"/cases/{case.id}/steps/{steps[0]['id']}/comments", headers=ah, json={"body": "a"}
    )
    assert len(_mails_to(expat.email)) == 1
    # client → agent, dans la même minute : la fenêtre du client ne couvre
    # PAS l'owner — son mail part.
    r = await client.post(
        f"/expat/cases/{case.id}/steps/{steps[0]['id']}/comments",
        headers=expat_headers(expat),
        json={"body": "reponse"},
    )
    assert r.status_code == 201, r.text
    assert len(_mails_to(admin.email)) == 1
    # et les deux fenêtres coexistent en base, une par destinataire
    rows = (
        (
            await db_session.execute(
                select(NotificationWindow).where(NotificationWindow.case_id == case.id)
            )
        )
        .scalars()
        .all()
    )
    assert {(r.recipient_email, r.category) for r in rows} >= {
        (expat.email, "comments"),
        (admin.email, "comments"),
    }
