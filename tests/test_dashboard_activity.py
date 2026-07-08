"""GET /dashboard/activity - the "Activite des clients" bento feed.

Covers: (a) STRICT whitelist - an agent/system gesture never appears,
even on whitelisted types, and non-listed types never appear; (b) demo
cases excluded even if an event slips in; (c) same-day aggregation per
(type, case, day) with the exact count and the most recent timestamp;
(d) the 14-day sliding window and the 15-item cap AFTER aggregation;
(e) strict tenant scoping. Types are renamed to the bento vocabulary."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from shared.models.usage import UsageEvent
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def me(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


@pytest_asyncio.fixture
async def case(me: Agent, expat: ExpatUser, make_client_case: MakeClientCase) -> ClientCase:
    return await make_client_case(
        agency_id=me.agency_id, principal_expat_user_id=expat.id, owner_agent_id=me.id
    )


def _event(
    agency_id: uuid.UUID, case_id: uuid.UUID, event_type: str, actor: str, at: datetime
) -> UsageEvent:
    return UsageEvent(
        agency_id=agency_id,
        case_id=case_id,
        actor_type=actor,
        event_type=event_type,
        details={},
        created_at=at,
    )


async def _feed(client: AsyncClient, headers: dict[str, str]) -> list[dict]:
    response = await client.get("/dashboard/activity", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


# --- (a) + (b) + (e): whitelist, demo, tenant ----------------------------------------


async def test_whitelist_demo_and_tenant_scoping(
    client: AsyncClient,
    db_session: AsyncSession,
    me: Agent,
    expat: ExpatUser,
    case: ClientCase,
    make_client_case: MakeClientCase,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    now = datetime.now(UTC)
    demo = await make_client_case(
        agency_id=me.agency_id, principal_expat_user_id=expat.id, is_demo=True
    )
    other_admin = await make_agent(role=system_roles["admin"], email="other@tenant.com")
    other_case = await make_client_case(
        agency_id=other_admin.agency_id, principal_expat_user_id=expat.id
    )

    db_session.add_all(
        [
            # IN: the four client gestures, renamed in the response.
            _event(me.agency_id, case.id, "document.added", "expat", now),
            _event(me.agency_id, case.id, "message.sent", "expat", now - timedelta(hours=1)),
            _event(me.agency_id, case.id, "case.step_validated", "expat", now - timedelta(hours=2)),
            _event(
                me.agency_id,
                case.id,
                "case.client_account_activated",
                "expat",
                now - timedelta(hours=3),
            ),
            # OUT (a): the SAME types, agency/system gestures.
            _event(me.agency_id, case.id, "document.added", "agent", now),
            _event(me.agency_id, case.id, "message.sent", "agent", now),
            _event(me.agency_id, case.id, "case.step_validated", "system", now),
            # OUT (a): not whitelisted.
            _event(me.agency_id, case.id, "case.created", "agent", now),
            _event(me.agency_id, case.id, "journey.created", "agent", now),
            # OUT (b): demo case, whitelisted type and expat actor.
            _event(me.agency_id, demo.id, "document.added", "expat", now),
            # OUT (e): another tenant.
            _event(other_admin.agency_id, other_case.id, "document.added", "expat", now),
        ]
    )
    await db_session.commit()

    items = await _feed(client, agent_headers(me))
    assert [i["type"] for i in items] == [
        "documents_uploaded",
        "comment_added",
        "step_validated",
        "account_activated",
    ]  # bento vocabulary, newest first, nothing else
    assert all(i["count"] == 1 for i in items)
    assert all(i["client_name"] == "Marie Curie" for i in items)
    assert all(i["expat_user_id"] == str(expat.id) for i in items)

    theirs = await _feed(client, agent_headers(other_admin))
    assert [i["type"] for i in theirs] == ["documents_uploaded"]  # only its own


# --- (c) same-day aggregation ---------------------------------------------------------


async def test_same_day_gestures_aggregate_with_count(
    client: AsyncClient,
    db_session: AsyncSession,
    me: Agent,
    expat: ExpatUser,
    case: ClientCase,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    base = datetime.now(UTC).replace(hour=12)  # keep the trio same-day in UTC
    other_case = await make_client_case(agency_id=me.agency_id, principal_expat_user_id=expat.id)
    db_session.add_all(
        [
            # 3 deposits, same client, same day -> ONE line, count 3,
            # occurred_at = the most recent of the group.
            _event(me.agency_id, case.id, "document.added", "expat", base),
            _event(me.agency_id, case.id, "document.added", "expat", base - timedelta(hours=1)),
            _event(me.agency_id, case.id, "document.added", "expat", base - timedelta(hours=2)),
            # Same type, same day, OTHER case -> its own line.
            _event(me.agency_id, other_case.id, "document.added", "expat", base),
            # Same type, same case, PREVIOUS day -> its own line.
            _event(me.agency_id, case.id, "document.added", "expat", base - timedelta(days=1)),
            # Other type, same case, same day -> its own line.
            _event(me.agency_id, case.id, "message.sent", "expat", base),
        ]
    )
    await db_session.commit()

    items = await _feed(client, agent_headers(me))
    assert len(items) == 4  # trio collapsed + other case + previous day + comment
    grouped = next(
        i
        for i in items
        if i["type"] == "documents_uploaded" and i["case_id"] == str(case.id) and i["count"] == 3
    )
    assert grouped["occurred_at"].startswith(base.strftime("%Y-%m-%dT%H"))  # most recent
    assert sorted(i["count"] for i in items) == [1, 1, 1, 3]
    assert sum(1 for i in items if i["type"] == "documents_uploaded") == 3
    assert sum(1 for i in items if i["type"] == "comment_added") == 1


# --- (d) window and cap ----------------------------------------------------------------


async def test_window_and_cap_after_aggregation(
    client: AsyncClient,
    db_session: AsyncSession,
    me: Agent,
    expat: ExpatUser,
    case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    now = datetime.now(UTC)
    # 21 aggregated groups INSIDE the window (7 days x 3 gesture types)
    # + one event far beyond the 14 sliding days.
    rows = []
    for i in range(7):
        at = now - timedelta(days=i, hours=1)
        for event_type in ("document.added", "message.sent", "case.step_validated"):
            rows.append(_event(me.agency_id, case.id, event_type, "expat", at))
    rows.append(_event(me.agency_id, case.id, "document.added", "expat", now - timedelta(days=20)))
    db_session.add_all(rows)
    await db_session.commit()

    items = await _feed(client, agent_headers(me))
    assert len(items) == 15  # cap AFTER aggregation (21 groups qualified)
    occurred = [datetime.fromisoformat(i["occurred_at"].replace("Z", "+00:00")) for i in items]
    assert occurred == sorted(occurred, reverse=True)  # newest first
    # Nothing beyond the 14-day window ever enters, even below the cap.
    assert min(occurred) >= now - timedelta(days=14)
