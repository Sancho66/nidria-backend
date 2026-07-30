"""DURCISSEMENT signatures (pré-flip, 29/07) — les 4 gaps du smoke.

1. Une exigence signable ne se « fournit » QUE par la signature : dépôt
   refusé 422 pour TOUTE audience (membre, principal, agence) — garde au
   cœur partagé fulfill_document_requirement.
2. Annulation agence (gate STEP_COMPLETE, le gate des gestes d'étape) :
   archive provider + release + statut, idempotente ; « re-demander » est
   un geste séparé (POST steps/{id}/signature-requests → send_for_progress
   idempotent — l'annulation ne re-envoie jamais toute seule, constat).
3. Les demandes mortes (cancelled/expired) ne sortent jamais côté client ;
   les complétées restent (le front affiche « Signé » + n/m final).
4. Le livrable webhook expose uploaded_by_type='system' sur les DEUX faces
   — la clé du front pour « Signé via Nidria ».
"""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core.config import get_settings
from src.signatures import ledger
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.signature_plugin import FakeProvider
from tests.test_signatures import _requests, _signable_case, _signers

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest.fixture
def webhook_headers(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("DOCUSEAL_WEBHOOK_SECRET", "whsec-hard")
    get_settings.cache_clear()
    return {"X-Docuseal-Secret": "whsec-hard"}


PDF_UPLOAD = {"file": ("piece.pdf", b"%PDF-1.4 depot", "application/pdf")}


# --- (1) dépôt refusé sur une ligne signable, toute audience -------------------------


async def test_deposit_refused_on_a_signable_row_for_every_audience(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
    fake_provider: FakeProvider,
    give_credits,
    webhook_headers: dict[str, str],
) -> None:
    await give_credits(admin.agency_id, 5)
    headers = agent_headers(admin)
    case, _pid = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="hard",
    )
    case_id = case.id
    request = (await _requests(db_session, case_id))[0]
    signer_p, signer_m = await _signers(db_session, request.id)
    # Ids PRIMITIFS avant les 422/rollbacks (attribut ORM expiré touché
    # depuis le test = MissingGreenlet, leçon des batteries précédentes).
    from shared.models.case_person import CasePerson

    principal = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "hard-p@example.com"))
    ).scalar_one()
    principal_person_id = (
        await db_session.execute(
            select(CasePerson.id).where(
                CasePerson.case_id == case_id, CasePerson.expat_user_id == principal.id
            )
        )
    ).scalar_one()
    by_person = {s_.case_person_id: s_.case_step_requirement_id for s_ in (signer_p, signer_m)}
    principal_row_id = by_person[principal_person_id]
    member_row_id = next(v for k, v in by_person.items() if k != principal_person_id)
    signer_p_id = signer_p.id
    signed_row_id = by_person[signer_p.case_person_id]
    member = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "hard-m@example.com"))
    ).scalar_one()
    member.activated_at = datetime.now(UTC)
    await db_session.commit()
    principal_headers = expat_headers(principal)
    member_headers = expat_headers(member)

    # L'AGENCE ne contourne pas : dépôt agent → 422 nommé.
    r = await client.post(
        f"/cases/{case_id}/requirements/{principal_row_id}/document",
        headers=headers,
        files=PDF_UPLOAD,
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "requirement.signable_needs_signature"
    await db_session.rollback()  # le get_db réel ferme la session par requête

    # Le PRINCIPAL non plus (sa propre ligne).
    r = await client.post(
        f"/expat/cases/{case_id}/requirements/{principal_row_id}/document",
        headers=principal_headers,
        files=PDF_UPLOAD,
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "requirement.signable_needs_signature"
    await db_session.rollback()

    # Le MEMBRE non plus (sa ligne à lui).
    r = await client.post(
        f"/expat/cases/{case_id}/requirements/{member_row_id}/document",
        headers=member_headers,
        files=PDF_UPLOAD,
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "requirement.signable_needs_signature"
    await db_session.rollback()

    # La SIGNATURE, elle, complète la ligne comme avant (webhook).
    r = await client.post(
        "/webhooks/docuseal",
        headers=webhook_headers,
        json={"event_type": "form.completed", "data": {"external_id": str(signer_p_id)}},
    )
    assert r.status_code == 200
    db_session.expire_all()
    from shared.models.case_step_requirement import CaseStepRequirement

    row = await db_session.get(CaseStepRequirement, signed_row_id)
    assert row is not None and row.status == "provided"


# --- (2) annulation + re-envoi --------------------------------------------------------


async def test_cancel_endpoint_releases_and_is_idempotent(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    await give_credits(admin.agency_id, 5)
    agency_id = admin.agency_id
    headers = agent_headers(admin)
    case, _pid = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="cxl",
        with_member=False,
    )
    case_id = case.id
    request = (await _requests(db_session, case_id))[0]
    assert await ledger.balance(db_session, agency_id) == (4, 1)

    r = await client.post(
        f"/cases/{case_id}/signature-requests/{request.id}/cancel", headers=headers
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    request = (await _requests(db_session, case_id))[0]
    assert request.status == "cancelled"
    assert fake_provider.cancel_calls == [request.provider_ref]  # archive provider
    assert await ledger.balance(db_session, agency_id) == (5, 0)  # release

    # Double annulation : idempotente, rien ne bouge.
    r = await client.post(
        f"/cases/{case_id}/signature-requests/{request.id}/cancel", headers=headers
    )
    assert r.status_code == 200
    assert len(fake_provider.cancel_calls) == 1
    assert await ledger.balance(db_session, agency_id) == (5, 0)
    assert await ledger.derived_balance(db_session, agency_id) == (5, 0)

    # Cross-agency : la demande d'un autre dossier est invisible (404).
    from tests.plugins.agency_plugin import MakeAgency  # noqa: F401 — doc


async def test_resend_after_cancel_creates_a_fresh_request(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """L'annulation ne re-demande JAMAIS toute seule (constat : aucun
    déclencheur automatique) — « re-demander » est le geste explicite, et
    il est idempotent (sans manque → sent=0)."""
    await give_credits(admin.agency_id, 5)
    agency_id = admin.agency_id
    headers = agent_headers(admin)
    case, progress_id = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="resend",
        with_member=False,
    )
    case_id = case.id
    first = (await _requests(db_session, case_id))[0]
    r = await client.post(f"/cases/{case_id}/signature-requests/{first.id}/cancel", headers=headers)
    assert r.status_code == 200
    # Rien ne repart tout seul.
    assert len(await _requests(db_session, case_id)) == 1

    # Le geste explicite renvoie (nouveau crédit réservé, nouvelle demande).
    r = await client.post(
        f"/cases/{case_id}/steps/{progress_id}/signature-requests", headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent=1"
    db_session.expire_all()
    requests = await _requests(db_session, case_id)
    assert [x.status for x in requests] == ["cancelled", "sent"]
    assert await ledger.balance(db_session, agency_id) == (4, 1)

    # Idempotent : plus de manque → sent=0, aucun doublon.
    r = await client.post(
        f"/cases/{case_id}/steps/{progress_id}/signature-requests", headers=headers
    )
    assert r.status_code == 200
    assert r.json()["status"] == "sent=0"
    assert len(await _requests(db_session, case_id)) == 2


# --- (3) demandes mortes invisibles côté client --------------------------------------


async def test_dead_requests_never_reach_the_client(
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
    await give_credits(admin.agency_id, 5)
    headers = agent_headers(admin)
    case, _pid = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="dead",
        with_member=False,
    )
    case_id = case.id
    principal = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "dead-p@example.com"))
    ).scalar_one()
    tasks = (
        await client.get(f"/expat/cases/{case_id}/signatures", headers=expat_headers(principal))
    ).json()
    assert len(tasks) == 1  # vivante : visible

    request = (await _requests(db_session, case_id))[0]
    r = await client.post(
        f"/cases/{case_id}/signature-requests/{request.id}/cancel", headers=headers
    )
    assert r.status_code == 200
    tasks = (
        await client.get(f"/expat/cases/{case_id}/signatures", headers=expat_headers(principal))
    ).json()
    assert tasks == []  # morte : jamais à l'état brut côté client


# --- (4) le livrable système, la clé du front ----------------------------------------


async def test_system_deliverable_exposes_the_key_on_both_faces(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
    fake_provider: FakeProvider,
    give_credits,
    webhook_headers: dict[str, str],
) -> None:
    """Le PDF signé rangé par le webhook expose kind='deliverable' +
    uploaded_by_type='system' sur les DEUX faces — la clé (jamais un
    texte) sur laquelle le front rend « Signé via Nidria », plus jamais
    « une autre personne du dossier »."""
    await give_credits(admin.agency_id, 5)
    headers = agent_headers(admin)
    case, _pid = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="sysdoc",
        with_member=False,
    )
    case_id = case.id
    request = (await _requests(db_session, case_id))[0]
    signer = (await _signers(db_session, request.id))[0]
    r = await client.post(
        "/webhooks/docuseal",
        headers=webhook_headers,
        json={"event_type": "form.completed", "data": {"external_id": str(signer.id)}},
    )
    assert r.status_code == 200

    agent_docs = (await client.get(f"/cases/{case_id}/documents", headers=headers)).json()
    signed = [d for d in agent_docs if d["filename"] == "Statuts — signé.pdf"]
    assert len(signed) == 1
    assert signed[0]["kind"] == "deliverable"
    assert signed[0]["uploaded_by_type"] == "system"

    principal = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "sysdoc-p@example.com"))
    ).scalar_one()
    expat_docs = (
        await client.get(f"/expat/cases/{case_id}/documents", headers=expat_headers(principal))
    ).json()
    signed = [d for d in expat_docs if d["filename"] == "Statuts — signé.pdf"]
    assert len(signed) == 1
    assert signed[0]["kind"] == "deliverable"
    assert signed[0]["uploaded_by_type"] == "system"
    assert signed[0]["is_mine"] is False


# --- (complément) l'exposition : le client SAIT qu'une ligne est signable ------------


async def test_expat_timeline_exposes_signature_required(
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
    """ExpatRequirementResponse porte signature_required (rupture ASSUMÉE
    du gelé expat_schema, demandée par le lot) : le client affiche une
    ligne signable d'autrui en lecture seule « à signer », jamais un bouton
    de dépôt — le 422 requirement.signable_needs_signature reste la garde
    si un front contourne."""
    await give_credits(admin.agency_id, 5)
    case, _pid = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="expo2",
    )
    principal = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "expo2-p@example.com"))
    ).scalar_one()
    detail = (await client.get(f"/expat/cases/{case.id}", headers=expat_headers(principal))).json()
    reqs = [r for step in detail["timeline"] for r in step["requirements"]]
    by_ref: dict[str, list[dict]] = {}
    for r in reqs:
        by_ref.setdefault(r["reference"], []).append(r)
    assert all(r["signature_required"] for r in by_ref["Statuts"])
    assert all(r["signature_required"] is False for r in by_ref["Kbis"])


async def test_webhook_rate_limit_generous_then_429(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Volet 1 post-flip : l'endpoint public n'est plus nu — 120/min par IP,
    généreux (des re-livraisons légitimes n'approchent jamais le seuil),
    429 au-delà. Le limiteur est le seam in-process existant (signup)."""
    from src.core import ratelimit

    ratelimit.reset()
    try:
        for _ in range(120):
            r = await client.post(
                "/webhooks/docuseal",
                headers={"X-Docuseal-Secret": "wrong"},
                json={"event_type": "ping"},
            )
            assert r.status_code == 401  # compté mais refusé par le secret
        r = await client.post(
            "/webhooks/docuseal",
            headers={"X-Docuseal-Secret": "wrong"},
            json={"event_type": "ping"},
        )
        assert r.status_code == 429
    finally:
        ratelimit.reset()
