"""Usage trackers bloc 1: event layer + milestone aggregate + trial model.

Covers: (a) a representative emitter per domain writes its typed event;
(b) first_at is immutable, count increments; (c) replay rebuilds the
same aggregate; (d) backfill poses real-dated milestones on a pre-tracker
agency and re-running is a no-op; (e) demo cases emit NOTHING anywhere;
(f) S0 → S1 → S2 on a full scenario; (g) the wizard starts the 30-day
trial and emits agency.activated."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.invitation import CaseInvitation
from shared.models.rbac import Role
from shared.models.usage import AgencyUsageMilestone, UsageEvent
from src.usage.usage_backfill import backfill_usage_milestones, replay_usage_milestones
from src.usage.usage_manager import UsageManager
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _events(db: AsyncSession, agency_id: uuid.UUID) -> list[str]:
    stmt = (
        select(UsageEvent.event_type)
        .where(UsageEvent.agency_id == agency_id)
        .order_by(UsageEvent.created_at)
    )
    return list((await db.execute(stmt)).scalars())


async def _milestones(db: AsyncSession, agency_id: uuid.UUID) -> dict[str, AgencyUsageMilestone]:
    stmt = select(AgencyUsageMilestone).where(AgencyUsageMilestone.agency_id == agency_id)
    return {m.key: m for m in (await db.execute(stmt)).scalars()}


def _case_payload(email_addr: str, journey_template_id: str) -> dict[str, str]:
    return {
        "first_name": "Jean",
        "last_name": "Martin",
        "email": email_addr,
        "origin_country": "FR",
        "dest_country": "PY",
        "journey_template_id": journey_template_id,
    }


# --- (a) + (g): representative emitters, wizard trial ------------------------------------


async def test_wizard_starts_trial_and_emits_activation(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"])
    created = await client.post(
        "/agencies",
        headers=agent_headers(superadmin),
        json={
            "name": "Trial Agency",
            "admin_email": "trial-admin@example.com",
            "admin_first_name": "A",
            "admin_last_name": "B",
        },
    )
    assert created.status_code == 201, created.text
    agency_id = uuid.UUID(created.json()["agency"]["id"])
    agency = await db_session.get(Agency, agency_id)
    assert agency is not None and agency.trial_ends_at is not None
    lifetime = agency.trial_ends_at - datetime.now(UTC)
    assert timedelta(days=29) < lifetime < timedelta(days=31)
    assert "agency.activated" in await _events(db_session, agency_id)
    assert "agence_activee" in await _milestones(db_session, agency_id)


async def test_emitters_write_their_events(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    # Journeys: template + step; CRM mapping.
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S"})
    await client.post(
        "/imports/mappings",
        headers=headers,
        json={
            "journey_template_id": tid,
            "crm_slug": "hubspot-crm",
            "name": "M",
            "mapping": {"Email": "email", "First": "first_name", "Last": "last_name"},
        },
    )
    # Case + journey + step worked to DONE; comment, reminder, status, export.
    created = await client.post(
        "/cases", headers=headers, json=_case_payload("track@example.com", tid)
    )
    case_id = created.json()["id"]
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    pid = detail["progress"][0]["id"]
    await client.patch(
        f"/cases/{case_id}/steps/{pid}", headers=headers, json={"status": "in_progress"}
    )
    await client.patch(f"/cases/{case_id}/steps/{pid}", headers=headers, json={"status": "done"})
    await client.post(
        f"/cases/{case_id}/steps/{pid}/comments", headers=headers, json={"body": "hello"}
    )
    await client.patch(f"/cases/{case_id}", headers=headers, json={"status": "in_progress"})
    assert (await client.get(f"/cases/{case_id}/export", headers=headers)).status_code == 200
    # Custom field + member invitation + impersonation of the client.
    await client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={"key": "budget", "label": "Budget", "field_type": "text"},
    )
    await client.post(
        "/agencies/me/invitations",
        headers=headers,
        json={
            "email": "new@example.com",
            "role_id": str(
                (
                    await db_session.execute(
                        select(Role).where(Role.name == "member", Role.is_system)
                    )
                )
                .scalar_one()
                .id
            ),
        },
    )
    events = await _events(db_session, admin.agency_id)
    for expected in (
        "journey.created",
        "journey.step_added",
        "journey.crm_mapping_set",
        "case.created",
        "case.client_invited",
        "case.assigned",
        "case.step_validated",
        "message.sent",
        "case.status_changed",
        "case.exported_pdf",
        "agency.custom_fields_set",
        "member.invited",
    ):
        assert expected in events, expected

    milestones = await _milestones(db_session, admin.agency_id)
    for key in (
        "premier_parcours_cree",
        "premier_dossier_cree",
        "premier_client_invite",
        "premiere_etape_validee",
        "premier_message_envoye",
        "premier_export_pdf",
        "champs_perso_configures",
        "premier_membre_invite",
    ):
        assert key in milestones, key


# --- (b) first_at immutable -----------------------------------------------------------------


async def test_first_at_immutable_count_increments(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    agency_id = admin.agency_id
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await client.post("/cases", headers=headers, json=_case_payload("one@example.com", tid))
    first = (await _milestones(db_session, agency_id))["premier_dossier_cree"]
    original_first_at, original_count = first.first_at, first.count

    await client.post("/cases", headers=headers, json=_case_payload("two@example.com", tid))
    db_session.expire_all()
    second = (await _milestones(db_session, agency_id))["premier_dossier_cree"]
    assert second.first_at == original_first_at
    assert second.count == original_count + 1


# --- (c) replay rebuilds the same aggregate ---------------------------------------------------


async def test_replay_matches_incremental(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await client.post("/cases", headers=headers, json=_case_payload("rp1@example.com", tid))
    await client.post("/cases", headers=headers, json=_case_payload("rp2@example.com", tid))
    incremental = {
        k: (m.first_at, m.count)
        for k, m in (await _milestones(db_session, admin.agency_id)).items()
    }

    await replay_usage_milestones(db_session, admin.agency_id)
    rebuilt = {
        k: (m.first_at, m.count)
        for k, m in (await _milestones(db_session, admin.agency_id)).items()
    }
    # Same keys, same counts; first_at equal within the clock-source
    # tolerance (events stamp app time, state rows stamp DB time).
    assert set(rebuilt) >= set(incremental)
    for key, (first_at, count) in incremental.items():
        r_first, r_count = rebuilt[key]
        assert r_count == count, key
        assert abs((r_first - first_at).total_seconds()) < 5, key
    # Idempotence: a second replay is byte-identical.
    again = await replay_usage_milestones(db_session, admin.agency_id)
    assert {k: v for k, v in again.items()} == {
        k: (m.first_at, m.count)
        for k, m in (await _milestones(db_session, admin.agency_id)).items()
    }


# --- (d) backfill on a pre-tracker agency ------------------------------------------------------


async def test_backfill_poses_real_dates_and_reruns_noop(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    # History built via FIXTURES (no events — the pre-tracker era) with an
    # activated principal.
    expat = await make_expat_user()  # activated by default
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    agency_id = admin.agency_id
    case_created_at = case.created_at
    # Wipe whatever milestones exist to simulate the fresh deploy.
    await db_session.execute(
        delete(AgencyUsageMilestone).where(AgencyUsageMilestone.agency_id == agency_id)
    )
    await db_session.commit()

    inserted = await backfill_usage_milestones(db_session)
    assert inserted > 0
    milestones = await _milestones(db_session, agency_id)
    assert milestones["premier_dossier_cree"].first_at == case_created_at  # the REAL date
    assert "premier_client_compte_active" in milestones  # active principal detected
    assert "agence_activee" in milestones

    # Idempotent: nothing inserted, nothing touched on the second run.
    before = {k: (m.first_at, m.count) for k, m in milestones.items()}
    assert await backfill_usage_milestones(db_session) == 0
    db_session.expire_all()
    after = {
        k: (m.first_at, m.count) for k, m in (await _milestones(db_session, agency_id)).items()
    }
    assert after == before


# --- (e) demo cases emit nothing ---------------------------------------------------------------


async def test_demo_cases_excluded_everywhere(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    demo = await make_client_case(agency_id=admin.agency_id, is_demo=True)
    # An emitter on the demo case: PATCH status writes NO event.
    patched = await client.patch(
        f"/cases/{demo.id}", headers=agent_headers(admin), json={"status": "in_progress"}
    )
    assert patched.status_code == 200
    assert await _events(db_session, admin.agency_id) == []

    # Backfill ignores it; counters too; the state stays S0.
    await backfill_usage_milestones(db_session)
    milestones = await _milestones(db_session, admin.agency_id)
    assert "premier_dossier_cree" not in milestones
    usage = UsageManager(db_session)
    counters = await usage.counters(admin.agency_id)
    assert counters["nb_dossiers"] == 0
    assert await usage.compute_usage_state(admin.agency_id) == "S0"


# --- (f) S0 -> S1 -> S2 -------------------------------------------------------------------------


async def test_usage_state_progression(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    agency_id = admin.agency_id
    usage = UsageManager(db_session)
    assert await usage.compute_usage_state(agency_id) == "S0"

    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    created = await client.post(
        "/cases", headers=headers, json=_case_payload("journey-client@example.com", tid)
    )
    assert created.status_code == 201
    assert await usage.compute_usage_state(agency_id) == "S1"

    # The client activates through the case invitation: THE adoption signal.
    invitation = (
        await db_session.execute(
            select(CaseInvitation).where(CaseInvitation.case_id == uuid.UUID(created.json()["id"]))
        )
    ).scalar_one()
    activated = await client.post(
        "/auth/expat/activate",
        json={"token": invitation.token, "password": "fresh-password-1"},
    )
    assert activated.status_code == 200, activated.text
    db_session.expire_all()
    assert await usage.compute_usage_state(agency_id) == "S2"
    events = await _events(db_session, agency_id)
    assert "case.client_account_activated" in events
    counters = await usage.counters(agency_id)
    assert counters["nb_dossiers"] == 1
    assert counters["nb_dossiers_avec_client_actif"] == 1
