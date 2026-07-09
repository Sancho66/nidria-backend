"""Adoption dashboard (Phase 2, Eric) projected on GET /admin/agencies: the 3
onboarding gestures + S0/S1/S2 + last_login heartbeat, batched (no N+1), plus
the trial/onboarding filters. The superadmin gate itself is re-proven by
test_admin_agencies.test_superadmin_sees_the_table_others_are_403 (agency admin
→ 403), which still passes with these new fields."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from shared.models.usage import AgencyUsageMilestone, UsageEvent
from src.nurture.nurture_job import _usage_state
from src.usage.usage_manager import UsageManager, classify_usage_state
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

_T = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"], email="root@platform.io")


async def _row_for(client: AsyncClient, headers: dict, agency_id) -> dict:
    body = (await client.get("/admin/agencies?page_size=100", headers=headers)).json()
    return next(r for r in body["items"] if r["id"] == str(agency_id))


# --- the row projects onboarding + state + last_login (MAX of agents) ----------------


async def test_row_projects_onboarding_usage_state_and_last_login(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency(name="Adopt")
    a_late = await make_agent(agency_id=agency.id, email="late@x.io")
    a_early = await make_agent(agency_id=agency.id, email="early@x.io")
    await db_session.execute(update(Agent).where(Agent.id == a_late.id).values(last_login_at=_T))
    await db_session.execute(
        update(Agent).where(Agent.id == a_early.id).values(last_login_at=_T - timedelta(days=10))
    )
    # A dossier opened + a "view as client" done — but NO journey created.
    db_session.add(
        AgencyUsageMilestone(agency_id=agency.id, key="premier_dossier_cree", first_at=_T, count=1)
    )
    db_session.add(
        UsageEvent(agency_id=agency.id, actor_type="agent", event_type="case.viewed_as_client")
    )
    await db_session.commit()

    row = await _row_for(client, agent_headers(superadmin), agency.id)
    gestures = {g["key"]: g["done"] for g in row["onboarding"]}
    assert gestures == {"create_journey": False, "open_case": True, "view_as_client": True}
    assert row["usage_state"] == "S1"  # a dossier, no activated client
    assert row["last_login_at"].startswith("2026-07-01")  # MAX of the two agents


# --- classify_usage_state: ONE source, three feeders agree ---------------------------


async def test_classify_usage_state_is_single_source_across_feeders(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_agency: MakeAgency,
) -> None:
    agency = await make_agency(name="Feeders")
    db_session.add(
        AgencyUsageMilestone(agency_id=agency.id, key="premier_dossier_cree", first_at=_T, count=1)
    )
    await db_session.commit()

    keys = {"premier_dossier_cree"}
    pure = classify_usage_state(keys)
    async_feeder = await UsageManager(db_session).compute_usage_state(agency.id)
    with sync_session_local() as db:
        sync_feeder = _usage_state(db, agency.id)
    # The pure rule, the async manager and the sync nurture cron: same state.
    assert pure == async_feeder == sync_feeder == "S1"


# --- last_login is posed at LOGIN, NEVER at refresh ----------------------------------


async def test_login_sets_last_login_but_refresh_does_not(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
) -> None:
    await make_agent(email="hb@x.io", password="pw12345678")
    login = await client.post(
        "/auth/agent/login", json={"email": "hb@x.io", "password": "pw12345678"}
    )
    assert login.status_code == 200, login.text
    refresh_token = login.json()["refresh_token"]

    db_session.expire_all()
    after_login = (
        await db_session.execute(select(Agent.last_login_at).where(Agent.email == "hb@x.io"))
    ).scalar_one()
    assert after_login is not None  # LOGIN posed the heartbeat

    # Overwrite to a detectable MARKER, then refresh: it must NOT be touched.
    marker = datetime(2020, 1, 1, tzinfo=UTC)
    await db_session.execute(
        update(Agent).where(Agent.email == "hb@x.io").values(last_login_at=marker)
    )
    await db_session.commit()
    refreshed = await client.post("/auth/agent/refresh", json={"refresh_token": refresh_token})
    assert refreshed.status_code == 200, refreshed.text

    db_session.expire_all()
    after_refresh = (
        await db_session.execute(select(Agent.last_login_at).where(Agent.email == "hb@x.io"))
    ).scalar_one()
    assert after_refresh == marker  # a refresh is the SAME session, not a login


# --- filters: "who expires soon and hasn't started" (combinable, SQL, pre-pagination) -


async def _stub_trial(db: AsyncSession, agency_id, days: int) -> None:
    await db.execute(
        update(Agency)
        .where(Agency.id == agency_id)
        .values(trial_ends_at=datetime.now(UTC) + timedelta(days=days), converted_at=None)
    )


async def test_filter_trial_expiring_and_onboarding_incomplete(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    make_agency: MakeAgency,
    agent_headers: AuthHeaders,
) -> None:
    expiring = await make_agency(name="Expiring")
    await _stub_trial(db_session, expiring.id, days=3)  # soon, no gestures → incomplete
    later = await make_agency(name="Later")
    await _stub_trial(db_session, later.id, days=30)  # not soon
    started = await make_agency(name="Started")
    await _stub_trial(db_session, started.id, days=3)  # soon BUT onboarding complete
    for key in ("premier_parcours_cree", "premier_dossier_cree"):
        db_session.add(AgencyUsageMilestone(agency_id=started.id, key=key, first_at=_T, count=1))
    db_session.add(
        UsageEvent(agency_id=started.id, actor_type="agent", event_type="case.viewed_as_client")
    )
    await db_session.commit()
    h = agent_headers(superadmin)

    soon = (
        await client.get("/admin/agencies?trial_expiring_within_days=7&page_size=100", headers=h)
    ).json()
    soon_ids = {r["id"] for r in soon["items"]}
    assert {str(expiring.id), str(started.id)} <= soon_ids and str(later.id) not in soon_ids

    # Eric's real question: expiring soon AND hasn't started → 'started' drops out.
    urgent = (
        await client.get(
            "/admin/agencies?trial_expiring_within_days=7&onboarding_incomplete=true&page_size=100",
            headers=h,
        )
    ).json()
    urgent_ids = {r["id"] for r in urgent["items"]}
    assert str(expiring.id) in urgent_ids
    assert str(started.id) not in urgent_ids and str(later.id) not in urgent_ids
