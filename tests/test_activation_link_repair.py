"""LOT B point 1 — le lien d'activation expiré se répare tout seul (14/08).

Constat prod : six clients de Domiciliation Bulgarie, invités entre le 6 et le
27 juillet, ne POUVAIENT plus entrer — le lien vit 14 jours, rien ne le leur
redit, et le clic ne donnait qu'un « Invalid or expired invitation token »
indistinct. Ici : l'expiration est NOMMÉE (elle a un correctif en un clic),
et le lien périmé sert de preuve d'invitation pour en obtenir un neuf.

Couvre aussi le point 4 : l'objet ne commence plus par « Nidria : », et
l'expéditeur AFFICHÉ est l'agence."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.invitation import CaseInvitation
from shared.models.rbac import Role
from src.core import email
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import MakeAgent
from tests.plugins.case_plugin import MakeCaseInvitation, MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")

RESEND = "/auth/expat/activate/resend"


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def expired_case(
    db_session: AsyncSession,
    admin: Agent,
    make_agency: MakeAgency,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> tuple[ClientCase, ExpatUser, CaseInvitation]:
    """Le cas de Rachid Semlali : invité il y a 38 jours, jamais activé,
    lien mort depuis trois semaines."""
    expat = await make_expat_user(activated=False, email="bloque@example.com")
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    invitation = await make_case_invitation(
        case=case,
        email=expat.email,
        # Envoyée il y a 38 jours, morte depuis 24 : la combinaison réelle (le
        # cooldown de renvoi se lit sur created_at, pas sur l'expiration).
        created_at=datetime.now(UTC) - timedelta(days=38),
        expires_at=datetime.now(UTC) - timedelta(days=24),
    )
    return case, expat, invitation


async def _invitations(db: AsyncSession, case_id: uuid.UUID) -> list[CaseInvitation]:
    return list(
        (
            await db.execute(
                select(CaseInvitation)
                .where(CaseInvitation.case_id == case_id)
                .order_by(CaseInvitation.created_at)
            )
        )
        .scalars()
        .all()
    )


async def test_an_expired_link_is_named_expired_not_just_invalid(
    client: AsyncClient, expired_case: tuple[ClientCase, ExpatUser, CaseInvitation]
) -> None:
    """Sans ce nom, le front ne peut pas proposer le correctif : « expiré »
    se répare en un clic, « inconnu » non."""
    _case, _expat, invitation = expired_case
    refused = await client.post(
        "/auth/expat/activate", json={"token": invitation.token, "password": "motdepasse1"}
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "invitation.expired"

    unknown = await client.post(
        "/auth/expat/activate", json={"token": "jamais-vu", "password": "motdepasse1"}
    )
    assert unknown.status_code == 400
    assert unknown.json()["code"] == "invitation.invalid"


async def test_the_expired_link_buys_a_new_one_and_that_one_works(
    client: AsyncClient,
    db_session: AsyncSession,
    expired_case: tuple[ClientCase, ExpatUser, CaseInvitation],
) -> None:
    """Le geste complet, de bout en bout : un clic, un mail, un lien neuf qui
    ACTIVE vraiment le compte."""
    case, expat, invitation = expired_case
    response = await client.post(RESEND, json={"token": invitation.token})
    assert response.status_code == 200, response.text

    rows = await _invitations(db_session, case.id)
    assert len(rows) == 2
    fresh = rows[-1]
    assert fresh.token != invitation.token
    assert fresh.expires_at > datetime.now(UTC)
    assert fresh.status == "pending"
    # L'ancien jeton est mort : un dossier ne porte jamais deux liens vivants.
    await db_session.refresh(invitation)
    assert invitation.status == "cancelled"

    assert len(email.outbox) == 1
    mail = email.outbox[0]
    assert mail.to == expat.email
    assert fresh.token in mail.body
    # POINT 4 : l'agence devant, nous derrière — dans l'objet ET dans le From.
    assert not mail.subject.startswith("Nidria")
    assert "vous a ouvert un espace de suivi" in mail.subject
    assert mail.sender is not None and mail.sender.startswith('"Test Agency"')

    activated = await client.post(
        "/auth/expat/activate", json={"token": fresh.token, "password": "motdepasse1"}
    )
    assert activated.status_code == 200, activated.text
    await db_session.refresh(expat)
    assert expat.activated_at is not None


async def test_the_public_route_is_bounded_by_a_cooldown(
    client: AsyncClient,
    db_session: AsyncSession,
    expired_case: tuple[ClientCase, ExpatUser, CaseInvitation],
) -> None:
    """Public par nature, donc borné : le deuxième appel répond PAREIL et
    n'envoie rien — l'endpoint ne devient pas un canal d'envoi."""
    case, _expat, invitation = expired_case
    first = await client.post(RESEND, json={"token": invitation.token})
    second = await client.post(RESEND, json={"token": invitation.token})
    assert first.json() == second.json()  # indiscernables
    assert len(email.outbox) == 1  # un seul mail
    assert len(await _invitations(db_session, case.id)) == 2  # aucune 3e invitation


async def test_the_answer_never_reveals_anything(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
    expired_case: tuple[ClientCase, ExpatUser, CaseInvitation],
) -> None:
    """Jeton inconnu et compte déjà actif : même 200, même corps, zéro mail —
    la route ne dit jamais si une invitation existe."""
    _case, _expat, invitation = expired_case
    reference = (await client.post(RESEND, json={"token": invitation.token})).json()
    email.outbox.clear()

    active = await make_expat_user(email="deja@example.com")  # activated=True par défaut
    active_case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=active.id
    )
    active_invitation = await make_case_invitation(case=active_case, email=active.email)

    for token in ("inconnu-total", active_invitation.token):
        answer = await client.post(RESEND, json={"token": token})
        assert answer.status_code == 200
        assert answer.json() == reference
    assert email.outbox == []  # ni sonde, ni envoi
