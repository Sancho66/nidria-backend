"""GET /cases — derived per-case URGENCY (NID-10). Priority overdue >
to_validate > awaiting_client > neutral, the SAME rule as the dashboard
worklist (src/cases/case_urgency.py), exposed on the list, sortable
(?sort_by=urgency) and filterable (?urgency=…). The last test pins
list-overdue == worklist-overdue so the two engines (SQL here, Python in the
worklist) never diverge (single source)."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from src.dashboard.dashboard_manager import WorklistManager
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")

PAST = "2020-01-01T00:00:00Z"


@pytest_asyncio.fixture
async def me(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"], first_name="Al", last_name="M")


async def _journey_case(
    client: AsyncClient,
    ah: dict[str, str],
    *,
    agency_id: uuid.UUID,
    expat_id: uuid.UUID,
    owner_id: uuid.UUID,
    status: str,
    make_client_case: MakeClientCase,
) -> tuple[uuid.UUID, list[str]]:
    tid = (
        await client.post("/journeys", headers=ah, json={"name": f"T{uuid.uuid4().hex[:6]}"})
    ).json()["id"]
    assert (
        await client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "S0"})
    ).status_code == 201
    case = await make_client_case(
        agency_id=agency_id,
        principal_expat_user_id=expat_id,
        owner_agent_id=owner_id,
        status=status,
    )
    prog = (
        await client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    return case.id, [s["id"] for s in prog]


async def _scenario(
    client: AsyncClient,
    me: Agent,
    ah: dict[str, str],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> dict[str, uuid.UUID]:
    async def new_expat(email: str) -> uuid.UUID:
        return (await make_expat_user(email=email, first_name="C", last_name="X")).id

    common = {"agency_id": me.agency_id, "owner_id": me.id, "make_client_case": make_client_case}

    # overdue: an in_progress step past its due date.
    over, p = await _journey_case(
        client, ah, expat_id=await new_expat("o@x.com"), status="in_progress", **common
    )
    await client.patch(
        f"/cases/{over}/steps/{p[0]}", headers=ah, json={"status": "in_progress", "due_at": PAST}
    )
    # to_validate: an in_progress step (default agent validator), no deadline.
    tov, q = await _journey_case(
        client, ah, expat_id=await new_expat("v@x.com"), status="in_progress", **common
    )
    await client.patch(f"/cases/{tov}/steps/{q[0]}", headers=ah, json={"status": "in_progress"})
    # awaiting_client: AWAITING_DOCUMENTS, no overdue / to_validate step (TODO).
    awa, _ = await _journey_case(
        client, ah, expat_id=await new_expat("a@x.com"), status="awaiting_documents", **common
    )
    # neutral: in_progress case, step still TODO.
    neu, _ = await _journey_case(
        client, ah, expat_id=await new_expat("n@x.com"), status="in_progress", **common
    )
    return {"overdue": over, "to_validate": tov, "awaiting_client": awa, "neutral": neu}


def _urgency_by_id(items: list[dict]) -> dict[str, str]:
    return {i["id"]: i["urgency"] for i in items}


async def test_urgency_derivation_per_case(
    client: AsyncClient,
    me: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(me)
    ids = await _scenario(client, me, ah, make_client_case, make_expat_user)
    items = (await client.get("/cases", headers=ah, params={"page_size": 100})).json()["items"]
    urg = _urgency_by_id(items)
    assert urg[str(ids["overdue"])] == "overdue"
    assert urg[str(ids["to_validate"])] == "to_validate"
    assert urg[str(ids["awaiting_client"])] == "awaiting_client"
    assert urg[str(ids["neutral"])] == "neutral"


async def test_sort_by_urgency_across_pages(
    client: AsyncClient,
    me: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(me)
    await _scenario(client, me, ah, make_client_case, make_expat_user)
    order = ["overdue", "to_validate", "awaiting_client", "neutral"]
    seen: list[str] = []
    for page in (1, 2):
        items = (
            await client.get(
                "/cases",
                headers=ah,
                params={"sort_by": "urgency", "order": "asc", "page": page, "page_size": 2},
            )
        ).json()["items"]
        seen.extend(i["urgency"] for i in items)
    # The four scenario cases, most urgent first, split across the two pages.
    assert seen == order


async def test_filter_urgency_returns_only_matching(
    client: AsyncClient,
    me: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(me)
    ids = await _scenario(client, me, ah, make_client_case, make_expat_user)
    resp = (await client.get("/cases", headers=ah, params={"urgency": "overdue"})).json()
    got = {i["id"] for i in resp["items"]}
    assert got == {str(ids["overdue"])}
    # Combinable with status (AND): overdue AND status in_progress still matches.
    resp2 = (
        await client.get(
            "/cases", headers=ah, params={"urgency": "to_validate", "status": "in_progress"}
        )
    ).json()
    assert {i["id"] for i in resp2["items"]} == {str(ids["to_validate"])}


async def test_list_overdue_matches_worklist(
    client: AsyncClient,
    db_session,
    me: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """Single source: every case the worklist flags overdue is `overdue` on
    the list too (the list is agency-wide, a superset of my worklist)."""
    ah = agent_headers(me)
    await _scenario(client, me, ah, make_client_case, make_expat_user)
    worklist = await WorklistManager(db_session).get_worklist(me)
    overdue_case_ids = {str(i.case_id) for i in worklist.items if i.is_overdue}
    assert overdue_case_ids  # the scenario has at least one
    items = (await client.get("/cases", headers=ah, params={"page_size": 100})).json()["items"]
    urg = _urgency_by_id(items)
    for cid in overdue_case_ids:
        assert urg[cid] == "overdue"
