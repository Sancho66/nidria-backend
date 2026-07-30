"""Lot propagation (30/07) — le PATCH d'exigence descend dans les dossiers
en vol.

Règle : les lignes PENDING (jamais répondues) des instances ACTIVES suivent
la définition ; les répondues gardent leur snapshot (doctrine LOT 6 pour
l'acquis, levée pour le non-commencé). Chaîne d'effets par la mécanique
existante : devient signable → envoi (backfill durci, crédit) ; cesse →
annulation + release ; élargi → matérialisation (gel de composition), les
nouveaux passant par la mécanique tardive quand une demande vit déjà.
Verdict solde insuffisant : la propagation échoue proprement (422 typé),
le PATCH de définition reste ACQUIS ; rejouer le PATCH rejoue la
propagation, idempotente."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.signature_plugin import FakeProvider
from tests.test_signatures import _document_template

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _plain_doc_journey(
    client: AsyncClient, headers: dict[str, str], *, scope: str = "principal"
) -> tuple[str, str, str]:
    """(journey_id, step_id, requirement_id) — une étape avec UN document
    SIMPLE (le point de départ du scénario Mandat 2)."""
    journey = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await client.post(
            f"/journeys/{journey['id']}/steps", headers=headers, json={"name": "Contrat"}
        )
    ).json()
    req = (
        await client.post(
            f"/journeys/{journey['id']}/steps/{step['id']}/requirements",
            headers=headers,
            json={"kind": "document", "reference": "Mandat", "scope": scope},
        )
    ).json()
    return journey["id"], step["id"], req["id"]


async def _active_case(
    client: AsyncClient,
    admin: Agent,
    headers: dict[str, str],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    journey_id: str,
    email_addr: str,
    *,
    member: bool = False,
) -> tuple[ClientCase, str]:
    principal = await make_expat_user(activated=True, email=email_addr)
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    if member:
        r = await client.post(
            f"/cases/{case.id}/persons",
            headers=headers,
            json={"full_name": "Membre Prop", "relationship": "associate"},
        )
        assert r.status_code == 201, r.text
    r = await client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": journey_id}
    )
    assert r.status_code == 201, r.text
    progress_id = r.json()[0]["id"]
    r = await client.patch(
        f"/cases/{case.id}/steps/{progress_id}", headers=headers, json={"status": "in_progress"}
    )
    assert r.status_code == 200, r.text
    return case, progress_id


async def _rows(db: AsyncSession, progress_id: str) -> list[CaseStepRequirement]:
    return list(
        (
            await db.execute(
                select(CaseStepRequirement).where(
                    CaseStepRequirement.case_step_progress_id == uuid.UUID(progress_id)
                )
            )
        )
        .scalars()
        .all()
    )


# --- (1) le scénario Mandat 2 : doc simple → PATCH signable → le vol voit « Signer » --


async def test_mandat2_patch_signable_reaches_the_case_in_flight(
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
    journey_id, step_id, req_id = await _plain_doc_journey(client, headers)
    case, progress_id = await _active_case(
        client, admin, headers, make_client_case, make_expat_user, journey_id, "m2@example.com"
    )
    case_id = case.id
    doc_template = await _document_template(client, headers, roles=1)
    r = await client.patch(
        f"/journeys/{journey_id}/steps/{step_id}/requirements/{req_id}",
        headers=headers,
        json={"signature_required": True, "document_template_id": doc_template["id"]},
    )
    assert r.status_code == 200, r.text
    # La ligne du dossier en vol a suivi : signable, modèle snapshoté.
    db_session.expire_all()
    (row,) = await _rows(db_session, progress_id)
    assert row.signature_required is True
    assert str(row.document_template_id) == doc_template["id"]
    # L'envoi est parti par le backfill durci : 1 demande, crédit réservé.
    assert len(fake_provider.create_calls) == 1
    assert fake_provider.create_calls[0]["roles"] == ["Signataire 1"]
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert (balance["available"], balance["reserved"]) == (9, 1)
    # La timeline agence dit « à signer », demande vivante nommée.
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    step = next(s for s in detail["progress"] if s["id"] == progress_id)
    (req_state,) = step["requirements"]
    assert req_state["signature_required"] is True
    assert req_state["signature_status"] == "pending"
    assert req_state["signature_request_status"] == "sent"


# --- (2) l'acquis : une ligne répondue ne bouge jamais --------------------------------


async def test_answered_row_keeps_its_snapshot(
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
    journey_id, step_id, req_id = await _plain_doc_journey(client, headers)
    _case, progress_id = await _active_case(
        client, admin, headers, make_client_case, make_expat_user, journey_id, "acq@example.com"
    )
    # Le client répond (dépôt) AVANT le PATCH : status fait autorité.
    (row,) = await _rows(db_session, progress_id)
    row.status = "provided"
    await db_session.commit()
    doc_template = await _document_template(client, headers, roles=1)
    r = await client.patch(
        f"/journeys/{journey_id}/steps/{step_id}/requirements/{req_id}",
        headers=headers,
        json={"signature_required": True, "document_template_id": doc_template["id"]},
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    (row,) = await _rows(db_session, progress_id)
    assert row.signature_required is False  # l'acquis, intouché
    assert row.document_template_id is None
    assert row.status == "provided"
    assert fake_provider.create_calls == []  # rien à envoyer


# --- (3) cesse d'être signable : annulation + release ---------------------------------


async def test_unsignable_patch_cancels_the_live_request_and_releases(
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
    journey_id, step_id, req_id = await _plain_doc_journey(client, headers)
    doc_template = await _document_template(client, headers, roles=1)
    r = await client.patch(
        f"/journeys/{journey_id}/steps/{step_id}/requirements/{req_id}",
        headers=headers,
        json={"signature_required": True, "document_template_id": doc_template["id"]},
    )
    assert r.status_code == 200, r.text
    _case, progress_id = await _active_case(
        client, admin, headers, make_client_case, make_expat_user, journey_id, "uns@example.com"
    )
    assert len(fake_provider.create_calls) == 1  # l'activation a envoyé
    r = await client.patch(
        f"/journeys/{journey_id}/steps/{step_id}/requirements/{req_id}",
        headers=headers,
        json={"signature_required": False},
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    (row,) = await _rows(db_session, progress_id)
    assert row.signature_required is False
    assert row.document_template_id is None
    assert len(fake_provider.cancel_calls) == 1  # la vivante est annulée
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert (balance["available"], balance["reserved"]) == (10, 0)  # release


# --- (4) élargissement avec demande vivante : la mécanique tardive --------------------


async def test_widening_scope_seats_the_member_via_late_mechanic(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Principal déjà assis sur une vivante → le PATCH scope=each_person
    matérialise la ligne du membre (gel de composition) et l'assoit par la
    mécanique TARDIVE (sa demande partielle, son crédit) — jamais le send
    groupé, que la garde exact-match refuserait."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    journey_id, step_id, req_id = await _plain_doc_journey(client, headers)
    doc_template = await _document_template(client, headers, roles=1)
    r = await client.patch(
        f"/journeys/{journey_id}/steps/{step_id}/requirements/{req_id}",
        headers=headers,
        json={"signature_required": True, "document_template_id": doc_template["id"]},
    )
    assert r.status_code == 200, r.text
    _case, progress_id = await _active_case(
        client,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        journey_id,
        "wide@example.com",
        member=True,
    )
    assert len(fake_provider.create_calls) == 1  # le principal, à l'activation
    r = await client.patch(
        f"/journeys/{journey_id}/steps/{step_id}/requirements/{req_id}",
        headers=headers,
        json={"scope": "each_person"},
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    rows = await _rows(db_session, progress_id)
    assert len(rows) == 2  # la ligne du membre est née
    assert all(r.signature_required for r in rows)
    assert all(r.scope == "each_person" for r in rows)
    # Une DEUXIÈME demande (partielle, à lui) — la première vit toujours.
    assert len(fake_provider.create_calls) == 2
    assert fake_provider.cancel_calls == []
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert (balance["available"], balance["reserved"]) == (8, 2)


# --- (5) le verdict : solde insuffisant, le PATCH définition reste acquis -------------


async def test_insufficient_credits_fails_cleanly_definition_acquired(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    headers = agent_headers(admin)
    journey_id, step_id, req_id = await _plain_doc_journey(client, headers)
    _case, progress_id = await _active_case(
        client, admin, headers, make_client_case, make_expat_user, journey_id, "nocred@example.com"
    )
    doc_template = await _document_template(client, headers, roles=1)
    r = await client.patch(
        f"/journeys/{journey_id}/steps/{step_id}/requirements/{req_id}",
        headers=headers,
        json={"signature_required": True, "document_template_id": doc_template["id"]},
    )
    # La propagation échoue proprement (crédit), erreur typée…
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "signatures.credits_insufficient"
    await db_session.rollback()
    # …le PATCH de définition reste ACQUIS…
    definition = (
        await client.get(f"/journeys/{journey_id}/steps/{step_id}/requirements", headers=headers)
    ).json()
    assert definition[0]["signature_required"] is True
    # …et la ligne du dossier n'a PAS bougé (rollback de la propagation).
    (row,) = await _rows(db_session, progress_id)
    assert row.signature_required is False
    assert fake_provider.create_calls == []
    # Rejouer le PATCH après achat : la propagation repart, tout descend.
    await give_credits(admin.agency_id, 5)
    r = await client.patch(
        f"/journeys/{journey_id}/steps/{step_id}/requirements/{req_id}",
        headers=headers,
        json={"signature_required": True, "document_template_id": doc_template["id"]},
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    (row,) = await _rows(db_session, progress_id)
    assert row.signature_required is True
    assert len(fake_provider.create_calls) == 1


# --- (6) idempotence : PATCH rejoué, ni double envoi ni double ligne ------------------


async def test_replayed_patch_sends_nothing_twice(
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
    journey_id, step_id, req_id = await _plain_doc_journey(client, headers)
    _case, progress_id = await _active_case(
        client, admin, headers, make_client_case, make_expat_user, journey_id, "idem@example.com"
    )
    doc_template = await _document_template(client, headers, roles=1)
    payload = {"signature_required": True, "document_template_id": doc_template["id"]}
    for _ in range(2):
        r = await client.patch(
            f"/journeys/{journey_id}/steps/{step_id}/requirements/{req_id}",
            headers=headers,
            json=payload,
        )
        assert r.status_code == 200, r.text
    db_session.expire_all()
    rows = await _rows(db_session, progress_id)
    assert len(rows) == 1  # jamais une double ligne
    assert len(fake_provider.create_calls) == 1  # jamais un double envoi
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert (balance["available"], balance["reserved"]) == (9, 1)
