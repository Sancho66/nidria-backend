"""Lot 10/08 — LES DEUX MURS : l'essai attend, l'annulation ferme.

Le mur de facturation cesse d'être un bloc : il se dédouble selon la
RAISON, parce que les deux situations n'ont pas le même avenir.

- `trial_expired` (et `past_due`) : l'agence est ATTENDUE — elle va
  convertir, ou refaire sa carte. Les faces extérieures restent OUVERTES :
  le prestataire dépose, le client valide, et les dépôts qui s'empilent
  sous les yeux d'agents en lecture seule sont l'argument commercial.
- `canceled` : l'agence est PARTIE. Les faces extérieures ferment aussi —
  laisser un notaire déposer dans un espace mort ne fabrique qu'un
  document orphelin que personne ne lira jamais. Message NEUTRE : ni le
  prestataire ni le client n'apprennent que l'agence n'a pas payé.
- Le superadmin reste exempt des DEUX (la sortie humaine).

La face expat se gère PAR DOSSIER, jamais par personne : un ExpatUser n'a
pas d'agency_id (il peut avoir des dossiers chez plusieurs agences) — le
test le plus important de ce fichier est celui-là.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

_BOGUS = uuid.UUID("00000000-0000-0000-0000-000000000009")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def external_agent(make_agent: MakeAgent, admin: Agent, db_session: AsyncSession) -> Agent:
    role = (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()
    return await make_agent(
        agency_id=admin.agency_id, role=role, is_external=True, email="notaire@ext.com"
    )


async def _expire_trial(db: AsyncSession, agency_id: uuid.UUID) -> None:
    await db.execute(
        update(Agency)
        .where(Agency.id == agency_id)
        .values(trial_ends_at=datetime.now(UTC) - timedelta(days=1))
    )
    await db.commit()


async def _cancel(db: AsyncSession, agency_id: uuid.UUID) -> None:
    """L'échéance atteinte : Paddle a posé `canceled` (l'agence est partie)."""
    await db.execute(
        update(Agency)
        .where(Agency.id == agency_id)
        .values(
            plan="cabinet",
            billing_cycle="mensuel",
            converted_at=datetime.now(UTC) - timedelta(days=60),
            billing_mode="paddle",
            billing_status="canceled",
            paddle_subscription_id="sub_gone",
        )
    )
    await db.commit()


# --- LE MUR EXTERNE : l'essai laisse passer, l'annulation ferme ------------------------


async def test_external_provider_keeps_working_on_an_expired_trial(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_agent: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Essai expiré : le prestataire n'est PAS bloqué (son geste n'est pas
    la faute de l'agence, et le dépôt qui s'empile fait pression). Sa
    requête traverse le mur et va jusqu'au manager, qui répond sur le
    fond — jamais le 403 du mur."""
    await _expire_trial(db_session, admin.agency_id)

    passed = await client.post(
        f"/external/cases/{_BOGUS}/steps/{_BOGUS}/comments",
        headers=agent_headers(external_agent),
        json={"body": "Acte prêt"},
    )
    assert passed.status_code != 403, passed.text  # le mur ne s'est pas levé


async def test_external_provider_is_closed_on_a_canceled_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_agent: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Annulation : le prestataire ferme AUSSI — avec un message neutre
    qui ne dit rien de la facturation de l'agence."""
    await _cancel(db_session, admin.agency_id)

    refused = await client.post(
        f"/external/cases/{_BOGUS}/steps/{_BOGUS}/comments",
        headers=agent_headers(external_agent),
        json={"body": "Acte prêt"},
    )
    assert refused.status_code == 403, refused.text
    body = refused.json()
    assert body["code"] == "billing.workspace_inactive"
    assert body["detail"] == "Cet espace n'est plus actif — contactez votre interlocuteur."
    assert "abonnement" not in body["detail"].lower()  # jamais un mot de facturation
    assert body["params"]["reason"] == "canceled"

    # Et ses LECTURES restent ouvertes : le mur ne ferme que les écritures.
    read = await client.get(
        f"/external/cases/{_BOGUS}/documents", headers=agent_headers(external_agent)
    )
    assert read.status_code != 403, read.text


# --- LE MUR CLIENT : par DOSSIER, jamais par personne ----------------------------------


async def test_expat_keeps_writing_on_an_expired_trial(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat_user: ExpatUser,
    make_client_case,
    expat_headers: AuthHeaders,
) -> None:
    """Essai expiré : le client dépose et valide comme avant."""
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat_user.id, owner_agent_id=admin.id
    )
    await _expire_trial(db_session, admin.agency_id)

    passed = await client.put(
        f"/expat/cases/{case.id}/requirements/{_BOGUS}",
        headers=expat_headers(expat_user),
        json={"fulfilled": True},
    )
    assert passed.status_code != 403, passed.text  # le mur ne s'est pas levé


async def test_expat_is_closed_on_a_canceled_agency_but_only_there(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat_user: ExpatUser,
    make_agent: MakeAgent,
    make_client_case,
    system_roles: dict[str, Role],
    expat_headers: AuthHeaders,
) -> None:
    """LE TEST QUI DÉCIDE DE LA JUSTESSE : un expatrié n'appartient à
    AUCUNE agence — il peut avoir des dossiers chez plusieurs. Le mur se
    pose donc sur le DOSSIER : son espace chez l'agence partie ferme, son
    dossier chez l'agence active continue. Un blocage par personne aurait
    fermé les deux."""
    gone_case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat_user.id, owner_agent_id=admin.id
    )
    other_admin = await make_agent(role=system_roles["admin"], email="autre@example.com")
    living_case = await make_client_case(
        agency_id=other_admin.agency_id,
        principal_expat_user_id=expat_user.id,
        owner_agent_id=other_admin.id,
    )
    await _cancel(db_session, admin.agency_id)
    headers = expat_headers(expat_user)

    refused = await client.put(
        f"/expat/cases/{gone_case.id}/requirements/{_BOGUS}",
        headers=headers,
        json={"fulfilled": True},
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "billing.workspace_inactive"
    assert refused.json()["detail"] == (
        "Cet espace n'est plus actif — contactez votre interlocuteur."
    )

    # L'AUTRE agence, bien vivante : le client y travaille sans rien voir.
    living = await client.put(
        f"/expat/cases/{living_case.id}/requirements/{_BOGUS}",
        headers=headers,
        json={"fulfilled": True},
    )
    assert living.status_code != 403, living.text

    # Ses lectures chez l'agence partie restent ouvertes (rien n'est caché).
    assert (await client.get("/expat/cases", headers=headers)).status_code == 200
    assert (await client.get(f"/expat/cases/{gone_case.id}", headers=headers)).status_code == 200


async def test_expat_self_endpoints_survive_a_canceled_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat_user: ExpatUser,
    make_client_case,
    expat_headers: AuthHeaders,
) -> None:
    """Les gestes qui ne nomment aucun dossier (session, mot de passe,
    consentements, profil) ne sont pas agency-scopés : ils passent, comme
    l'allowlist côté agent. On ne verrouille jamais quelqu'un hors de son
    propre compte."""
    await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat_user.id, owner_agent_id=admin.id
    )
    await _cancel(db_session, admin.agency_id)
    headers = expat_headers(expat_user)

    profile = await client.patch("/profile/expat", headers=headers, json={"first_name": "Léa"})
    assert profile.status_code != 403, profile.text
    logout = await client.post("/auth/expat/logout", headers=headers)
    assert logout.status_code != 403, logout.text


# --- LA SORTIE HUMAINE : exempte des deux murs -----------------------------------------


async def test_superadmin_is_exempt_from_both_walls(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Le superadmin écrit sur une agence en essai expiré ET sur une
    agence annulée — c'est la main qui répare, elle ne se verrouille
    jamais dehors."""
    superadmin = await make_agent(role=system_roles["superadmin"])
    headers = agent_headers(superadmin)

    await _expire_trial(db_session, superadmin.agency_id)
    on_trial = await client.post("/journeys", headers=headers, json={"name": "Essai expiré"})
    assert on_trial.status_code == 201, on_trial.text

    await _cancel(db_session, superadmin.agency_id)
    on_canceled = await client.post("/journeys", headers=headers, json={"name": "Annulée"})
    assert on_canceled.status_code == 201, on_canceled.text
