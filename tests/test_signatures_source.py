"""LOT 6 — source du document + deux gardes.

1. Le PDF de l'agence est LA source : uploadé sur l'exigence signable du
   template, snapshoté à la matérialisation (une ré-upload ne touche jamais
   un dossier en vol), transmis au provider en octets. Sans PDF → 422 nommé
   à l'assignation ET à l'envoi — on ne signe JAMAIS un document vide.
2. Sémantique du flag verrouillée : l'env est MAÎTRE (off = off pour toute
   agence quoi que dise son réglage) ; le réglage agence n'est qu'un
   sous-interrupteur de rollout (env on + agence off = pas d'envoi).
3. Le « Signé n/m » sur les réponses espace client.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.signature_plugin import SOURCE_PDF, FakeProvider
from tests.test_signatures import _requests, _signable_case, _signers

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")

SOURCE_PDF_V2 = b"%PDF-1.4 contrat source agence V2"


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _own_journey(
    client: AsyncClient, headers: dict[str, str], *, with_pdf: bool = True
) -> tuple[str, str, str]:
    """(template_id, step_id, requirement_id) — un parcours à UNE exigence
    signable, PDF uploadé (ou pas)."""
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await client.post(
            f"/journeys/{template['id']}/steps", headers=headers, json={"name": "Contrat"}
        )
    ).json()
    req = (
        await client.post(
            f"/journeys/{template['id']}/steps/{step['id']}/requirements",
            headers=headers,
            json={
                "kind": "document",
                "reference": "Statuts",
                "scope": "principal",
                "signature_required": True,
            },
        )
    ).json()
    if with_pdf:
        up = await client.post(
            f"/journeys/{template['id']}/steps/{step['id']}/requirements/{req['id']}"
            "/signature-document",
            headers=headers,
            files={"file": ("statuts.pdf", SOURCE_PDF, "application/pdf")},
        )
        assert up.status_code == 200, up.text
        assert up.json()["signature_document_filename"] == "statuts.pdf"
    return template["id"], step["id"], req["id"]


async def _case_on(
    client: AsyncClient,
    admin: Agent,
    headers: dict[str, str],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    template_id: str,
    email_addr: str,
    *,
    activate: bool = True,
) -> tuple[ClientCase, str]:
    principal = await make_expat_user(activated=True, email=email_addr)
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    timeline_resp = await client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": template_id}
    )
    assert timeline_resp.status_code == 201, timeline_resp.text
    progress_id = timeline_resp.json()[0]["id"]
    if activate:
        r = await client.patch(
            f"/cases/{case.id}/steps/{progress_id}", headers=headers, json={"status": "in_progress"}
        )
        assert r.status_code == 200, r.text
    return case, progress_id


async def _signable_row(db: AsyncSession, progress_id: str) -> CaseStepRequirement:
    return (
        await db.execute(
            select(CaseStepRequirement).where(
                CaseStepRequirement.case_step_progress_id == uuid.UUID(progress_id),
                CaseStepRequirement.signature_required.is_(True),
            )
        )
    ).scalar_one()


# --- (1) la source : snapshot + octets au provider -----------------------------------


async def test_pdf_is_snapshotted_and_sent_to_the_provider(
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
    template_id, _, _ = await _own_journey(client, headers)
    _case, progress_id = await _case_on(
        client, admin, headers, make_client_case, make_expat_user, template_id, "src@example.com"
    )
    row = await _signable_row(db_session, progress_id)
    assert row.signature_document_path is not None
    assert row.signature_document_filename == "statuts.pdf"
    # Le provider a reçu LES OCTETS du PDF de l'agence.
    assert fake_provider.create_calls[0]["document_pdf"] == SOURCE_PDF
    assert fake_provider.create_calls[0]["document_filename"] == "statuts.pdf"


async def test_reupload_never_touches_a_case_in_flight(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Ré-upload = NOUVEAU chemin : le dossier déjà activé garde SON
    snapshot ; le dossier suivant prend la V2 (et le provider ses octets)."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    template_id, step_id, req_id = await _own_journey(client, headers)
    _case_a, progress_a = await _case_on(
        client, admin, headers, make_client_case, make_expat_user, template_id, "reup-a@example.com"
    )
    row_a = await _signable_row(db_session, progress_a)
    old_path = row_a.signature_document_path

    up = await client.post(
        f"/journeys/{template_id}/steps/{step_id}/requirements/{req_id}/signature-document",
        headers=headers,
        files={"file": ("statuts-v2.pdf", SOURCE_PDF_V2, "application/pdf")},
    )
    assert up.status_code == 200, up.text

    _case_b, progress_b = await _case_on(
        client, admin, headers, make_client_case, make_expat_user, template_id, "reup-b@example.com"
    )
    db_session.expire_all()
    row_a = await _signable_row(db_session, progress_a)
    row_b = await _signable_row(db_session, progress_b)
    assert row_a.signature_document_path == old_path  # le vol garde sa version
    assert row_b.signature_document_path != old_path
    assert row_b.signature_document_filename == "statuts-v2.pdf"
    assert fake_provider.create_calls[-1]["document_pdf"] == SOURCE_PDF_V2


# --- (1bis) jamais un document vide : les deux gardes 422 ----------------------------


async def test_assignment_refuses_signable_without_pdf(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
) -> None:
    headers = agent_headers(admin)
    template_id, _, _ = await _own_journey(client, headers, with_pdf=False)
    principal = await make_expat_user(activated=True, email="nopdf@example.com")
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=principal.id)
    r = await client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": template_id}
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "journey.signature_document_missing"
    assert r.json()["params"]["reference"] == "Statuts"


async def test_send_guard_catches_a_requirement_added_after_assignment(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """L'exigence signable AJOUTÉE sans PDF sur un template déjà assigné
    (étape active) : le backfill voudrait envoyer → la garde d'envoi refuse,
    même 422 nommé — la défense est structurelle, pas seulement à
    l'assignation."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await client.post(
            f"/journeys/{template['id']}/steps", headers=headers, json={"name": "Contrat"}
        )
    ).json()
    _case, _pid = await _case_on(
        client,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        template["id"],
        "late-def@example.com",
    )
    r = await client.post(
        f"/journeys/{template['id']}/steps/{step['id']}/requirements",
        headers=headers,
        json={
            "kind": "document",
            "reference": "Avenant",
            "scope": "principal",
            "signature_required": True,
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "journey.signature_document_missing"
    assert fake_provider.create_calls == []


async def test_upload_gardes(client: AsyncClient, admin: Agent, agent_headers: AuthHeaders) -> None:
    headers = agent_headers(admin)
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await client.post(f"/journeys/{template['id']}/steps", headers=headers, json={"name": "S"})
    ).json()
    plain = (
        await client.post(
            f"/journeys/{template['id']}/steps/{step['id']}/requirements",
            headers=headers,
            json={"kind": "document", "reference": "Kbis", "scope": "principal"},
        )
    ).json()
    # Un PDF sur une exigence NON signable → 422 nommé.
    r = await client.post(
        f"/journeys/{template['id']}/steps/{step['id']}/requirements/{plain['id']}"
        "/signature-document",
        headers=headers,
        files={"file": ("x.pdf", SOURCE_PDF, "application/pdf")},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "journey.signature_document_on_non_signable"
    # Un non-PDF sur une signable → 422 nommé.
    signable = (
        await client.post(
            f"/journeys/{template['id']}/steps/{step['id']}/requirements",
            headers=headers,
            json={
                "kind": "document",
                "reference": "Statuts",
                "scope": "principal",
                "signature_required": True,
            },
        )
    ).json()
    r = await client.post(
        f"/journeys/{template['id']}/steps/{step['id']}/requirements/{signable['id']}"
        "/signature-document",
        headers=headers,
        files={"file": ("x.docx", b"not a pdf", "application/msword")},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "journey.signature_document_not_pdf"


# --- (2) la garde du flag : env MAÎTRE, réglage agence sous-interrupteur -------------


async def test_env_master_beats_the_agency_setting(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env OFF : l'agence a beau écrire signatures_enabled=true dans ses
    settings (le PATCH le permet), RIEN ne s'active — le AND est
    structurel, aucune auto-activation possible."""
    headers = agent_headers(admin)
    template_id, _, _ = await _own_journey(client, headers)

    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.settings = {**(agency.settings or {}), "signatures_enabled": True}
    await db_session.commit()

    monkeypatch.setenv("SIGNATURES_ENABLED", "false")
    get_settings.cache_clear()
    case, _pid = await _case_on(
        client,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        template_id,
        "master@example.com",
    )
    assert await _requests(db_session, case.id) == []
    assert fake_provider.create_calls == []
    body = (await client.get("/agencies/me", headers=headers)).json()
    assert body["signatures_enabled"] is False  # l'EFFECTIF exposé


async def test_agency_subswitch_off_blocks_sends(
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
    """Env ON + sous-interrupteur agence OFF : pas d'envoi, exposition
    effective false, face client muette — le rollout sélectif."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    template_id, _, _ = await _own_journey(client, headers)
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.settings = {**(agency.settings or {}), "signatures_enabled": False}
    await db_session.commit()

    case, _pid = await _case_on(
        client,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        template_id,
        "subsw@example.com",
    )
    assert await _requests(db_session, case.id) == []
    assert fake_provider.create_calls == []
    assert (await client.get("/agencies/me", headers=headers)).json()["signatures_enabled"] is False
    principal = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "subsw@example.com"))
    ).scalar_one()
    tasks = (
        await client.get(f"/expat/cases/{case.id}/signatures", headers=expat_headers(principal))
    ).json()
    assert tasks == []


# --- (3) le « Signé n/m » côté client -------------------------------------------------


async def test_expat_listing_carries_signed_n_over_m(
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
    """Le principal voit « en attente des autres signataires » : sa tâche
    porte signed/total de la DEMANDE (0/2 puis 1/2 quand le membre a
    signé)."""
    await give_credits(admin.agency_id, 10)
    case, _pid = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="nm",
    )
    case_id = case.id
    principal = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "nm-p@example.com"))
    ).scalar_one()
    tasks = (
        await client.get(f"/expat/cases/{case_id}/signatures", headers=expat_headers(principal))
    ).json()
    assert (tasks[0]["request_signed_count"], tasks[0]["request_signer_total"]) == (0, 2)

    # UN signataire signe (webhook) → le principal lit 1/2.
    request = (await _requests(db_session, case_id))[0]
    a_signer = (await _signers(db_session, request.id))[0]
    monkeypatch.setenv("DOCUSEAL_WEBHOOK_SECRET", "whsec-nm")
    get_settings.cache_clear()
    r = await client.post(
        "/webhooks/docuseal",
        headers={"X-Docuseal-Secret": "whsec-nm"},
        json={"event_type": "form.completed", "data": {"external_id": str(a_signer.id)}},
    )
    assert r.status_code == 200, r.text
    tasks = (
        await client.get(f"/expat/cases/{case_id}/signatures", headers=expat_headers(principal))
    ).json()
    assert (tasks[0]["request_signed_count"], tasks[0]["request_signer_total"]) == (1, 2)
