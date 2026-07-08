"""GET /dashboard/worklist - the unified "to handle" queue (contract
validated 2026-07-08, arbitrages: owner fills the agency-in-general
validator hole; documents scoped owner OR step responsible; reminders
scoped owner).

Covers: the four item types with their action links; the dedup rule (a
late step awaiting my validation = ONE step_to_validate item flagged
overdue); the contract sort (overdue first, largest delay first, then
oldest waiting); STRICT per-agent and per-tenant scoping proven at the
data level; agency deposits and reviewed documents never queue; and the
no-N+1 guarantee (constant query count, measured)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from src.dashboard.dashboard_manager import WorklistManager
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")

PAST = "2020-01-01T00:00:00Z"


@pytest_asyncio.fixture
async def me(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"], first_name="Alex", last_name="M")


@pytest_asyncio.fixture
async def colleague(me: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(agency_id=me.agency_id, role=system_roles["admin"], email="col@x.com")


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


async def _journey_case(
    client: AsyncClient,
    ah: dict[str, str],
    *,
    agency_id: uuid.UUID,
    expat_id: uuid.UUID,
    owner_id: uuid.UUID,
    steps: int,
    make_client_case: MakeClientCase,
) -> tuple[ClientCase, list[str]]:
    tid = (await client.post("/journeys", headers=ah, json={"name": f"T{steps}"})).json()["id"]
    for i in range(steps):
        added = await client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": f"S{i}"})
        assert added.status_code == 201
    case = await make_client_case(
        agency_id=agency_id,
        principal_expat_user_id=expat_id,
        owner_agent_id=owner_id,
        status="in_progress",
    )
    progress = (
        await client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    return case, [s["id"] for s in progress]


async def _scenario(
    client: AsyncClient,
    db_session: AsyncSession,
    me: Agent,
    colleague: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    ah: dict[str, str],
) -> dict:
    """The full queue for `me`:
    - MY case (2 steps): p0 in_progress + due PAST, default validator
      (agent, NULL) -> step_to_validate OVERDUE via the owner arbitrage,
      dedup proves it never doubles as step_overdue; p1 in_progress,
      validator designated me -> step_to_validate.
    - COLLEAGUE's case (1 step): responsible me, due PAST, validator
      NULL+owner=colleague -> for ME a pure step_overdue.
    - 3 documents on my case: expat-unreviewed (queued), expat-reviewed
      and agent-uploaded (never queued).
    - 2 reminders on my case: to_approve past (late) + future (waiting).
    """
    mine, p = await _journey_case(
        client,
        ah,
        agency_id=me.agency_id,
        expat_id=expat.id,
        owner_id=me.id,
        steps=2,
        make_client_case=make_client_case,
    )
    await client.patch(
        f"/cases/{mine.id}/steps/{p[0]}", headers=ah, json={"status": "in_progress", "due_at": PAST}
    )
    await client.put(
        f"/cases/{mine.id}/steps/{p[1]}/validator",
        headers=ah,
        json={"validated_by_type": "agent", "validated_by_agent_id": str(me.id)},
    )
    await client.patch(f"/cases/{mine.id}/steps/{p[1]}", headers=ah, json={"status": "in_progress"})

    theirs, q = await _journey_case(
        client,
        ah,
        agency_id=me.agency_id,
        expat_id=expat.id,
        owner_id=colleague.id,
        steps=1,
        make_client_case=make_client_case,
    )
    await client.put(
        f"/cases/{theirs.id}/steps/{q[0]}/responsible",
        headers=ah,
        json={"responsible_type": "agent", "responsible_agent_id": str(me.id)},
    )
    await client.patch(
        f"/cases/{theirs.id}/steps/{q[0]}",
        headers=ah,
        json={"status": "in_progress", "due_at": PAST},
    )

    def _doc(filename: str, uploaded_by: str, status: str | None) -> Document:
        return Document(
            case_id=mine.id,
            filename=filename,
            storage_path=f"cases/{mine.id}/{filename}",
            uploaded_by_type=uploaded_by,
            uploaded_by_id=expat.id if uploaded_by == "expat" else me.id,
            validation_status=status,
        )

    db_session.add(_doc("passeport.pdf", "expat", None))  # queued
    db_session.add(_doc("relu.pdf", "expat", "ok"))  # reviewed: never queued
    db_session.add(_doc("mandat-agence.pdf", "agent", None))  # agency deposit: never queued

    def _reminder(scheduled_at: datetime, body: str) -> Reminder:
        return Reminder(
            case_id=mine.id,
            channel="mail",
            scheduled_at=scheduled_at,
            status="to_approve",
            recipient_type="expat",
            message_body=body,
        )

    db_session.add(_reminder(datetime(2020, 1, 1, tzinfo=UTC), "Relance passeport"))
    db_session.add(_reminder(datetime.now(UTC) + timedelta(days=3), "Relance future"))
    await db_session.commit()
    return {"mine": mine, "theirs": theirs, "p": p, "q": q}


async def test_worklist_types_dedup_and_sort(
    client: AsyncClient,
    db_session: AsyncSession,
    me: Agent,
    colleague: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ctx = await _scenario(
        client, db_session, me, colleague, expat, make_client_case, agent_headers(me)
    )
    response = await client.get("/dashboard/worklist", headers=agent_headers(me))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["counts"] == {
        "step_to_validate": 2,
        "step_overdue": 1,
        "document_to_review": 1,
        "reminder_to_approve": 2,
        "total": 6,
    }
    by_type: dict[str, list[dict]] = {}
    for item in body["items"]:
        by_type.setdefault(item["type"], []).append(item)

    # Dedup: p0 (late AND awaiting my validation via the owner
    # arbitrage) is ONE step_to_validate item flagged overdue.
    validate_ids = {i["progress_id"] for i in by_type["step_to_validate"]}
    assert validate_ids == {ctx["p"][0], ctx["p"][1]}
    late_validate = next(i for i in by_type["step_to_validate"] if i["progress_id"] == ctx["p"][0])
    assert late_validate["is_overdue"] is True and late_validate["days_late"] > 0
    assert late_validate["client_name"] == "Marie Curie"

    # The colleague-owned case only reaches me as responsible: overdue.
    overdue = by_type["step_overdue"][0]
    assert overdue["progress_id"] == ctx["q"][0]
    assert overdue["is_overdue"] is True and overdue["days_late"] > 0

    # Document: only the unreviewed CLIENT upload queues, with its link.
    doc = by_type["document_to_review"][0]
    assert doc["title"] == "passeport.pdf" and doc["document_id"]
    assert doc["is_overdue"] is False

    # Reminders: the past one is late, the future one just waits.
    reminders = {i["title"]: i for i in by_type["reminder_to_approve"]}
    assert reminders["Relance passeport"]["is_overdue"] is True
    assert reminders["Relance future"]["is_overdue"] is False
    assert reminders["Relance future"]["reminder_id"]

    # Contract sort: every overdue item strictly before every waiting one.
    flags = [item["is_overdue"] for item in body["items"]]
    assert flags == sorted(flags, reverse=True)
    # Largest delay first among the overdue block.
    late = [item["days_late"] for item in body["items"] if item["is_overdue"]]
    assert late == sorted(late, reverse=True)


async def test_worklist_scoping_same_agency_and_cross_tenant(
    client: AsyncClient,
    db_session: AsyncSession,
    me: Agent,
    colleague: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    ctx = await _scenario(
        client, db_session, me, colleague, expat, make_client_case, agent_headers(me)
    )
    # The COLLEAGUE only sees what lands on THEIR desk: the case they own
    # has one in_progress step with the agency-in-general validator ->
    # one step_to_validate; none of my documents/reminders leak.
    theirs = (await client.get("/dashboard/worklist", headers=agent_headers(colleague))).json()
    assert theirs["counts"] == {"step_to_validate": 1, "total": 1}
    assert theirs["items"][0]["progress_id"] == ctx["q"][0]

    # Another tenant sees NOTHING.
    outsider = await make_agent(role=system_roles["admin"], email="out@other.com")
    empty = (await client.get("/dashboard/worklist", headers=agent_headers(outsider))).json()
    assert empty == {"items": [], "counts": {"total": 0}}

    # No permission (empty custom role) -> 403, deny by default.
    powerless = await make_agent(agency_id=me.agency_id)
    denied = await client.get("/dashboard/worklist", headers=agent_headers(powerless))
    assert denied.status_code == 403


async def test_worklist_query_count_is_constant(
    client: AsyncClient,
    db_session: AsyncSession,
    me: Agent,
    colleague: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """No N+1: the manager runs a FIXED number of batched queries however
    many items the queue holds (6 items in this scenario)."""
    await _scenario(client, db_session, me, colleague, expat, make_client_case, agent_headers(me))
    engine = db_session.get_bind()
    counter = {"n": 0}

    def _count(*_args: object, **_kwargs: object) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        result = await WorklistManager(db_session).get_worklist(me)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert result.counts["total"] == 6
    assert counter["n"] <= 7, f"worklist ran {counter['n']} queries"
