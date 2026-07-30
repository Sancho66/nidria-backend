"""Lot superadmin (30/07) — essai ajustable + crédits de signature offerts.

L'essai en JOURS AJOUTÉS (ancre max(now, fin actuelle) — le passé est
structurellement impossible) ; agence convertie → 422 nommé. Le grant est
une écriture kind=grant aux invariants du ledger (jamais un update nu),
un crédit comme un autre au solde (aucune priorité payé/offert), distinct
à l'historique, note stockée sur l'écriture. Gate plateforme strict
(agency.create — superadmin seul)."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.signatures import ledger
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"])


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


# --- essai ----------------------------------------------------------------------------


async def test_extend_trial_anchors_on_max_now_or_current_end(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(superadmin)
    agency_id = admin.agency_id
    now = datetime.now(UTC)
    # Essai encore VIVANT (fin dans 5 jours) : +10 jours s'ancrent sur la
    # fin actuelle, pas sur maintenant.
    agency = await db_session.get(Agency, agency_id)
    assert agency is not None
    current_end = now + timedelta(days=5)
    agency.trial_ends_at = current_end
    await db_session.commit()
    r = await client.patch(
        f"/agencies/{agency_id}/trial", headers=headers, json={"extend_days": 10}
    )
    assert r.status_code == 200, r.text
    ends = datetime.fromisoformat(r.json()["trial_ends_at"])
    assert abs((ends - (current_end + timedelta(days=10))).total_seconds()) < 2

    # Essai EXPIRÉ (fin passée) : l'ancre devient maintenant — jamais une
    # fin dans le passé, structurellement.
    agency = await db_session.get(Agency, agency_id)
    assert agency is not None
    agency.trial_ends_at = now - timedelta(days=30)
    await db_session.commit()
    r = await client.patch(f"/agencies/{agency_id}/trial", headers=headers, json={"extend_days": 7})
    assert r.status_code == 200, r.text
    ends = datetime.fromisoformat(r.json()["trial_ends_at"])
    assert ends > datetime.now(UTC)
    assert abs((ends - (now + timedelta(days=7))).total_seconds()) < 5


async def test_extend_trial_refuses_a_converted_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.plan = "agence"
    agency.converted_at = datetime.now(UTC)
    await db_session.commit()
    r = await client.patch(
        f"/agencies/{admin.agency_id}/trial",
        headers=agent_headers(superadmin),
        json={"extend_days": 10},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "trial.already_converted"


# --- crédits offerts ------------------------------------------------------------------


async def test_grant_credits_updates_balance_and_writes_distinct_entry(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    agency_id = admin.agency_id
    r = await client.post(
        f"/agencies/{agency_id}/signature-credits/grant",
        headers=agent_headers(superadmin),
        json={"credits": 25, "note": "partenaire test"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"granted": 25, "available": 25, "reserved": 0}
    # L'invariant comptable tient (balance == dérivation des écritures).
    assert await ledger.balance(db_session, agency_id) == (25, 0)
    assert await ledger.derived_balance(db_session, agency_id) == (25, 0)
    # L'historique agence l'affiche DISTINCTEMENT : kind=grant + la note
    # + qui l'a posé, sur l'écriture.
    entries = (
        await client.get("/agencies/me/signature-credits/entries", headers=agent_headers(admin))
    ).json()["items"]
    grant = next(e for e in entries if e["kind"] == "grant")
    assert grant["amount"] == 25
    assert grant["details"]["note"] == "partenaire test"
    assert grant["details"]["granted_by_agent_id"] == str(superadmin.id)


async def test_consume_mixes_paid_and_granted_without_priority(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
    give_credits,
    make_client_case,
) -> None:
    """Le solde est GLOBAL : payé + offert se mélangent, la réservation ne
    connaît aucune provenance (FIFO implicite du solde — rien à inventer)."""
    agency_id = admin.agency_id
    await give_credits(agency_id, 1)  # 1 payé
    r = await client.post(
        f"/agencies/{agency_id}/signature-credits/grant",
        headers=agent_headers(superadmin),
        json={"credits": 1},
    )
    assert r.status_code == 200, r.text
    assert await ledger.balance(db_session, agency_id) == (2, 0)
    # Deux réservations passent — la seconde puise indifféremment. (Un
    # dossier + une étape minimaux : la demande exige un ancrage réel.)
    from shared.models.case_step_progress import CaseStepProgress
    from shared.models.journey import JourneyTemplate, JourneyTemplateStep
    from shared.models.signature import SignatureRequest
    from src.core.enums import SignatureProviderKind, SignatureRequestStatus

    case = await make_client_case(agency_id=agency_id)
    journey = JourneyTemplate(agency_id=agency_id, name="Mix")
    db_session.add(journey)
    await db_session.flush()
    step = JourneyTemplateStep(template_id=journey.id, name="Mix", position=0)
    db_session.add(step)
    await db_session.flush()
    progress = CaseStepProgress(case_id=case.id, template_step_id=step.id, status="in_progress")
    db_session.add(progress)
    await db_session.flush()
    for _ in range(2):
        request = SignatureRequest(
            case_id=case.id,
            case_step_progress_id=progress.id,
            step_requirement_id=None,
            reference="Mix",
            provider=SignatureProviderKind.DOCUSEAL.value,
            level="ses",
            status=SignatureRequestStatus.DRAFT.value,
        )
        db_session.add(request)
        await db_session.flush()
        await ledger.reserve_credit(db_session, agency_id, request)
    await db_session.commit()
    assert await ledger.balance(db_session, agency_id) == (0, 2)
    assert await ledger.derived_balance(db_session, agency_id) == (0, 2)


async def test_grant_bounds_and_unknown_agency(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    import uuid as uuid_mod

    headers = agent_headers(superadmin)
    r = await client.post(
        f"/agencies/{uuid_mod.uuid4()}/signature-credits/grant",
        headers=headers,
        json={"credits": 10},
    )
    assert r.status_code == 404
    unknown = await client.post(
        f"/agencies/{uuid_mod.uuid4()}/signature-credits/grant",
        headers=headers,
        json={"credits": 1001},
    )
    assert unknown.status_code == 422  # borne 1..1000 (pydantic)


# --- gate plateforme strict -----------------------------------------------------------


async def test_normal_agent_is_403_on_both(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    r = await client.patch(
        f"/agencies/{admin.agency_id}/trial", headers=headers, json={"extend_days": 5}
    )
    assert r.status_code == 403
    r = await client.post(
        f"/agencies/{admin.agency_id}/signature-credits/grant",
        headers=headers,
        json={"credits": 5},
    )
    assert r.status_code == 403
