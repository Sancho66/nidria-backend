"""NID-24 — le parcours redevient optionnel à la création d'un dossier.

Renverse la décision du 11/07 (parcours requis) : un prospect n'a pas
toujours de parcours défini à l'entrée. La crainte d'alors — « assigner
plus tard » produisait des dossiers morts — se traite en SURFAÇANT, pas en
bloquant : la fiche remplace la timeline par l'appel à l'action d'assignation
(AssignJourneyCard → POST /cases/{id}/journey, chemin préexistant réutilisé
tel quel), la liste marque les dossiers sans parcours.

Couvre : création sans parcours (201, zéro étape, invitation au libellé
neutre), fiche et liste lisibles, espace client explicite (timeline vide,
pas un écran cassé), assignation ensuite (étapes créées, re-POST → 409
inchangé), jobs muets sur un dossier sans parcours, et la non-régression
du dossier AVEC parcours.
"""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.rbac import Role
from src.core import email
from src.digest.digest_job import run_notification_digest
from src.reminders.reminders_jobs import create_auto_reminders
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _payload(email_addr: str, **overrides: object) -> dict[str, object]:
    return {
        "first_name": "Nora",
        "last_name": "Prospect",
        "email": email_addr,
        "origin_country": "FR",
        "dest_country": "PT",
        **overrides,
    }


# --- (1) création sans parcours : dossier vivant, zéro étape -------------------------


async def test_create_without_journey_yields_a_readable_case(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    resp = await client.post("/cases", headers=headers, json=_payload("nojourney@example.com"))
    assert resp.status_code == 201, resp.text
    case_id = resp.json()["id"]
    assert resp.json()["journey_template_id"] is None

    # Zéro étape instanciée.
    steps = (
        (
            await db_session.execute(
                select(CaseStepProgress).where(CaseStepProgress.case_id == case_id)
            )
        )
        .scalars()
        .all()
    )
    assert steps == []

    # L'invitation part quand même — au libellé NEUTRE (pas de nom de
    # parcours à citer, pas de « None » qui fuit).
    assert len(email.outbox) == 1
    assert email.outbox[0].to == "nojourney@example.com"
    assert "None" not in email.outbox[0].body

    # La fiche est lisible : progress vide, journey_name absent — c'est le
    # contrat sur lequel le front affiche l'appel à l'action d'assignation.
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    assert detail["progress"] == []
    assert detail["journey_name"] is None
    assert detail["current_step_name"] is None

    # La liste : pas de parcours, pas d'étape courante, urgence neutre —
    # aucun calcul absurde sur un dossier sans étapes.
    items = (await client.get("/cases", headers=headers)).json()["items"]
    row = next(i for i in items if i["id"] == case_id)
    assert row["journey_name"] is None
    assert row["current_step_name"] is None
    assert row["urgency"] == "neutral"


# --- (2) l'espace client dit « pas encore de parcours », il ne casse pas -------------


async def test_expat_space_shows_an_explicit_empty_timeline(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
) -> None:
    principal = await make_expat_user(activated=True, email="space-nojourney@example.com")
    resp = await client.post(
        "/cases",
        headers=agent_headers(admin),
        json=_payload("space-nojourney@example.com"),
    )
    assert resp.status_code == 201
    case_id = resp.json()["id"]

    detail = (await client.get(f"/expat/cases/{case_id}", headers=expat_headers(principal))).json()
    # timeline [] est le contrat : le front rend l'état explicite « votre
    # agence prépare votre parcours » (TimelinePage, timeline.noJourney*),
    # pas un écran vide. Les compteurs restent cohérents (0/0).
    assert detail["timeline"] == []
    summary = (await client.get("/expat/cases", headers=expat_headers(principal))).json()
    mine = next(s for s in summary if s["id"] == case_id)
    assert (mine["steps_done"], mine["steps_total"]) == (0, 0)


# --- (3) assigner ensuite : le chemin préexistant, réutilisé -------------------------


async def test_assign_later_instantiates_the_steps(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    case_id = (
        await client.post("/cases", headers=headers, json=_payload("later@example.com"))
    ).json()["id"]
    tid = (await client.post("/journeys", headers=headers, json={"name": "Setup"})).json()["id"]
    await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "Ouverture"})
    await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "Depot"})

    r = await client.post(
        f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
    )
    assert r.status_code == 201, r.text
    assert len(r.json()) == 2  # la timeline, immédiatement

    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    assert detail["journey_template_id"] == tid
    assert [s["name"] for s in detail["progress"]] == ["Ouverture", "Depot"]

    # Re-POST sur un dossier qui a déjà son parcours : 409 inchangé (le
    # remplacement mid-flight reste une opération V1.5, jamais un écrasement
    # silencieux — aucune étape recréée, aucune donnée perdue).
    again = await client.post(
        f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
    )
    assert again.status_code == 409


# --- (4) les jobs ignorent proprement un dossier sans parcours -----------------------


async def test_jobs_stay_silent_on_a_journey_less_case(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    sync_session_local: sessionmaker[Session],
) -> None:
    await make_expat_user(activated=True, email="jobs-nojourney@example.com")
    resp = await client.post(
        "/cases", headers=agent_headers(admin), json=_payload("jobs-nojourney@example.com")
    )
    assert resp.status_code == 201

    # Relances auto : sans étape il n'y a pas de candidat — 0 créé, 0 écarté,
    # aucune erreur (le join sur le parcours exclut le dossier en amont).
    with sync_session_local() as db:
        stats = create_auto_reminders(db, log=lambda _: None)
    assert stats["created"] == 0
    assert stats["skipped_no_client_space"] == 0

    # Digest : aucun évènement d'étape possible — run muet, sans erreur.
    email.outbox.clear()
    with sync_session_local() as db:
        dstats = run_notification_digest(
            db, log=lambda _: None, now=datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
        )
    assert dstats["mails"] == 0
    assert email.outbox == []


# --- (5) non-régression : le dossier AVEC parcours est inchangé ----------------------


async def test_create_with_journey_still_instantiates_in_one_call(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S1"})
    resp = await client.post(
        "/cases",
        headers=headers,
        json=_payload("withjourney@example.com", journey_template_id=tid),
    )
    assert resp.status_code == 201, resp.text
    detail = (await client.get(f"/cases/{resp.json()['id']}", headers=headers)).json()
    assert len(detail["progress"]) == 1
