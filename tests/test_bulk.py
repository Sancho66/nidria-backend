"""Bulk actions + soft delete battery. The crown jewel is
`test_soft_deleted_case_invisible_everywhere` — it asserts a deleted
case vanishes from the listing, a saved view, the detail (404), the
expat space, the approval queue AND the scheduler (no mail dispatched,
no auto follow-up created)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from src.core import email
from src.reminders.reminders_jobs import create_auto_reminders, dispatch_due_reminders
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep
from tests.plugins.reminder_plugin import MakeReminder

_NOW = datetime.now(UTC)


@pytest.fixture
def bulk_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def manager(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """case_manager: holds case.edit AND case.delete."""
    return await make_agent(role=system_roles["case_manager"])


@pytest_asyncio.fixture
async def member(manager: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """Same agency, member role: case.edit but NOT case.delete."""
    return await make_agent(agency_id=manager.agency_id, role=system_roles["member"])


def _run_dispatch(session_local: sessionmaker[Session]) -> dict:
    with session_local() as db:
        return dispatch_due_reminders(db, log=lambda _: None)


def _run_auto(session_local: sessionmaker[Session]) -> dict:
    with session_local() as db:
        return create_auto_reminders(db, log=lambda _: None)


# --- bulk-action edit ----------------------------------------------------------------


async def test_bulk_set_status(
    bulk_client: AsyncClient,
    manager: Agent,
    db_session: AsyncSession,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    a = await make_client_case(agency_id=manager.agency_id, status="prospect")
    b = await make_client_case(agency_id=manager.agency_id, status="prospect")
    response = await bulk_client.post(
        "/cases/bulk-action",
        headers=agent_headers(manager),
        json={"action": "set_status", "case_ids": [str(a.id), str(b.id)], "status": "in_progress"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "set_status"
    assert body["examined"] == 2 and body["affected"] == 2
    assert set(body["affected_ids"]) == {str(a.id), str(b.id)}

    await db_session.refresh(a)
    await db_session.refresh(b)
    assert a.status == "in_progress" and b.status == "in_progress"
    # ActivityLog per case.
    logs = (
        (
            await db_session.execute(
                select(ActivityLog).where(ActivityLog.action_type == "case.status_changed")
            )
        )
        .scalars()
        .all()
    )
    assert {log.case_id for log in logs} == {a.id, b.id}


async def test_bulk_set_owner_validates_and_unassigns(
    bulk_client: AsyncClient,
    manager: Agent,
    member: Agent,
    make_agent: MakeAgent,
    db_session: AsyncSession,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager)
    case = await make_client_case(agency_id=manager.agency_id, owner_agent_id=manager.id)

    # Assign to the colleague…
    assign = await bulk_client.post(
        "/cases/bulk-action",
        headers=headers,
        json={"action": "set_owner", "case_ids": [str(case.id)], "owner_agent_id": str(member.id)},
    )
    assert assign.status_code == 200 and assign.json()["affected"] == 1
    await db_session.refresh(case)
    assert case.owner_agent_id == member.id

    # …then unassign (null).
    unassign = await bulk_client.post(
        "/cases/bulk-action",
        headers=headers,
        json={"action": "set_owner", "case_ids": [str(case.id)], "owner_agent_id": None},
    )
    assert unassign.status_code == 200 and unassign.json()["affected"] == 1
    await db_session.refresh(case)
    assert case.owner_agent_id is None

    # A foreign agent as owner → 422 (membership gate, like the unit PATCH).
    foreign = await make_agent()
    bad = await bulk_client.post(
        "/cases/bulk-action",
        headers=headers,
        json={
            "action": "set_owner",
            "case_ids": [str(case.id)],
            "owner_agent_id": str(foreign.id),
        },
    )
    assert bad.status_code == 422


async def test_bulk_add_and_remove_tags_idempotent(
    bulk_client: AsyncClient,
    manager: Agent,
    db_session: AsyncSession,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager)
    case = await make_client_case(agency_id=manager.agency_id, tags=["vip"])

    add = await bulk_client.post(
        "/cases/bulk-action",
        headers=headers,
        json={"action": "add_tags", "case_ids": [str(case.id)], "tags": ["urgent", "vip"]},
    )
    assert add.status_code == 200 and add.json()["affected"] == 1
    await db_session.refresh(case)
    assert set(case.tags) == {"vip", "urgent"}  # "vip" not duplicated

    # Re-adding the same tags is a no-op (idempotent).
    again = await bulk_client.post(
        "/cases/bulk-action",
        headers=headers,
        json={"action": "add_tags", "case_ids": [str(case.id)], "tags": ["urgent"]},
    )
    assert again.json()["affected"] == 0

    remove = await bulk_client.post(
        "/cases/bulk-action",
        headers=headers,
        json={"action": "remove_tags", "case_ids": [str(case.id)], "tags": ["vip", "ghost"]},
    )
    assert remove.json()["affected"] == 1
    await db_session.refresh(case)
    assert case.tags == ["urgent"]


# --- gate & scoping ------------------------------------------------------------------


async def test_bulk_delete_gate(
    bulk_client: AsyncClient,
    manager: Agent,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=manager.agency_id)
    payload = {"case_ids": [str(case.id)]}

    # member has case.edit but NOT case.delete → 403 by the engine.
    denied = await bulk_client.post(
        "/cases/bulk-delete", headers=agent_headers(member), json=payload
    )
    assert denied.status_code == 403
    # …but member CAN run an edit-action.
    ok = await bulk_client.post(
        "/cases/bulk-action",
        headers=agent_headers(member),
        json={"action": "set_status", "case_ids": [str(case.id)], "status": "in_progress"},
    )
    assert ok.status_code == 200

    # case_manager holds case.delete → 200.
    allowed = await bulk_client.post(
        "/cases/bulk-delete", headers=agent_headers(manager), json=payload
    )
    assert allowed.status_code == 200 and allowed.json()["affected"] == 1


async def test_bulk_cross_agency_ids_silently_ignored(
    bulk_client: AsyncClient,
    manager: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    mine = await make_client_case(agency_id=manager.agency_id, status="prospect")
    foreign = await make_client_case(status="prospect")  # other agency
    response = await bulk_client.post(
        "/cases/bulk-action",
        headers=agent_headers(manager),
        json={
            "action": "set_status",
            "case_ids": [str(mine.id), str(foreign.id)],
            "status": "validated",
        },
    )
    assert response.status_code == 200
    body = response.json()
    # examined counts both submitted ids; only the own-agency one is affected.
    assert body["examined"] == 2 and body["affected"] == 1
    assert body["affected_ids"] == [str(mine.id)]


async def test_bulk_delete_idempotent(
    bulk_client: AsyncClient,
    manager: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=manager.agency_id)
    headers = agent_headers(manager)
    first = await bulk_client.post(
        "/cases/bulk-delete", headers=headers, json={"case_ids": [str(case.id)]}
    )
    assert first.json()["affected"] == 1
    # Re-deleting: the row no longer resolves → no-op.
    second = await bulk_client.post(
        "/cases/bulk-delete", headers=headers, json={"case_ids": [str(case.id)]}
    )
    assert second.json()["affected"] == 0


async def test_bulk_cap_422(
    bulk_client: AsyncClient, manager: Agent, agent_headers: AuthHeaders
) -> None:
    too_many = [str(uuid.uuid4()) for _ in range(501)]
    response = await bulk_client.post(
        "/cases/bulk-action",
        headers=agent_headers(manager),
        json={"action": "set_status", "case_ids": too_many, "status": "prospect"},
    )
    assert response.status_code == 422


# --- THE soft-delete invisibility test ------------------------------------------------


async def test_soft_deleted_case_invisible_everywhere(
    bulk_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    manager: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_reminder: MakeReminder,
    expat_headers: AuthHeaders,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager)
    expat = await make_expat_user(activated=True)
    case = await make_client_case(
        agency_id=manager.agency_id, principal_expat_user_id=expat.id, status="in_progress"
    )

    # A saved view that would surface the case.
    await bulk_client.post(
        "/views",
        headers=headers,
        json={"name": "Tous", "filters": {}, "is_shared": True},
    )

    # A stalled step eligible for an auto follow-up (updated_at way past
    # the J+20 threshold).
    template = await make_journey_template(agency_id=manager.agency_id)
    step = await make_template_step(template=template)
    progress = CaseStepProgress(case_id=case.id, template_step_id=step.id, status="in_progress")
    db_session.add(progress)
    await db_session.commit()
    await db_session.execute(
        CaseStepProgress.__table__.update()
        .where(CaseStepProgress.id == progress.id)
        .values(updated_at=_NOW - timedelta(days=40))
    )
    # A DUE, APPROVED mail reminder that the scheduler would send.
    await make_reminder(
        case=case,
        status="approved",
        channel="mail",
        recipient_type="expat",
        scheduled_at=_NOW - timedelta(hours=1),
    )
    await db_session.commit()

    # Sanity: before deletion it IS visible in the listing.
    pre = await bulk_client.get("/cases", headers=headers)
    assert str(case.id) in {c["id"] for c in pre.json()["items"]}

    # --- bulk-delete it ---
    deleted = await bulk_client.post(
        "/cases/bulk-delete", headers=headers, json={"case_ids": [str(case.id)]}
    )
    assert deleted.status_code == 200 and deleted.json()["affected"] == 1

    # 1. Gone from GET /cases.
    listing = await bulk_client.get("/cases", headers=headers)
    assert str(case.id) not in {c["id"] for c in listing.json()["items"]}

    # 2. Gone from a saved-view-filtered listing (filters={} = all).
    import json as _json
    from urllib.parse import quote

    view_listing = await bulk_client.get(
        f"/cases?filters={quote(_json.dumps({}))}", headers=headers
    )
    assert str(case.id) not in {c["id"] for c in view_listing.json()["items"]}

    # 3. Detail → 404.
    assert (await bulk_client.get(f"/cases/{case.id}", headers=headers)).status_code == 404

    # 4. Expat space: list empty + detail 404.
    expat_list = await bulk_client.get("/expat/cases", headers=expat_headers(expat))
    assert str(case.id) not in {c["id"] for c in expat_list.json()}
    expat_detail = await bulk_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))
    assert expat_detail.status_code == 404

    # 5. Approval queue: the TO_APPROVE/APPROVED reminder is gone.
    rem_list = await bulk_client.get("/reminders", headers=headers)
    assert all(r["case_id"] != str(case.id) for r in rem_list.json()["items"])

    # 6. THE SCHEDULER — no mail dispatched, no auto follow-up created.
    email.outbox.clear()
    dispatch_stats = _run_dispatch(sync_session_local)
    assert dispatch_stats == {"due": 0, "sent": 0}
    assert email.outbox == []
    auto_stats = _run_auto(sync_session_local)
    assert auto_stats["created"] == 0
    # No reminder was auto-created on the deleted case.
    auto_rows = (
        (await db_session.execute(select(Reminder).where(Reminder.case_id == case.id)))
        .scalars()
        .all()
    )
    assert len(auto_rows) == 1  # only the one we seeded; none added by the job


async def test_soft_deleted_case_excluded_from_dashboard(
    bulk_client: AsyncClient,
    manager: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager)
    keep = await make_client_case(agency_id=manager.agency_id, status="prospect")
    drop = await make_client_case(agency_id=manager.agency_id, status="prospect")

    before = (await bulk_client.get("/dashboard", headers=headers)).json()
    assert before["total_cases"] == 2

    await bulk_client.post("/cases/bulk-delete", headers=headers, json={"case_ids": [str(drop.id)]})

    after = (await bulk_client.get("/dashboard", headers=headers)).json()
    assert after["total_cases"] == 1
    assert after["by_status"].get("prospect") == 1
    _ = keep
