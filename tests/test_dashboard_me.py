"""Agent-centric dashboard (GET /dashboard/me) — the "dashboard of action".

The headline is SERVER-SIDE ISOLATION: an agent sees ONLY its own actions
(responsible OR validator == me), proven at the data level — a colleague in
the same agency sees an EMPTY board while I see mine (so the filter is in the
query, not a front mask), and an agent of another tenant sees nothing of mine.
Plus the 4 figures, the unified to-do (badges, overdue-first sort, blocked
shown-not-hidden) and the weekly load shape.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

PAST = "2020-01-01T00:00:00Z"


@pytest.fixture
def dm(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def me(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"], first_name="Alex", last_name="M")


@pytest_asyncio.fixture
async def colleague(me: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """Same agency as `me`, no actions assigned → its board must be empty."""
    return await make_agent(agency_id=me.agency_id, role=system_roles["admin"], email="col@x.com")


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


async def _scenario(
    dm: AsyncClient,
    ah: dict[str, str],
    me: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
) -> ClientCase:
    """A case owned by `me` with three steps:
    p0 — responsible=me, in_progress, deadline PAST  → to_realize + overdue
    p1 — validator=me, in_progress                   → to_validate
    p2 — responsible=me, TODO, prerequisite p0 unmet → to_realize + blocked
    """
    tid = (await dm.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    sids = [
        (await dm.post(f"/journeys/{tid}/steps", headers=ah, json={"name": f"S{i}"})).json()["id"]
        for i in range(3)
    ]
    # S2 requires S0.
    await dm.put(
        f"/journeys/{tid}/steps/{sids[2]}/prerequisites",
        headers=ah,
        json={"prerequisite_step_ids": [sids[0]]},
    )
    case = await make_client_case(
        agency_id=me.agency_id,
        principal_expat_user_id=expat.id,
        owner_agent_id=me.id,
        status="in_progress",
    )
    steps = (
        await dm.post(f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid})
    ).json()
    p = [s["id"] for s in steps]  # ordered by position → p[0],p[1],p[2]

    # p0: my responsibility, started, overdue.
    await dm.put(
        f"/cases/{case.id}/steps/{p[0]}/responsible",
        headers=ah,
        json={"responsible_type": "agent", "responsible_agent_id": str(me.id)},
    )
    await dm.patch(
        f"/cases/{case.id}/steps/{p[0]}", headers=ah, json={"status": "in_progress", "due_at": PAST}
    )
    # p1: I validate it, active.
    await dm.put(
        f"/cases/{case.id}/steps/{p[1]}/validator",
        headers=ah,
        json={"validated_by_type": "agent", "validated_by_agent_id": str(me.id)},
    )
    await dm.patch(f"/cases/{case.id}/steps/{p[1]}", headers=ah, json={"status": "in_progress"})
    # p2: my responsibility, blocked by p0 (not done), left TODO.
    await dm.put(
        f"/cases/{case.id}/steps/{p[2]}/responsible",
        headers=ah,
        json={"responsible_type": "agent", "responsible_agent_id": str(me.id)},
    )
    return case


# --- THE ISOLATION TEST (server-side, the critical one) ------------------------------


async def test_dashboard_me_isolation_same_agency(
    dm: AsyncClient,
    me: Agent,
    colleague: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """`me` sees its 3 actions; the COLLEAGUE (same agency, no actions) sees
    an EMPTY board → the filter is on agent.id in the query, not a front mask."""
    await _scenario(dm, agent_headers(me), me, expat, make_client_case)

    mine = (await dm.get("/dashboard/me", headers=agent_headers(me))).json()
    assert mine["counts"] == {"to_realize": 2, "to_validate": 1, "my_cases": 1, "overdue": 1}
    assert len(mine["todo"]) == 3

    # The colleague shares the agency and the cases, yet sees NOTHING — the
    # actions are not theirs.
    theirs = (await dm.get("/dashboard/me", headers=agent_headers(colleague))).json()
    assert theirs["counts"] == {"to_realize": 0, "to_validate": 0, "my_cases": 0, "overdue": 0}
    assert theirs["todo"] == []


async def test_dashboard_me_cross_tenant(
    dm: AsyncClient,
    me: Agent,
    expat: ExpatUser,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """An agent of ANOTHER agency never sees my actions (tenant isolation)."""
    await _scenario(dm, agent_headers(me), me, expat, make_client_case)
    # Authorized admin of ANOTHER agency (has case.view) → request passes
    # RBAC; tenant isolation is proven by the empty board (query-level).
    stranger = await make_agent(role=system_roles["admin"])
    board = (await dm.get("/dashboard/me", headers=agent_headers(stranger))).json()
    assert board["counts"] == {"to_realize": 0, "to_validate": 0, "my_cases": 0, "overdue": 0}
    assert board["todo"] == []


# --- the to-do: badges, overdue-first sort, blocked shown-not-hidden -----------------


async def test_dashboard_me_todo_badges_sort_and_blocked(
    dm: AsyncClient,
    me: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await _scenario(dm, agent_headers(me), me, expat, make_client_case)
    body = (await dm.get("/dashboard/me", headers=agent_headers(me))).json()
    todo = body["todo"]

    # Overdue first.
    assert todo[0]["is_overdue"] is True
    assert todo[0]["badge"] == "to_realize"

    by_name = {i["step_name"]: i for i in todo}
    # The validated step → "to_validate" badge.
    assert by_name["S1"]["badge"] == "to_validate"
    # The blocked step is PRESENT (not hidden) and flagged.
    assert by_name["S2"]["is_blocked"] is True
    assert by_name["S2"]["badge"] == "to_realize"
    # Client + country surfaced; the case is clickable via case_id.
    assert by_name["S0"]["client_name"] == "Marie Curie"
    assert all(i["case_id"] == str(case.id) for i in todo)


async def test_dashboard_me_status_and_weekly_shape(
    dm: AsyncClient,
    me: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    await _scenario(dm, agent_headers(me), me, expat, make_client_case)
    body = (await dm.get("/dashboard/me", headers=agent_headers(me))).json()
    assert body["first_name"] == "Alex"
    assert body["by_status"] == {"in_progress": 1}  # my one active case
    # Weekly load = 7 days (rolling today→+6), well-formed.
    assert len(body["weekly_load"]) == 7
    assert all(set(d) == {"date", "count"} for d in body["weekly_load"])


async def test_dashboard_me_weekly_load_actionable(
    dm: AsyncClient,
    me: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """NID-17 — "Ma charge" mirrors the "À traiter" queue over a ROLLING
    today→+6 window. From `_scenario`: p0 (overdue, agency-validated on my
    case) and p1 (my validation, undated) both land on today; p2 (TODO, not in
    the queue) is out. Dating p1 in-window then moves it onto its own bar."""
    ah = agent_headers(me)
    await _scenario(dm, ah, me, expat, make_client_case)

    board = (await dm.get("/dashboard/me", headers=ah)).json()
    load = board["weekly_load"]
    assert len(load) == 7
    # today's bar = p0 (overdue → today) + p1 (undated → today); p2 blocked out.
    assert load[0]["count"] == 2
    assert all(d["count"] == 0 for d in load[1:])
    assert sum(d["count"] for d in load) == 2

    # Give p1 a firm deadline three days out → it leaves today for its own day.
    p1 = next(i for i in board["todo"] if i["step_name"] == "S1")
    due = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    await dm.patch(
        f"/cases/{p1['case_id']}/steps/{p1['progress_id']}", headers=ah, json={"due_at": due}
    )
    load2 = (await dm.get("/dashboard/me", headers=ah)).json()["weekly_load"]
    target = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
    assert load2[0]["count"] == 1  # only p0 (overdue) remains on today
    assert next(d for d in load2 if d["date"] == target)["count"] == 1  # p1 on its day
    assert sum(d["count"] for d in load2) == 2


async def test_weekly_load_follows_worklist_not_counts(
    dm: AsyncClient,
    me: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """NID-17 — the exact reported gap: a step validated by "the agency" (no
    named person — the DEFAULT) on a case I OWN is invisible to the me-named
    counts (à-valider stays 0), yet it MUST appear in "Ma charge" because it is
    in the "À traiter" queue. Overdue → today."""
    ah = agent_headers(me)
    tid = (await dm.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    await dm.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "S"})
    case = await make_client_case(
        agency_id=me.agency_id,
        principal_expat_user_id=expat.id,
        owner_agent_id=me.id,
        status="in_progress",
    )
    p = (
        await dm.post(f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid})
    ).json()[0]["id"]
    # Default validator = "the agency" (type=agent, no named agent); make it
    # active + overdue. Responsible/validator are NOT me by name.
    await dm.patch(
        f"/cases/{case.id}/steps/{p}", headers=ah, json={"status": "in_progress", "due_at": PAST}
    )

    body = (await dm.get("/dashboard/me", headers=ah)).json()
    # Counts stay strictly me-named → this agency-level step counts for none.
    assert body["counts"]["to_validate"] == 0
    assert body["counts"]["to_realize"] == 0
    assert body["todo"] == []
    # But "Ma charge" follows the queue → the overdue agency validation on my
    # case lands on today.
    assert body["weekly_load"][0]["count"] == 1
    assert sum(d["count"] for d in body["weekly_load"]) == 1


async def test_dashboard_me_excludes_closed_and_validated(
    dm: AsyncClient,
    me: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """A closed/validated case I own is NOT in my active count (D2)."""
    ah = agent_headers(me)
    await make_client_case(
        agency_id=me.agency_id,
        principal_expat_user_id=expat.id,
        owner_agent_id=me.id,
        status="closed",
    )
    body = (await dm.get("/dashboard/me", headers=ah)).json()
    assert body["counts"]["my_cases"] == 0
    assert body["by_status"] == {}
