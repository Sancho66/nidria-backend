"""Méga-lot signatures électroniques (28/07) — la batterie des 3 lots.

Lot 1 : matérialisation par scope à l'activation, personne tardive (sa
propre demande), auto-clôture « validée par : Personne », témoins (flag
éteint, signature sur non-document, niveau non implémenté).
Lot 2 : ledger (réservation/consommation/libération, dérivation =
matérialisation, idempotence Paddle, insuffisance atomique, concurrence
basique, seuil bas).
Lot 3 : cycle complet sur FakeProvider (envoi → partiel → complété →
crédit consommé → PDF + preuve rangés), annulation → release, webhook
idempotent + secret, slug personnel (404 non-révélateur), relances.

Tout sur FakeProvider (pattern outbox) — AUCUN réseau.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from shared.models.signature import SignatureRequest, SignatureSigner
from shared.models.signature_credit import SignatureCreditEntry
from src.core import email, storage
from src.core.config import get_settings
from src.signatures import ledger
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.signature_plugin import FAKE_PDF, SOURCE_PDF, FakeProvider

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _document_template(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str = "Statuts",
    synced: bool = True,
) -> dict:
    """Un modèle de la bibliothèque, zones posées (builder-sync simulé sur
    le FakeProvider — default_roles) sauf synced=False."""
    r = await client.post(
        "/document-templates",
        headers=headers,
        data={"name": name},
        files={"file": ("statuts.pdf", SOURCE_PDF, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    template = r.json()
    if synced:
        r = await client.post(f"/document-templates/{template['id']}/builder-sync", headers=headers)
        assert r.status_code == 200, r.text
        template = r.json()
    return template


async def _signable_case(
    client: AsyncClient,
    db: AsyncSession,
    admin: Agent,
    headers: dict[str, str],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    *,
    email_prefix: str,
    with_member: bool = True,
    validated_by_none: bool = False,
    activate: bool = True,
) -> tuple[ClientCase, str]:
    """Un dossier avec l'étape « Contrat » portant un document SIGNABLE
    each_person (« Statuts ») + un document classique principal (« Kbis »).
    Retourne (case, progress_id de l'étape)."""
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    body: dict = {"name": "Contrat"}
    if validated_by_none:
        body["validated_by_type"] = "none"
    step = (
        await client.post(f"/journeys/{template['id']}/steps", headers=headers, json=body)
    ).json()
    # Méga-lot modèles : l'exigence signable naît AVEC son modèle de la
    # bibliothèque (PDF source + zones sauvegardées au builder — simulé par
    # builder-sync sur le FakeProvider). Le PDF-direct est mort.
    doc_template = await _document_template(client, headers, name=f"Statuts {email_prefix}")
    r = await client.post(
        f"/journeys/{template['id']}/steps/{step['id']}/requirements",
        headers=headers,
        json={
            "kind": "document",
            "reference": "Statuts",
            "scope": "each_person",
            "signature_required": True,
            "document_template_id": doc_template["id"],
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        f"/journeys/{template['id']}/steps/{step['id']}/requirements",
        headers=headers,
        json={"kind": "document", "reference": "Kbis", "scope": "principal"},
    )
    assert r.status_code == 201, r.text

    principal = await make_expat_user(activated=True, email=f"{email_prefix}-p@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    if with_member:
        created = await client.post(
            f"/cases/{case.id}/persons",
            headers=headers,
            json={
                "full_name": "Assoc Signer",
                "relationship": "associate",
                "email": f"{email_prefix}-m@example.com",
            },
        )
        assert created.status_code == 201, created.text
    timeline = (
        await client.post(
            f"/cases/{case.id}/journey",
            headers=headers,
            json={"journey_template_id": template["id"]},
        )
    ).json()
    progress_id = timeline[0]["id"]
    if activate:
        r = await client.patch(
            f"/cases/{case.id}/steps/{progress_id}", headers=headers, json={"status": "in_progress"}
        )
        assert r.status_code == 200, r.text
    return case, progress_id


async def _requests(db: AsyncSession, case_id: uuid.UUID) -> list[SignatureRequest]:
    return list(
        (
            await db.execute(
                select(SignatureRequest)
                .where(SignatureRequest.case_id == case_id)
                .order_by(SignatureRequest.created_at)
            )
        ).scalars()
    )


async def _signers(db: AsyncSession, request_id: uuid.UUID) -> list[SignatureSigner]:
    return list(
        (
            await db.execute(
                select(SignatureSigner).where(SignatureSigner.signature_request_id == request_id)
            )
        ).scalars()
    )


# =====================================================================================
# LOT 1 — modèle + matérialisation
# =====================================================================================


async def test_activation_materializes_one_request_per_scope(
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
    case, progress_id = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="mat",
    )

    requests = await _requests(db_session, case.id)
    # UNE demande (le document signable) — le Kbis classique n'en crée pas.
    assert len(requests) == 1
    request = requests[0]
    assert request.status == "sent"
    assert request.reference == "Statuts"
    assert request.provider_ref is not None and request.provider_ref.startswith("sub_")
    assert str(request.case_step_progress_id) == progress_id
    # each_person : le principal ET le membre, chacun lié à SA ligne.
    signers = await _signers(db_session, request.id)
    assert len(signers) == 2
    assert all(s.provider_slug for s in signers)
    linked_rows = {s.case_step_requirement_id for s in signers}
    signable_rows = {
        r.id
        for r in (
            await db_session.execute(
                select(CaseStepRequirement).where(
                    CaseStepRequirement.case_step_progress_id == uuid.UUID(progress_id),
                    CaseStepRequirement.signature_required.is_(True),
                )
            )
        ).scalars()
    }
    assert linked_rows == signable_rows
    # Le provider a reçu NOS ids en external_id, en UN appel.
    assert len(fake_provider.create_calls) == 1
    sent_ids = {s.signer_id for s in fake_provider.create_calls[0]["signers"]}
    assert sent_ids == {str(s.id) for s in signers}
    # 1 document envoyé = 1 crédit réservé, quel que soit le nombre de signataires.
    assert await ledger.balance(db_session, admin.agency_id) == (9, 1)
    assert await ledger.derived_balance(db_session, admin.agency_id) == (9, 1)


async def test_flag_off_nothing_materializes(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Configuré flag ON (le CRUD bibliothèque est gaté au flag effectif),
    # puis KILL SWITCH : l'activation passe, mais rien ne part.
    headers = agent_headers(admin)
    case, progress_id = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="off",
        activate=False,
    )
    case_id = case.id
    monkeypatch.setenv("SIGNATURES_ENABLED", "false")
    get_settings.cache_clear()
    r = await client.patch(
        f"/cases/{case_id}/steps/{progress_id}", headers=headers, json={"status": "in_progress"}
    )
    assert r.status_code == 200, r.text
    # L'activation a réussi, et RIEN n'existe : ni demande, ni appel
    # provider, ni écriture ledger.
    assert await _requests(db_session, case_id) == []
    assert fake_provider.create_calls == []
    assert await ledger.derived_balance(db_session, admin.agency_id) == (0, 0)


async def test_late_person_gets_their_own_request(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Personne tardive (décision au rapport) : le port ne sait pas ajouter
    un signataire à une soumission vivante → RECRÉATION, une demande à elle
    (1 signataire), qui coûte SON crédit."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    case, _ = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="late",
        with_member=False,
    )
    assert len(await _requests(db_session, case.id)) == 1

    created = await client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={"full_name": "Late Signer", "relationship": "associate"},
    )
    assert created.status_code == 201, created.text

    requests = await _requests(db_session, case.id)
    assert len(requests) == 2  # la sienne, en plus — jamais réassis ailleurs
    late = requests[1]
    late_signers = await _signers(db_session, late.id)
    assert len(late_signers) == 1
    assert len(fake_provider.create_calls) == 2
    # 2 documents envoyés = 2 crédits réservés.
    assert await ledger.balance(db_session, admin.agency_id) == (8, 2)


async def test_signature_gates_the_none_autoclose(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
    webhook_headers,
) -> None:
    """Étape « Action validée par : Personne » (none) : elle ne s'auto-clôt
    PAS tant que la signature manque, et s'auto-clôt à la dernière
    signature — les signatures comptent exactement comme les pièces."""
    await give_credits(admin.agency_id, 10)
    case, progress_id = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="auto",
        with_member=False,
        validated_by_none=True,
    )
    # Le Kbis (pièce classique) est fourni — seule la signature manque.
    kbis = (
        await db_session.execute(
            select(CaseStepRequirement).where(
                CaseStepRequirement.case_step_progress_id == uuid.UUID(progress_id),
                CaseStepRequirement.reference == "Kbis",
            )
        )
    ).scalar_one()
    kbis.status = "provided"
    kbis.provided_at = datetime.now(UTC)
    await db_session.commit()

    progress = await db_session.get(CaseStepProgress, uuid.UUID(progress_id))
    assert progress is not None and progress.status == "in_progress"  # gated

    request = (await _requests(db_session, case.id))[0]
    signer = (await _signers(db_session, request.id))[0]
    r = await client.post(
        "/webhooks/docuseal",
        headers=webhook_headers,
        json={"event_type": "form.completed", "data": {"external_id": str(signer.id)}},
    )
    assert r.status_code == 200, r.text
    await db_session.refresh(progress)
    assert progress.status == "done"  # la signature a fermé l'étape


async def test_signature_refused_on_non_document_and_non_ses(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await client.post(f"/journeys/{template['id']}/steps", headers=headers, json={"name": "S"})
    ).json()
    base = f"/journeys/{template['id']}/steps/{step['id']}/requirements"
    r = await client.post(
        base,
        headers=headers,
        json={
            "kind": "document",
            "reference": "Contrat",
            "scope": "principal",
            "signature_required": True,
            "signature_level": "qes",
        },
    )
    assert r.status_code == 422
    assert r.json()["code"] == "journey.signature_level_not_implemented"
    # Un champ ne se signe pas (la déclaration exige d'abord l'onglet
    # Informations, mais le refus signature doit primer et nommer sa cause).
    r = await client.post(
        base,
        headers=headers,
        json={
            "kind": "base_field",
            "reference": "passport_number",
            "scope": "principal",
            "signature_required": True,
        },
    )
    assert r.status_code == 422
    assert r.json()["code"] == "journey.signature_on_non_document"


# =====================================================================================
# LOT 2 — ledger
# =====================================================================================


async def test_insufficient_credits_block_activation_atomically(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
) -> None:
    """Solde 0 → l'activation répond une erreur domaine typée et RIEN n'est
    à moitié fait : étape TODO, zéro demande, zéro signer, zéro écriture,
    zéro appel provider."""
    headers = agent_headers(admin)
    agency_id = admin.agency_id
    case, progress_id = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="broke",
        activate=False,
    )
    case_id = case.id  # primitif : `case` expirera au rollback ci-dessous
    r = await client.patch(
        f"/cases/{case.id}/steps/{progress_id}", headers=headers, json={"status": "in_progress"}
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "signatures.credits_insufficient"
    assert r.json()["params"]["available"] == 0

    # Le get_db réel ferme la session par requête : l'état à moitié écrit
    # meurt avec l'exception. L'override de test PARTAGE la session — on la
    # rollback (libère les verrous du tx avorté) et on observe via une
    # session NEUVE, exactement ce que la prod verrait.
    await db_session.rollback()
    maker = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    async with maker() as fresh:
        progress = await fresh.get(CaseStepProgress, uuid.UUID(progress_id))
        assert progress is not None and progress.status == "todo"
        assert await _requests(fresh, case_id) == []
        entries = list(
            (
                await fresh.execute(
                    select(SignatureCreditEntry).where(SignatureCreditEntry.agency_id == agency_id)
                )
            ).scalars()
        )
        assert entries == []
    assert fake_provider.create_calls == []


async def test_paddle_pack_purchase_is_idempotent(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """transaction.completed portant un pack → +50, rejeu du MÊME event →
    no-op (ligne événement unique + ceinture ledger sur l'id de
    transaction). Même doctrine que le billing."""
    import json as json_lib

    from tests.test_billing_paddle import PRICE_IDS, SECRET, _envelope, _post

    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("PADDLE_PRICE_IDS", json_lib.dumps(PRICE_IDS))
    monkeypatch.setenv(
        "SIGNATURE_CREDIT_PACKS", json_lib.dumps({"pri_sig50": 50, "pri_sig200": 200})
    )
    get_settings.cache_clear()

    envelope = _envelope(
        "transaction.completed",
        agency_id=admin.agency_id,
        subscription_id="txn_pack_1",
        items=[{"price": {"id": "pri_sig50"}, "quantity": 1}],
    )
    resp = await _post(client, envelope)
    assert resp.status_code == 200, resp.text
    assert await ledger.balance(db_session, admin.agency_id) == (50, 0)

    # Rejeu (nouvel event_id, MÊME transaction) : la ceinture du ledger tient.
    replay = _envelope(
        "transaction.completed",
        agency_id=admin.agency_id,
        subscription_id="txn_pack_1",
        items=[{"price": {"id": "pri_sig50"}, "quantity": 1}],
    )
    resp = await _post(client, replay)
    assert resp.status_code == 200, resp.text
    assert await ledger.balance(db_session, admin.agency_id) == (50, 0)
    assert await ledger.derived_balance(db_session, admin.agency_id) == (50, 0)


async def test_low_balance_alert_fires_once_per_crossing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Seuil (défaut 10) : le mail part au FRANCHISSEMENT (10 → 9), pas à
    chaque réservation — l'admin le reçoit une fois."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    email.outbox.clear()
    case, _ = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="low1",
        with_member=False,
    )
    low_mails = [m for m in email.outbox if "crédits signature" in m.subject]
    assert len(low_mails) == 1
    assert low_mails[0].to == admin.email

    email.outbox.clear()
    case2, _ = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="low2",
        with_member=False,
    )
    assert [m for m in email.outbox if "crédits signature" in m.subject] == []


async def test_basic_concurrency_never_goes_negative(
    db_session: AsyncSession,
    admin: Agent,
    give_credits,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Deux réservations en course sur 1 crédit : le verrou de ligne les
    sérialise — exactement UNE passe, l'autre reçoit l'erreur typée, le
    solde ne descend jamais sous zéro."""
    await give_credits(admin.agency_id, 1)
    principal = await make_expat_user(activated=True, email="conc@example.com")
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=principal.id)
    maker = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)

    async def _reserve_once() -> str:
        async with maker() as session:
            request = SignatureRequest(
                case_id=case.id,
                case_step_progress_id=None,  # type: ignore[arg-type]
                reference="Course",
                provider="docuseal",
                level="ses",
            )
            # progress obligatoire : matérialise un vrai progress minimal.
            from shared.models.journey import JourneyTemplate, JourneyTemplateStep

            tpl = JourneyTemplate(agency_id=admin.agency_id, name=f"C-{uuid.uuid4().hex[:6]}")
            session.add(tpl)
            await session.flush()
            step = JourneyTemplateStep(template_id=tpl.id, name="S", position=1)
            session.add(step)
            await session.flush()
            progress = CaseStepProgress(
                case_id=case.id, template_step_id=step.id, status="in_progress"
            )
            session.add(progress)
            await session.flush()
            request.case_step_progress_id = progress.id
            session.add(request)
            await session.flush()
            try:
                await ledger.reserve_credit(session, admin.agency_id, request)
            except ledger.SignatureCreditsInsufficientError:
                await session.rollback()
                return "refused"
            await session.commit()
            return "reserved"

    results = await asyncio.gather(_reserve_once(), _reserve_once())
    assert sorted(results) == ["refused", "reserved"]
    available, reserved = await ledger.balance(db_session, admin.agency_id)
    assert (available, reserved) == (0, 1)
    assert await ledger.derived_balance(db_session, admin.agency_id) == (0, 1)


# =====================================================================================
# LOT 3 — provider + webhook + espace client + relances
# =====================================================================================


@pytest.fixture
def webhook_headers(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("DOCUSEAL_WEBHOOK_SECRET", "whsec-test")
    get_settings.cache_clear()
    return {"X-Docuseal-Secret": "whsec-test"}


async def test_full_cycle_partial_completed_credit_and_filing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
    webhook_headers: dict[str, str],
) -> None:
    agency_id = admin.agency_id
    await give_credits(agency_id, 5)
    case, progress_id = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="cycle",
    )
    # Ids PRIMITIFS capturés avant les expire_all : un attribut ORM expiré
    # touché depuis le test = chargement sync hors greenlet (MissingGreenlet).
    case_id = case.id
    request = (await _requests(db_session, case_id))[0]
    signer_a, signer_b = await _signers(db_session, request.id)
    signer_a_id, signer_b_id = signer_a.id, signer_b.id
    row_a_id = signer_a.case_step_requirement_id
    row_b_id = signer_b.case_step_requirement_id

    # Signature partielle : signer A.
    r = await client.post(
        "/webhooks/docuseal",
        headers=webhook_headers,
        json={"event_type": "form.completed", "data": {"external_id": str(signer_a_id)}},
    )
    assert r.status_code == 200 and r.json()["status"] == "processed"
    db_session.expire_all()
    request = (await _requests(db_session, case_id))[0]
    assert request.status == "partially_signed"
    row_a = await db_session.get(CaseStepRequirement, row_a_id)
    assert row_a is not None and row_a.status == "provided"  # SA ligne, fournie
    row_b = await db_session.get(CaseStepRequirement, row_b_id)
    assert row_b is not None and row_b.status == "pending"  # l'autre attend
    assert await ledger.balance(db_session, agency_id) == (4, 1)  # toujours réservé

    # Complétion : signer B → consume + téléchargement immédiat + rangement.
    r = await client.post(
        "/webhooks/docuseal",
        headers=webhook_headers,
        json={"event_type": "form.completed", "data": {"external_id": str(signer_b_id)}},
    )
    assert r.status_code == 200 and r.json()["status"] == "processed"
    db_session.expire_all()
    request = (await _requests(db_session, case_id))[0]
    assert request.status == "completed"
    assert fake_provider.download_calls == [request.provider_ref]
    assert await ledger.balance(db_session, agency_id) == (4, 0)  # consommé
    assert await ledger.derived_balance(db_session, agency_id) == (4, 0)

    # GAP-B : PDF signé + dossier de preuve rangés en LIVRABLES du dossier,
    # octets stockés (jamais une URL), lignes d'exigence pointant le PDF.
    docs = list(
        (await db_session.execute(select(Document).where(Document.case_id == case_id))).scalars()
    )
    assert {d.filename for d in docs} == {"signed-document.pdf", "audit-log.pdf"}
    assert all(d.kind == "deliverable" for d in docs)
    signed = next(d for d in docs if d.filename == "signed-document.pdf")
    assert storage.mock_store[signed.storage_path] == FAKE_PDF
    await db_session.refresh(row_a)
    assert row_a.document_id == signed.id


async def test_cancel_releases_the_credit(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    from src.signatures.signatures_manager import SignaturesManager

    await give_credits(admin.agency_id, 3)
    case, _ = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="cancel",
        with_member=False,
    )
    request = (await _requests(db_session, case.id))[0]
    assert await ledger.balance(db_session, admin.agency_id) == (2, 1)

    case_row = await db_session.get(ClientCase, case.id)
    await SignaturesManager(db_session).cancel_request(case_row, request)
    await db_session.commit()
    assert fake_provider.cancel_calls == [request.provider_ref]
    assert request.status == "cancelled"
    assert await ledger.balance(db_session, admin.agency_id) == (3, 0)  # libéré
    # Double annulation : release idempotent, rien ne bouge.
    await SignaturesManager(db_session).cancel_request(case_row, request)
    await db_session.commit()
    assert await ledger.balance(db_session, admin.agency_id) == (3, 0)
    assert await ledger.derived_balance(db_session, admin.agency_id) == (3, 0)


async def test_webhook_replay_and_expiry_are_idempotent(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
    webhook_headers: dict[str, str],
) -> None:
    await give_credits(admin.agency_id, 5)
    case, _ = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="replay",
        with_member=False,
    )
    request = (await _requests(db_session, case.id))[0]
    signer = (await _signers(db_session, request.id))[0]
    payload = {"event_type": "form.completed", "data": {"external_id": str(signer.id)}}
    for _ in range(3):  # rejoué : converge, jamais un double consume
        r = await client.post("/webhooks/docuseal", headers=webhook_headers, json=payload)
        assert r.status_code == 200
    assert await ledger.balance(db_session, admin.agency_id) == (4, 0)
    consumes = list(
        (
            await db_session.execute(
                select(SignatureCreditEntry).where(
                    SignatureCreditEntry.signature_request_id == request.id,
                    SignatureCreditEntry.kind == "consume",
                )
            )
        ).scalars()
    )
    assert len(consumes) == 1
    # Une expiration qui arrive APRÈS la complétion ne libère rien.
    r = await client.post(
        "/webhooks/docuseal",
        headers=webhook_headers,
        json={"event_type": "submission.expired", "data": {"id": request.provider_ref}},
    )
    assert r.status_code == 200
    assert await ledger.balance(db_session, admin.agency_id) == (4, 0)


async def test_webhook_authenticity(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCUSEAL_WEBHOOK_SECRET", "whsec-test")
    get_settings.cache_clear()
    payload = {"event_type": "form.completed", "data": {}}
    # Mauvais secret → 401 ; absent → 401.
    r = await client.post(
        "/webhooks/docuseal", headers={"X-Docuseal-Secret": "wrong"}, json=payload
    )
    assert r.status_code == 401
    r = await client.post("/webhooks/docuseal", json=payload)
    assert r.status_code == 401
    # Secret NON configuré chez nous → fermé (401), jamais accepté à l'aveugle.
    monkeypatch.delenv("DOCUSEAL_WEBHOOK_SECRET")
    get_settings.cache_clear()
    r = await client.post(
        "/webhooks/docuseal", headers={"X-Docuseal-Secret": "whsec-test"}, json=payload
    )
    assert r.status_code == 401
    # Flag éteint → 200 « ignored » silencieux (rien n'existe, pas de boucle
    # de re-livraison côté provider).
    monkeypatch.setenv("SIGNATURES_ENABLED", "false")
    get_settings.cache_clear()
    r = await client.post("/webhooks/docuseal", json=payload)
    assert r.status_code == 200 and r.json()["status"] == "ignored"


async def test_expat_slug_is_personal(
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
    """Chacun N'OBTIENT QUE SON slug (il ouvre SA session de signature) :
    le membre voit sa ligne, le principal la sienne — jamais celle de
    l'autre ; l'étranger au dossier → 404 non-révélateur."""
    await give_credits(admin.agency_id, 5)
    case, _ = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="slug",
    )
    request = (await _requests(db_session, case.id))[0]
    signers = await _signers(db_session, request.id)
    principal_user = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "slug-p@example.com"))
    ).scalar_one()
    member_user = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "slug-m@example.com"))
    ).scalar_one()
    member_user.activated_at = datetime.now(UTC)
    await db_session.commit()

    mine = (
        await client.get(
            f"/expat/cases/{case.id}/signatures", headers=expat_headers(principal_user)
        )
    ).json()
    theirs = (
        await client.get(f"/expat/cases/{case.id}/signatures", headers=expat_headers(member_user))
    ).json()
    assert len(mine) == 1 and len(theirs) == 1
    assert mine[0]["signer_id"] != theirs[0]["signer_id"]
    assert {mine[0]["signer_id"], theirs[0]["signer_id"]} == {str(s.id) for s in signers}
    assert mine[0]["embed_slug"] and theirs[0]["embed_slug"]
    assert mine[0]["embed_slug"] != theirs[0]["embed_slug"]

    stranger = await make_expat_user(activated=True, email="stranger@example.com")
    r = await client.get(f"/expat/cases/{case.id}/signatures", headers=expat_headers(stranger))
    assert r.status_code == 404  # non-révélateur


async def test_pending_signature_keeps_the_reminder_pipeline_chasing(
    client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Une signature en attente = une exigence pendante : l'étape n'est pas
    « tout fourni », donc les relances auto v4 (et le bouton Relancer, même
    périmètre) continuent de la chasser — zéro mécanique parallèle."""
    from src.reminders.reminders_jobs import create_auto_reminders

    await give_credits(admin.agency_id, 5)
    case, progress_id = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="chase",
        with_member=False,
    )
    # Le Kbis est fourni : la SIGNATURE est le seul manque. Étape immobile 25 j.
    kbis = (
        await db_session.execute(
            select(CaseStepRequirement).where(
                CaseStepRequirement.case_step_progress_id == uuid.UUID(progress_id),
                CaseStepRequirement.reference == "Kbis",
            )
        )
    ).scalar_one()
    kbis.status = "provided"
    await db_session.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.id == uuid.UUID(progress_id))
        .values(updated_at=datetime.now(UTC) - timedelta(days=25))
    )
    await db_session.commit()

    with sync_session_local() as db:
        stats = create_auto_reminders(db, log=lambda _: None)
    assert stats["created"] == 1  # la relance chasse la signature manquante
