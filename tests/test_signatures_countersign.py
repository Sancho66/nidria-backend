"""Lot contreseing agence (30/07) — l'agence peut signer aussi.

Le modèle porte agency_countersigns ; le rôle provider « Agence » est
compté À PART (roles_count = rôles CLIENTS — les gardes exact-match
restent vraies telles quelles) ; le siège agence est un signature_signer
à agent_id (XOR personne) ; l'ordre part en GROUPES (clients 0, agence 1
— sonde : supporté par submitter) ; le TOUR de l'agence est app-enforced
(slug servi au seul agent résolu, quand tous les clients ont signé) ;
1 document envoyé = 1 crédit, contreseing compris."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Role
from src.core import email
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.signature_plugin import SOURCE_PDF, FakeProvider
from tests.test_signatures import _requests, _signers

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _countersign_template(
    client: AsyncClient,
    fake_provider: FakeProvider,
    headers: dict[str, str],
    *,
    client_roles: int = 2,
    name: str = "Contrat",
) -> dict:
    """Un modèle avec contreseing : rôles clients + « Agence », toutes
    zones posées (builder-sync simulé)."""
    r = await client.post(
        "/document-templates",
        headers=headers,
        data={"name": name, "agency_countersigns": "true"},
        files={"file": ("contrat.pdf", SOURCE_PDF, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    template = r.json()
    assert template["agency_countersigns"] is True
    fake_provider.default_roles = [f"Signataire {i + 1}" for i in range(client_roles)] + ["Agence"]
    r = await client.post(f"/document-templates/{template['id']}/builder-sync", headers=headers)
    assert r.status_code == 200, r.text
    synced = r.json()
    assert synced["fields_configured"] is True
    assert synced["roles_count"] == client_roles  # « Agence » compté À PART
    return synced


async def _signable_countersign_case(
    client: AsyncClient,
    admin: Agent,
    headers: dict[str, str],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    email_prefix: str,
) -> tuple[uuid.UUID, str]:
    """(case_id, progress_id) — étape « Contrat » each_person (2 clients)
    + contreseing, activée (envoi parti), validated_by none (autoclose)."""
    template = await _countersign_template(client, fake_provider, headers, client_roles=2)
    journey = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await client.post(
            f"/journeys/{journey['id']}/steps",
            headers=headers,
            json={"name": "Contrat", "validated_by_type": "none"},
        )
    ).json()
    r = await client.post(
        f"/journeys/{journey['id']}/steps/{step['id']}/requirements",
        headers=headers,
        json={
            "kind": "document",
            "reference": "Contrat",
            "scope": "each_person",
            "signature_required": True,
            "document_template_id": template["id"],
        },
    )
    assert r.status_code == 201, r.text
    principal = await make_expat_user(activated=True, email=f"{email_prefix}-p@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    r = await client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={"full_name": "Membre CS", "relationship": "associate"},
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": journey["id"]}
    )
    assert r.status_code == 201, r.text
    progress_id = r.json()[0]["id"]
    r = await client.patch(
        f"/cases/{case.id}/steps/{progress_id}", headers=headers, json={"status": "in_progress"}
    )
    assert r.status_code == 200, r.text
    return case.id, progress_id


async def _sign(client: AsyncClient, signer_id: uuid.UUID, secret: str) -> None:
    r = await client.post(
        "/webhooks/docuseal",
        headers={"X-Docuseal-Secret": secret},
        json={"event_type": "form.completed", "data": {"external_id": str(signer_id)}},
    )
    assert r.status_code == 200, r.text


# --- (1) le cycle complet : 2 clients + agence, ordre et tour respectés ---------------


async def test_full_cycle_two_clients_then_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    case_id, progress_id = await _signable_countersign_case(
        client, admin, headers, make_client_case, make_expat_user, fake_provider, "cycle"
    )
    admin_id = admin.id
    # L'envoi : 3 sièges, rôles et GROUPES d'ordre corrects, 1 seul crédit.
    sent = fake_provider.create_calls[0]
    assert sent["roles"] == ["Signataire 1", "Signataire 2", "Agence"]
    assert sent["orders"] == [0, 0, 1]
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert (balance["available"], balance["reserved"]) == (9, 1)
    request = (await _requests(db_session, case_id))[0]
    request_id = request.id
    signers = await _signers(db_session, request_id)
    clients = [s for s in signers if s.agent_id is None]
    agency_seat = next(s for s in signers if s.agent_id is not None)
    assert agency_seat.agent_id == admin_id  # l'owner du dossier, résolu
    client_ids = [s.id for s in clients]

    monkeypatch.setenv("DOCUSEAL_WEBHOOK_SECRET", "whsec-cs")
    get_settings.cache_clear()
    # Client 1 signe : pas encore le tour de l'agence, pas de mail de tour.
    await _sign(client, client_ids[0], "whsec-cs")
    assert all("à vous de signer" not in m.subject.lower() for m in email.outbox)
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    step = next(s for s in detail["progress"] if s["id"] == progress_id)
    (cs,) = step["countersigns"]
    assert cs["status"] == "pending"
    assert cs["my_turn"] is False
    assert cs["slug"] is None

    # Client 2 signe : LE TOUR — mail à l'agent résolu, slug servi à LUI.
    await _sign(client, client_ids[1], "whsec-cs")
    await db_session.rollback()
    turn_mails = [m for m in email.outbox if "à vous de signer" in m.subject.lower()]
    assert turn_mails and turn_mails[-1].to == admin.email
    db_session.expire_all()
    request = (await _requests(db_session, case_id))[0]
    assert request.status == "partially_signed"  # PAS complétée : l'agence manque
    # L'étape validated_by=none n'auto-clôt PAS tant que la demande est ouverte.
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    step = next(s for s in detail["progress"] if s["id"] == progress_id)
    assert step["status"] == "in_progress"
    (cs,) = step["countersigns"]
    assert cs["my_turn"] is True
    assert cs["slug"]  # le viewer EST l'agent résolu
    assert cs["agent_id"] == str(admin_id)
    # Un AUTRE agent de l'agence voit la ligne, jamais le slug.
    other = await make_agent(agency_id=admin.agency_id, role=system_roles["admin"])
    detail = (await client.get(f"/cases/{case_id}", headers=agent_headers(other))).json()
    step = next(s for s in detail["progress"] if s["id"] == progress_id)
    (cs_other,) = step["countersigns"]
    assert cs_other["my_turn"] is True
    assert cs_other["slug"] is None

    # L'agence signe : complétée, consume (1 seul crédit), autoclose.
    agency_seat_id = agency_seat.id
    await _sign(client, agency_seat_id, "whsec-cs")
    await db_session.rollback()
    db_session.expire_all()
    request = (await _requests(db_session, case_id))[0]
    assert request.status == "completed"
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert (balance["available"], balance["reserved"]) == (9, 0)  # consume, pas release
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    step = next(s for s in detail["progress"] if s["id"] == progress_id)
    assert step["status"] == "done"  # l'autoclose attendait le contreseing
    (cs,) = step["countersigns"]
    assert cs["status"] == "signed"
    assert cs["slug"] is None


async def test_expat_face_awaiting_agency_and_client_only_counts(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
    fake_provider: FakeProvider,
    give_credits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mini-lot exposition expat : le n/m est CLIENT-only (le siège agence
    n'y entre jamais), et awaiting_agency dit « tout le monde a signé,
    reste l'agence »."""
    from shared.models.expat_user import ExpatUser

    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    case_id, _pid = await _signable_countersign_case(
        client, admin, headers, make_client_case, make_expat_user, fake_provider, "exaw"
    )
    principal = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "exaw-p@example.com"))
    ).scalar_one()
    p_headers = expat_headers(principal)
    tasks = (await client.get(f"/expat/cases/{case_id}/signatures", headers=p_headers)).json()
    # 3 sièges sur la demande, mais le n/m ne compte QUE les 2 clients.
    assert (tasks[0]["request_signed_count"], tasks[0]["request_signer_total"]) == (0, 2)
    assert tasks[0]["awaiting_agency"] is False

    request = (await _requests(db_session, case_id))[0]
    signers = await _signers(db_session, request.id)
    client_ids = [s.id for s in signers if s.agent_id is None]
    monkeypatch.setenv("DOCUSEAL_WEBHOOK_SECRET", "whsec-exaw")
    get_settings.cache_clear()
    await _sign(client, client_ids[0], "whsec-exaw")
    tasks = (await client.get(f"/expat/cases/{case_id}/signatures", headers=p_headers)).json()
    assert (tasks[0]["request_signed_count"], tasks[0]["request_signer_total"]) == (1, 2)
    assert tasks[0]["awaiting_agency"] is False  # il manque encore un CLIENT

    await _sign(client, client_ids[1], "whsec-exaw")
    tasks = (await client.get(f"/expat/cases/{case_id}/signatures", headers=p_headers)).json()
    assert (tasks[0]["request_signed_count"], tasks[0]["request_signer_total"]) == (2, 2)
    assert tasks[0]["awaiting_agency"] is True  # ne manque QUE l'agence

    # L'agence signe : la tâche complétée n'attend plus personne.
    agency_seat_id = next(s.id for s in signers if s.agent_id is not None)
    await _sign(client, agency_seat_id, "whsec-exaw")
    tasks = (await client.get(f"/expat/cases/{case_id}/signatures", headers=p_headers)).json()
    assert tasks[0]["request_status"] == "completed"
    assert tasks[0]["awaiting_agency"] is False


# --- (2) sans contreseing : rien ne change (siège agence absent) ----------------------


async def test_without_countersign_no_agency_seat(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    from tests.test_signatures import _signable_case

    await give_credits(admin.agency_id, 10)
    case, progress_id = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="nocs",
    )
    case_id = case.id
    assert fake_provider.create_calls[0]["orders"] == [0, 0]  # tous parallèles
    request = (await _requests(db_session, case_id))[0]
    assert all(s.agent_id is None for s in await _signers(db_session, request.id))
    detail = (await client.get(f"/cases/{case_id}", headers=agent_headers(admin))).json()
    step = next(s for s in detail["progress"] if s["id"] == progress_id)
    assert step["countersigns"] == []
    # Face client : sans contreseing, awaiting_agency est false et le n/m
    # compte comme avant (2 clients).
    from shared.models.expat_user import ExpatUser

    principal = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "nocs-p@example.com"))
    ).scalar_one()
    tasks = (
        await client.get(f"/expat/cases/{case_id}/signatures", headers=expat_headers(principal))
    ).json()
    assert tasks[0]["awaiting_agency"] is False
    assert (tasks[0]["request_signed_count"], tasks[0]["request_signer_total"]) == (0, 2)


# --- (3) aucun contresignataire → 422 nommé -------------------------------------------


async def test_no_countersigner_refuses_activation(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Dossier sans owner + le seul porteur d'agency.manage désactivé →
    l'activation par un case_manager refuse : 422 signatures.no_countersigner,
    rien d'envoyé, aucun crédit bougé."""
    from datetime import UTC, datetime

    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    template = await _countersign_template(client, fake_provider, headers, client_roles=1)
    journey = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await client.post(
            f"/journeys/{journey['id']}/steps", headers=headers, json={"name": "Contrat"}
        )
    ).json()
    r = await client.post(
        f"/journeys/{journey['id']}/steps/{step['id']}/requirements",
        headers=headers,
        json={
            "kind": "document",
            "reference": "Contrat",
            "scope": "principal",
            "signature_required": True,
            "document_template_id": template["id"],
        },
    )
    assert r.status_code == 201, r.text
    manager = await make_agent(agency_id=admin.agency_id, role=system_roles["case_manager"])
    principal = await make_expat_user(activated=True, email="nosigner@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=None
    )
    manager_headers = agent_headers(manager)
    r = await client.post(
        f"/cases/{case.id}/journey",
        headers=manager_headers,
        json={"journey_template_id": journey["id"]},
    )
    assert r.status_code == 201, r.text
    progress_id = r.json()[0]["id"]
    # Le seul porteur d'agency.manage se désactive.
    admin_row = await db_session.get(Agent, admin.id)
    assert admin_row is not None
    admin_row.deactivated_at = datetime.now(UTC)
    await db_session.commit()
    r = await client.patch(
        f"/cases/{case.id}/steps/{progress_id}",
        headers=manager_headers,
        json={"status": "in_progress"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "signatures.no_countersigner"
    await db_session.rollback()
    assert fake_provider.create_calls == []
    balance = (await client.get("/agencies/me/signature-credits", headers=manager_headers)).json()
    assert (balance["available"], balance["reserved"]) == (10, 0)


# --- (4) personne tardive : SA demande porte AUSSI le contreseing ---------------------


async def test_late_person_request_carries_the_countersign_too(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Verdict (au rapport) : le livrable de CHAQUE document envoyé doit
    être complet — la demande partielle de la personne tardive porte donc
    aussi le siège agence (elle + Agence, groupes 0/1, SON crédit)."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    case_id, _pid = await _signable_countersign_case(
        client, admin, headers, make_client_case, make_expat_user, fake_provider, "late"
    )
    r = await client.post(
        f"/cases/{case_id}/persons",
        headers=headers,
        json={"full_name": "Tardif CS", "relationship": "associate"},
    )
    assert r.status_code == 201, r.text
    assert len(fake_provider.create_calls) == 2
    late = fake_provider.create_calls[1]
    assert late["roles"] == ["Signataire 1", "Agence"]
    assert late["orders"] == [0, 1]
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert (balance["available"], balance["reserved"]) == (8, 2)  # son crédit à elle


async def test_deleting_signable_requirement_cancels_its_live_request(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Mini-lot orphelins : supprimer l'exigence signable ANNULE sa demande
    vivante (archive provider + release du crédit) — et plus aucune face ne
    la montre (bloc contreseing agence, tâches client)."""
    from shared.models.expat_user import ExpatUser

    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    case_id, progress_id = await _signable_countersign_case(
        client, admin, headers, make_client_case, make_expat_user, fake_provider, "orph"
    )
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert (balance["available"], balance["reserved"]) == (9, 1)
    # Retrouver la définition via la demande (elle la référence) — puis la
    # supprimer par l'API.
    from shared.models.journey import JourneyTemplateStep
    from shared.models.step_requirement import StepRequirement

    request = (await _requests(db_session, case_id))[0]
    definition = await db_session.get(StepRequirement, request.step_requirement_id)
    assert definition is not None
    step_row = await db_session.get(JourneyTemplateStep, definition.step_id)
    assert step_row is not None
    journey_id, def_step_id, def_id = step_row.template_id, definition.step_id, definition.id
    r = await client.delete(
        f"/journeys/{journey_id}/steps/{def_step_id}/requirements/{def_id}",
        headers=headers,
    )
    assert r.status_code in (200, 204), r.text
    db_session.expire_all()
    # La vivante est annulée : archive provider + release du crédit.
    assert len(fake_provider.cancel_calls) == 1
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert (balance["available"], balance["reserved"]) == (10, 0)
    # Plus aucune face ne la montre.
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    step = next(s for s in detail["progress"] if s["id"] == progress_id)
    assert step["countersigns"] == []
    principal = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "orph-p@example.com"))
    ).scalar_one()
    tasks = (
        await client.get(f"/expat/cases/{case_id}/signatures", headers=expat_headers(principal))
    ).json()
    assert tasks == []


# --- (5) annulation : release, contreseing compris ------------------------------------


async def test_cancel_releases_with_countersign(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    case_id, progress_id = await _signable_countersign_case(
        client, admin, headers, make_client_case, make_expat_user, fake_provider, "ccl"
    )
    request = (await _requests(db_session, case_id))[0]
    request_id = str(request.id)
    r = await client.post(
        f"/cases/{case_id}/signature-requests/{request_id}/cancel", headers=headers
    )
    assert r.status_code == 200, r.text
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert (balance["available"], balance["reserved"]) == (10, 0)  # release
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    step = next(s for s in detail["progress"] if s["id"] == progress_id)
    assert step["countersigns"] == []  # une demande morte ne porte pas de ligne
