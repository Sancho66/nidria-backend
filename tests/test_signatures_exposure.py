"""TEMPS 2 — exposition du contrat signatures (6 trous relevés par le front).

1. `signatures_enabled` sur /agencies/me (ancre du front, précédent
   trial_ends_at). 2-3. GET /agencies/me/signature-credits (solde + seuil +
   grille des packs), lisible par TOUT agent. 4. Les lignes d'exigence
   portent signature_required + signature_status, l'étape porte l'agrégat
   « Signé n/m ». 5. La tâche de signature client porte requirement_id.
   6. Le ledger paginé (agency.manage).

Complément durcissement (29/07) : les lignes portent aussi
signature_request_id (la cible nommable du cancel) et
signature_request_status (la VIVANCE — une annulée ne se déguise plus
en pending).
"""

import json
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from shared.models.signature import SignatureSigner
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.signature_plugin import FakeProvider
from tests.test_signatures import _requests, _signable_case, _signers

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


# --- (1) le flag sur /agencies/me -----------------------------------------------------


async def test_flag_exposed_on_agencies_me(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (await client.get("/agencies/me", headers=agent_headers(admin))).json()
    assert body["signatures_enabled"] is True
    monkeypatch.setenv("SIGNATURES_ENABLED", "false")
    get_settings.cache_clear()
    body = (await client.get("/agencies/me", headers=agent_headers(admin))).json()
    assert body["signatures_enabled"] is False


# --- (2)(3) solde + seuil + grille, lisibles par tout agent ---------------------------


async def test_credits_readable_by_any_agent_with_packs(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    give_credits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SIGNATURE_CREDIT_PACKS",
        json.dumps({"pri_sig500": 500, "pri_sig50": 50, "pri_sig200": 200}),
    )
    get_settings.cache_clear()
    await give_credits(admin.agency_id, 7)
    # Un agent SANS permission (rôle vierge de make_agent) lit le solde :
    # l'activation d'étape peut échouer sur le solde, chacun doit le voir.
    bare = await make_agent(agency_id=admin.agency_id)
    body = (await client.get("/agencies/me/signature-credits", headers=agent_headers(bare))).json()
    assert body["available"] == 7
    assert body["reserved"] == 0
    assert body["low_threshold"] == 10  # défaut config
    # La grille : price_id + crédits, triée par taille croissante. Assertion
    # HERMÉTIQUE : pydantic-settings FUSIONNE les dicts .env + variable
    # d'environnement — un .env local portant les vrais packs sandbox (lot
    # OPS) ajouterait ses entrées et cassait l'égalité stricte. On épingle
    # les 3 posés par le test + l'ordre croissant du tout.
    ours = [p for p in body["packs"] if p["price_id"].startswith("pri_sig")]
    assert ours == [
        {"price_id": "pri_sig50", "credits": 50, "unit_amount": None, "currency": None},
        {"price_id": "pri_sig200", "credits": 200, "unit_amount": None, "currency": None},
        {"price_id": "pri_sig500", "credits": 500, "unit_amount": None, "currency": None},
    ]
    credits_order = [p["credits"] for p in body["packs"]]
    assert credits_order == sorted(credits_order)


async def test_low_threshold_reflects_agency_override(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    from shared.models.agency import Agency

    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.settings = {**(agency.settings or {}), "signature_credits_low_threshold": 25}
    await db_session.commit()
    body = (await client.get("/agencies/me/signature-credits", headers=agent_headers(admin))).json()
    assert body["low_threshold"] == 25


async def test_packs_carry_paddle_amounts_from_cache(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extension 30/07 : la grille SERT unit_amount + currency (relus de
    Paddle, cache TTL — seam de test) pour les ACTIFS ; un pack sans prix
    connu reste None (le fallback front sans prix tient)."""
    from src.signatures import pack_prices as pack_prices_module

    monkeypatch.setenv(
        "SIGNATURE_CREDIT_PACKS",
        json.dumps({"pri_sigp10": 10, "pri_sigp20": 20}),
    )
    get_settings.cache_clear()
    pack_prices_module.override = {"pri_sigp10": (1500, "EUR"), "pri_sigp20": (2400, "EUR")}
    try:
        body = (
            await client.get("/agencies/me/signature-credits", headers=agent_headers(admin))
        ).json()
    finally:
        pack_prices_module.override = None
    ours = [p for p in body["packs"] if p["price_id"].startswith("pri_sigp")]
    assert ours == [
        {"price_id": "pri_sigp10", "credits": 10, "unit_amount": 1500, "currency": "EUR"},
        {"price_id": "pri_sigp20", "credits": 20, "unit_amount": 2400, "currency": "EUR"},
    ]


# --- (4) exigences + agrégat d'étape --------------------------------------------------


async def test_timeline_exposes_signature_state_and_step_aggregate(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = agent_headers(admin)
    await give_credits(admin.agency_id, 5)
    case, progress_id = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="expo",
    )
    case_id = case.id

    def step_of(detail: dict) -> dict:
        return next(s for s in detail["progress"] if s["id"] == progress_id)

    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    step = step_of(detail)
    # L'agrégat « Signé 0/2 » : 2 lignes signables (principal + membre).
    assert (step["signature_signed_count"], step["signature_total"]) == (0, 2)
    by_ref: dict[str, list[dict]] = {}
    for req in step["requirements"]:
        by_ref.setdefault(req["reference"], []).append(req)
    assert all(r["signature_required"] for r in by_ref["Statuts"])
    assert all(r["signature_status"] == "pending" for r in by_ref["Statuts"])
    # Le Kbis classique : non signable, statut de signature None.
    assert by_ref["Kbis"][0]["signature_required"] is False
    assert by_ref["Kbis"][0]["signature_status"] is None

    # Un signataire signe → « Signé 1/2 », SA ligne passe signed.
    monkeypatch.setenv("DOCUSEAL_WEBHOOK_SECRET", "whsec-expo")
    get_settings.cache_clear()
    request = (await _requests(db_session, case_id))[0]
    signer = (await _signers(db_session, request.id))[0]
    signed_row_id = str(signer.case_step_requirement_id)
    r = await client.post(
        "/webhooks/docuseal",
        headers={"X-Docuseal-Secret": "whsec-expo"},
        json={"event_type": "form.completed", "data": {"external_id": str(signer.id)}},
    )
    assert r.status_code == 200, r.text
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    step = step_of(detail)
    assert (step["signature_signed_count"], step["signature_total"]) == (1, 2)
    statuses = {
        r["id"]: r["signature_status"] for r in step["requirements"] if r["signature_required"]
    }
    assert statuses[signed_row_id] == "signed"
    assert sorted(statuses.values()) == ["pending", "signed"]


async def test_timeline_names_the_request_and_its_liveness(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
) -> None:
    """Complément durcissement : id + vivance de la demande par ligne.

    Envoi → les lignes signables nomment la demande (cible du cancel) et
    la voient VIVANTE (sent). Annulation → l'id reste nommé mais la
    vivance dit cancelled (le front réactive « Renvoyer la demande »).
    Re-envoi → nouvel id, à nouveau sent. Le Kbis classique : None/None.
    """
    headers = agent_headers(admin)
    await give_credits(admin.agency_id, 5)
    case, progress_id = await _signable_case(
        client,
        db_session,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email_prefix="live",
    )
    case_id = case.id
    request_id = str((await _requests(db_session, case_id))[0].id)

    def signable_rows(detail: dict) -> list[dict]:
        step = next(s for s in detail["progress"] if s["id"] == progress_id)
        return [r for r in step["requirements"] if r["signature_required"]]

    def classic_row(detail: dict) -> dict:
        step = next(s for s in detail["progress"] if s["id"] == progress_id)
        return next(r for r in step["requirements"] if r["reference"] == "Kbis")

    # Envoi : les 2 lignes signables nomment LA demande, vivante.
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    rows = signable_rows(detail)
    assert len(rows) == 2
    assert all(r["signature_request_id"] == request_id for r in rows)
    assert all(r["signature_request_status"] == "sent" for r in rows)
    # Le Kbis classique ne nomme rien.
    assert classic_row(detail)["signature_request_id"] is None
    assert classic_row(detail)["signature_request_status"] is None

    # Annulation : l'id reste nommé, la vivance dit cancelled.
    r = await client.post(
        f"/cases/{case_id}/signature-requests/{request_id}/cancel", headers=headers
    )
    assert r.status_code == 200, r.text
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    rows = signable_rows(detail)
    assert all(r["signature_request_id"] == request_id for r in rows)
    assert all(r["signature_request_status"] == "cancelled" for r in rows)

    # Re-envoi : nouvelle demande nommée, à nouveau vivante.
    r = await client.post(
        f"/cases/{case_id}/steps/{progress_id}/signature-requests", headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent=1"
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    rows = signable_rows(detail)
    new_ids = {r["signature_request_id"] for r in rows}
    assert new_ids != {request_id}
    assert len(new_ids) == 1
    assert all(r["signature_request_status"] == "sent" for r in rows)


# --- (5) la tâche client porte requirement_id -----------------------------------------


async def test_expat_task_carries_the_requirement_id(
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
    case, _ = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="reqid",
        with_member=False,
    )
    case_id = case.id
    principal = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "reqid-p@example.com"))
    ).scalar_one()
    tasks = (
        await client.get(f"/expat/cases/{case_id}/signatures", headers=expat_headers(principal))
    ).json()
    assert len(tasks) == 1
    signer = (
        await db_session.execute(
            select(SignatureSigner).where(SignatureSigner.id == uuid.UUID(tasks[0]["signer_id"]))
        )
    ).scalar_one()
    assert tasks[0]["requirement_id"] == str(signer.case_step_requirement_id)


# --- (6) le ledger paginé (agency.manage) ---------------------------------------------


async def test_entries_paginated_and_gated(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    give_credits,
) -> None:
    await give_credits(admin.agency_id, 3)
    await give_credits(admin.agency_id, 2)
    await give_credits(admin.agency_id, 1)

    # Un agent sans agency.manage → 403 (surface facturation).
    bare = await make_agent(agency_id=admin.agency_id)
    r = await client.get("/agencies/me/signature-credits/entries", headers=agent_headers(bare))
    assert r.status_code == 403

    body = (
        await client.get(
            "/agencies/me/signature-credits/entries?page=1&page_size=2",
            headers=agent_headers(admin),
        )
    ).json()
    assert body["total"] == 3
    assert body["page_size"] == 2
    assert len(body["items"]) == 2
    assert all(i["kind"] == "purchase" for i in body["items"])
    assert body["items"][0]["amount"] == 1  # plus récent d'abord
    page2 = (
        await client.get(
            "/agencies/me/signature-credits/entries?page=2&page_size=2",
            headers=agent_headers(admin),
        )
    ).json()
    assert len(page2["items"]) == 1
    assert page2["items"][0]["amount"] == 3
