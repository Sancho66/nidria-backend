"""Source du document signable — ère MODÈLES (méga-lot 29/07, remplace le
PDF-direct du LOT 6, supprimé sur verdict prod zéro ligne).

1. Le MODÈLE de la bibliothèque est LA source : snapshoté sur la ligne à la
   matérialisation, transmis au provider en REF (plus jamais d'octets à
   l'envoi) avec le mapping de rôles « Signataire N » dans l'ordre de
   matérialisation.
2. Les gardes d'envoi : modèle absent (donnée dégradée) → 422 à
   l'assignation ; zones jamais sauvegardées au builder → 422 nommé à
   l'envoi ; plus de signataires que de rôles configurés → 422 nommé
   (constat sonde : DocuSeal accepte un rôle inconnu sans broncher).
3. Sémantique du flag verrouillée : l'env est MAÎTRE ; le réglage agence
   n'est qu'un sous-interrupteur de rollout.
4. Le « Signé n/m » sur les réponses espace client.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from shared.models.step_requirement import StepRequirement
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.signature_plugin import FakeProvider
from tests.test_signatures import _document_template, _requests, _signable_case, _signers

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _own_journey(
    client: AsyncClient, headers: dict[str, str], *, synced: bool = True
) -> tuple[str, str, str, dict]:
    """(template_id, step_id, requirement_id, document_template) — un
    parcours à UNE exigence signable adossée à un modèle de la
    bibliothèque (zones sauvegardées au builder sauf synced=False)."""
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await client.post(
            f"/journeys/{template['id']}/steps", headers=headers, json={"name": "Contrat"}
        )
    ).json()
    doc_template = await _document_template(client, headers, synced=synced)
    req = (
        await client.post(
            f"/journeys/{template['id']}/steps/{step['id']}/requirements",
            headers=headers,
            json={
                "kind": "document",
                "reference": "Statuts",
                "scope": "principal",
                "signature_required": True,
                "document_template_id": doc_template["id"],
            },
        )
    ).json()
    return template["id"], step["id"], req["id"], doc_template


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
    expect_activate: int = 200,
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
        assert r.status_code == expect_activate, r.text
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


# --- (1) la source : snapshot du modèle + ref au provider ----------------------------


async def test_template_is_snapshotted_and_sent_as_ref(
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
    # Scope principal = 1 signataire : le modèle doit porter EXACTEMENT un
    # rôle (garde inverse du mini-complément).
    fake_provider.default_roles = ["Signataire 1"]
    template_id, _, _, doc_template = await _own_journey(client, headers)
    _case, progress_id = await _case_on(
        client, admin, headers, make_client_case, make_expat_user, template_id, "src@example.com"
    )
    row = await _signable_row(db_session, progress_id)
    # Snapshot : la ligne matérialisée fige LE modèle de la définition.
    assert str(row.document_template_id) == doc_template["id"]
    # Le provider reçoit la REF du template (créé à la naissance du modèle,
    # PDF envoyé à ce moment-là) + le mapping de rôles convention sonde.
    assert fake_provider.create_template_calls[0]["external_id"] == doc_template["id"]
    sent = fake_provider.create_calls[0]
    assert sent["template_ref"] == fake_provider.create_template_calls[0]["ref"]
    assert sent["roles"] == ["Signataire 1"]


async def test_roles_follow_materialization_order_principal_first(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """each_person à 2 signataires : « Signataire 1 » = le principal,
    « Signataire 2 » = le membre — l'ordre de matérialisation."""
    await give_credits(admin.agency_id, 10)
    case, _pid = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="order",
    )
    sent = fake_provider.create_calls[0]
    assert sent["roles"] == ["Signataire 1", "Signataire 2"]
    principal_email = "order-p@example.com"
    assert sent["signers"][0].email == principal_email
    assert sent["signers"][0].role == "Signataire 1"


# --- (2) les gardes : modèle absent / zones absentes / rôles insuffisants ------------


async def test_assignment_refuses_signable_without_template(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
) -> None:
    """La création d'exigence refuse déjà un signable sans modèle — la garde
    d'assignation reste la défense structurelle contre la donnée dégradée
    (colonne NULLée en base, hors chemin API)."""
    headers = agent_headers(admin)
    template_id, _, req_id, _doc = await _own_journey(client, headers)
    await db_session.execute(
        update(StepRequirement)
        .where(StepRequirement.id == uuid.UUID(req_id))
        .values(document_template_id=None)
    )
    await db_session.commit()
    principal = await make_expat_user(activated=True, email="notpl@example.com")
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=principal.id)
    r = await client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": template_id}
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "journey.signature_document_missing"
    assert r.json()["params"]["reference"] == "Statuts"


async def test_requirement_creation_refuses_signable_without_template(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, fake_provider: FakeProvider
) -> None:
    headers = agent_headers(admin)
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await client.post(f"/journeys/{template['id']}/steps", headers=headers, json={"name": "S"})
    ).json()
    r = await client.post(
        f"/journeys/{template['id']}/steps/{step['id']}/requirements",
        headers=headers,
        json={
            "kind": "document",
            "reference": "Statuts",
            "scope": "principal",
            "signature_required": True,
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "journey.signature_template_required"
    # Et un modèle sur une exigence NON signable → 422 nommé aussi.
    doc_template = await _document_template(client, headers)
    r = await client.post(
        f"/journeys/{template['id']}/steps/{step['id']}/requirements",
        headers=headers,
        json={
            "kind": "document",
            "reference": "Kbis",
            "scope": "principal",
            "document_template_id": doc_template["id"],
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "journey.template_on_non_signable"


async def test_send_refuses_a_template_never_saved_in_the_builder(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """fields_configured=false (le builder n'a jamais sauvegardé) : on
    n'envoie PAS un modèle sans zones — l'activation entière refuse (422
    nommé), aucune demande, aucun crédit brûlé."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    template_id, _, _, _doc = await _own_journey(client, headers, synced=False)
    _case, _pid = await _case_on(
        client,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        template_id,
        "nofields@example.com",
        expect_activate=422,
    )
    await db_session.rollback()
    assert fake_provider.create_calls == []
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert balance["available"] == 10


async def test_send_refuses_more_signers_than_roles(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Constat sonde : le provider assoit un rôle inconnu SANS valider
    (signataire fantôme sans zones) — la garde de cohérence vit chez nous.
    Modèle re-sauvegardé à 1 rôle + exigence each_person sur dossier à 2
    personnes → 422 nommé à l'activation."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    _case, progress_id = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="roles",
        activate=False,
    )
    case_id = _case.id
    # L'agence rouvre le builder et ne laisse qu'UN rôle : le re-sync
    # constate 1 — l'activation d'un dossier à 2 signataires refuse.
    fake_provider.default_roles = ["Signataire 1"]
    template_id = (await client.get("/document-templates", headers=headers)).json()[0]["id"]
    r = await client.post(f"/document-templates/{template_id}/builder-sync", headers=headers)
    assert r.status_code == 200, r.text
    r = await client.patch(
        f"/cases/{case_id}/steps/{progress_id}", headers=headers, json={"status": "in_progress"}
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "signatures.template_roles_insufficient"
    await db_session.rollback()
    assert fake_provider.create_calls == []


async def test_send_refuses_more_roles_than_persons(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Mini-complément : le sens INVERSE de la garde de mapping. Modèle à
    2 rôles + exigence principal (1 signataire) → un rôle fantôme dont les
    zones ne seraient jamais signées : l'activation refuse (422 nommé),
    aucune demande, aucun crédit brûlé."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    template_id, _, _, _doc = await _own_journey(client, headers)  # default_roles = 2 rôles
    _case, progress_id = await _case_on(
        client,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        template_id,
        "ghost@example.com",
        activate=False,
    )
    r = await client.patch(
        f"/cases/{_case.id}/steps/{progress_id}", headers=headers, json={"status": "in_progress"}
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "signatures.template_roles_exceed_persons"
    assert (body["params"]["roles_count"], body["params"]["signers_count"]) == (2, 1)
    await db_session.rollback()
    assert fake_provider.create_calls == []
    balance = (await client.get("/agencies/me/signature-credits", headers=headers)).json()
    assert balance["available"] == 10


async def test_patch_requirement_toggles_deposit_to_signature_and_back(
    client: AsyncClient,
    admin: Agent,
    make_agent,
    system_roles,
    agent_headers: AuthHeaders,
    fake_provider: FakeProvider,
) -> None:
    """Mini-lot 30/07 : la bascule dépôt ↔ signature SANS delete/recreate,
    avec les invariants de la création (signable ⇒ modèle même agence,
    bascule OFF ⇒ modèle auto-détaché)."""
    headers = agent_headers(admin)
    journey = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await client.post(f"/journeys/{journey['id']}/steps", headers=headers, json={"name": "S"})
    ).json()
    plain = (
        await client.post(
            f"/journeys/{journey['id']}/steps/{step['id']}/requirements",
            headers=headers,
            json={"kind": "document", "reference": "Kbis", "scope": "principal"},
        )
    ).json()
    url = f"/journeys/{journey['id']}/steps/{step['id']}/requirements/{plain['id']}"

    # ON sans modèle → 422 nommé.
    r = await client.patch(url, headers=headers, json={"signature_required": True})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "journey.signature_template_required"
    # Modèle seul sur non-signable → 422 nommé.
    doc_template = await _document_template(client, headers, roles=1)
    r = await client.patch(url, headers=headers, json={"document_template_id": doc_template["id"]})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "journey.template_on_non_signable"
    # Modèle d'une AUTRE agence → 404 non-révélateur.
    other_admin = await make_agent(role=system_roles["admin"])
    foreign = await _document_template(client, agent_headers(other_admin), name="Ailleurs")
    r = await client.patch(
        url,
        headers=headers,
        json={"signature_required": True, "document_template_id": foreign["id"]},
    )
    assert r.status_code == 404, r.text
    # LA bascule ON.
    r = await client.patch(
        url,
        headers=headers,
        json={"signature_required": True, "document_template_id": doc_template["id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["signature_required"] is True
    assert body["document_template_id"] == doc_template["id"]
    # La bascule OFF : le modèle se détache tout seul.
    r = await client.patch(url, headers=headers, json={"signature_required": False})
    assert r.status_code == 200, r.text
    assert r.json()["signature_required"] is False
    assert r.json()["document_template_id"] is None


async def test_patch_template_swap_follows_on_pending_rows(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Lot propagation (30/07, remplace la doctrine du mini-lot) : la ligne
    PENDING du dossier en vol SUIT le swap de modèle A→B ; la demande
    vivante, elle, ne bouge pas (le document envoyé est envoyé — le rappel
    se fait par annulation explicite + re-envoi). L'acquis répondu reste
    couvert par test_answered_row_keeps_its_snapshot (batterie
    propagation)."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    fake_provider.default_roles = ["Signataire 1"]
    template_id, step_id, req_id, doc_a = await _own_journey(client, headers)
    _case, progress_id = await _case_on(
        client, admin, headers, make_client_case, make_expat_user, template_id, "snap@example.com"
    )
    doc_b = await _document_template(client, headers, name="Statuts B", roles=1)
    r = await client.patch(
        f"/journeys/{template_id}/steps/{step_id}/requirements/{req_id}",
        headers=headers,
        json={"document_template_id": doc_b["id"], "signature_required": True},
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    row = await _signable_row(db_session, progress_id)
    assert str(row.document_template_id) == doc_b["id"]  # la pending SUIT
    assert len(fake_provider.create_calls) == 1  # pas de re-envoi sauvage
    assert fake_provider.cancel_calls == []  # la vivante ne bouge pas


# --- (3) la garde du flag : env MAÎTRE, réglage agence sous-interrupteur -------------


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
    template_id, _, _, _doc = await _own_journey(client, headers)

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
    template_id, _, _, _doc = await _own_journey(client, headers)
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


# --- (4) le « Signé n/m » côté client -------------------------------------------------


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
