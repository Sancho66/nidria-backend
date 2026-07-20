"""Superadmin platform tasks (Prism port v1, GO 2026-07-20): CRUD, the
computed Prism order, filters, the completion stamps, the summary badge,
the platform gate (agency admin 403 — dedicated platform.task_manage,
not agency.create), agency SET NULL."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.platform_task import PlatformTask
from shared.models.rbac import Role
from src.core import email
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

_NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"], email="root@platform.io")


@pytest_asyncio.fixture
async def agency_admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _create(
    client: AsyncClient, headers: dict[str, str], **overrides: object
) -> dict[str, object]:
    payload: dict[str, object] = {"title": "Relancer le KYB", **overrides}
    response = await client.post("/admin/tasks", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- the platform gate ----------------------------------------------------------------


async def test_agency_admin_and_member_are_403(
    client: AsyncClient,
    agency_admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """platform.task_manage is a PLATFORM permission: the agency admin
    (who holds everything agency-scoped) is still excluded — the
    structural barrier, not a matrix accident."""
    member = await make_agent(role=system_roles["member"])
    for actor in (agency_admin, member):
        headers = agent_headers(actor)
        assert (await client.get("/admin/tasks", headers=headers)).status_code == 403
        create = await client.post("/admin/tasks", headers=headers, json={"title": "x"})
        assert create.status_code == 403


# --- create ---------------------------------------------------------------------------


async def test_create_defaults_and_self_assignment(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    body = await _create(client, agent_headers(superadmin))
    assert body["status"] == "todo" and body["priority"] == "medium"
    assert body["assigned_to_agent_id"] == str(superadmin.id)  # defaults to the actor
    assert body["assigned_to_name"] and body["created_by_agent_id"] == str(superadmin.id)
    assert body["agency_id"] is None and body["agency_name"] is None
    assert body["completed_at"] is None and body["is_overdue"] is False


async def test_create_with_agency_subject(
    client: AsyncClient,
    superadmin: Agent,
    make_agency: MakeAgency,
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency(name="Expat Lisbonne")
    body = await _create(client, agent_headers(superadmin), agency_id=str(agency.id))
    assert body["agency_id"] == str(agency.id)
    assert body["agency_name"] == "Expat Lisbonne"


async def test_assignee_must_be_superadmin(
    client: AsyncClient,
    superadmin: Agent,
    agency_admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    response = await client.post(
        "/admin/tasks",
        headers=agent_headers(superadmin),
        json={"title": "x", "assigned_to_agent_id": str(agency_admin.id)},
    )
    assert response.status_code == 422
    assert "superadmin" in response.json()["detail"]


async def test_unknown_status_and_priority_rejected(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    bad_status = await client.post(
        "/admin/tasks", headers=headers, json={"title": "x", "status": "blocked"}
    )
    assert bad_status.status_code == 422
    bad_priority = await client.post(
        "/admin/tasks", headers=headers, json={"title": "x", "priority": "asap"}
    )
    assert bad_priority.status_code == 422
    unknown_key = await client.post(
        "/admin/tasks", headers=headers, json={"title": "x", "sticky": True}
    )
    assert unknown_key.status_code == 422  # extra=forbid, no lying 200


async def test_created_directly_done_is_stamped(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    body = await _create(client, agent_headers(superadmin), status="done")
    assert body["completed_at"] is not None
    assert body["completed_by_agent_id"] == str(superadmin.id)


# --- list: the Prism order and the filters --------------------------------------------


async def test_list_prism_order_done_last_priority_then_due(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    day = timedelta(days=1)
    await _create(client, headers, title="done-urgent", status="done", priority="urgent")
    await _create(
        client, headers, title="low-early", priority="low", due_at=(_NOW + day).isoformat()
    )
    await _create(
        client,
        headers,
        title="urgent-late",
        priority="urgent",
        due_at=(_NOW + 9 * day).isoformat(),
    )
    await _create(
        client,
        headers,
        title="urgent-early",
        priority="urgent",
        due_at=(_NOW + 2 * day).isoformat(),
    )
    response = await client.get("/admin/tasks", headers=headers)
    assert response.status_code == 200
    titles = [t["title"] for t in response.json()["items"]]
    assert titles == ["urgent-early", "urgent-late", "low-early", "done-urgent"]


async def test_list_filters(
    client: AsyncClient,
    superadmin: Agent,
    make_agency: MakeAgency,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(superadmin)
    agency = await make_agency()
    await _create(client, headers, title="open", priority="high")
    await _create(client, headers, title="closed", status="done")
    await _create(client, headers, title="late", due_at=(_NOW - timedelta(days=3)).isoformat())
    await _create(client, headers, title="on-agency", agency_id=str(agency.id))

    async def titles(query: str) -> set[str]:
        response = await client.get(f"/admin/tasks?{query}", headers=headers)
        assert response.status_code == 200, response.text
        return {t["title"] for t in response.json()["items"]}

    assert await titles("include_done=false") == {"open", "late", "on-agency"}
    assert await titles("status=done") == {"closed"}
    assert await titles("priority=high") == {"open"}
    assert await titles("is_overdue=true") == {"late"}
    assert await titles(f"agency_id={agency.id}") == {"on-agency"}
    assert await titles("assigned_to=me") == {"open", "closed", "late", "on-agency"}
    assert await titles(f"assigned_to={uuid.uuid4()}") == set()
    bad = await client.get("/admin/tasks?assigned_to=nope", headers=headers)
    assert bad.status_code == 422


# --- lifecycle ------------------------------------------------------------------------


async def test_patch_edits_and_status_stamps(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    task = await _create(client, headers)
    patched = await client.patch(
        f"/admin/tasks/{task['id']}",
        headers=headers,
        json={"title": "Vérifier le KYB", "priority": "urgent", "status": "done"},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["title"] == "Vérifier le KYB" and body["priority"] == "urgent"
    assert body["status"] == "done" and body["completed_at"] is not None
    assert body["completed_by_agent_id"] == str(superadmin.id)

    reopened = await client.patch(
        f"/admin/tasks/{task['id']}", headers=headers, json={"status": "in_progress"}
    )
    assert reopened.json()["completed_at"] is None  # leaving done clears the stamp
    assert reopened.json()["completed_by_agent_id"] is None


async def test_complete_and_reopen_endpoints(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    task = await _create(client, headers)
    done = await client.post(f"/admin/tasks/{task['id']}/complete", headers=headers)
    assert done.status_code == 200
    assert done.json()["status"] == "done" and done.json()["completed_at"] is not None
    back = await client.post(f"/admin/tasks/{task['id']}/reopen", headers=headers)
    assert back.status_code == 200
    assert back.json()["status"] == "todo" and back.json()["completed_at"] is None


async def test_delete_204_and_404(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    task = await _create(client, headers)
    assert (await client.delete(f"/admin/tasks/{task['id']}", headers=headers)).status_code == 204
    assert (await client.delete(f"/admin/tasks/{task['id']}", headers=headers)).status_code == 404


# --- summary --------------------------------------------------------------------------


async def test_summary_badge_counts(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    await _create(client, headers, title="pending")
    await _create(client, headers, title="late", due_at=(_NOW - timedelta(days=2)).isoformat())
    today_end = _NOW.replace(hour=23, minute=59, second=59, microsecond=0)
    await _create(client, headers, title="today", due_at=today_end.isoformat())
    await _create(client, headers, title="shipped", status="done")
    response = await client.get("/admin/tasks/summary", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4 and body["pending"] == 3
    assert body["overdue"] == 1 and body["due_today"] == 1
    assert body["completed_this_week"] == 1


# --- agency SET NULL ------------------------------------------------------------------


async def test_agency_deletion_orphans_the_task_not_deletes_it(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    make_agency: MakeAgency,
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency()
    task = await _create(client, agent_headers(superadmin), agency_id=str(agency.id))
    task_id = uuid.UUID(str(task["id"]))
    await db_session.delete(agency)
    await db_session.commit()
    row = (
        await db_session.execute(select(PlatformTask).where(PlatformTask.id == task_id))
    ).scalar_one()
    assert row.agency_id is None  # SET NULL: the work item survives its subject


# --- micro-lot front gaps (2026-07-20): completer name + operators selector -----------


async def test_completed_by_name_served(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    task = await _create(client, headers)
    done = await client.post(f"/admin/tasks/{task['id']}/complete", headers=headers)
    body = done.json()
    expected = f"{superadmin.first_name} {superadmin.last_name}".strip()
    assert body["completed_by_name"] == expected  # the real name, not an id coincidence
    listed = await client.get("/admin/tasks", headers=headers)
    assert listed.json()["items"][0]["completed_by_name"] == expected


async def test_operators_list_and_gate(
    client: AsyncClient,
    superadmin: Agent,
    agency_admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    response = await client.get("/admin/operators", headers=agent_headers(superadmin))
    assert response.status_code == 200, response.text
    operators = response.json()
    assert {o["agent_id"] for o in operators} == {str(superadmin.id)}  # superadmins only
    assert operators[0]["name"]
    denied = await client.get("/admin/operators", headers=agent_headers(agency_admin))
    assert denied.status_code == 403


# --- task_type (Prism: task/call/meeting/follow_up) -----------------------------------


async def test_task_type_at_contract(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    plain = await _create(client, headers)
    assert plain["task_type"] == "task"  # the default
    call = await _create(client, headers, title="Appeler Eric", task_type="call")
    assert call["task_type"] == "call"
    patched = await client.patch(
        f"/admin/tasks/{call['id']}", headers=headers, json={"task_type": "meeting"}
    )
    assert patched.status_code == 200 and patched.json()["task_type"] == "meeting"
    bad = await client.post(
        "/admin/tasks", headers=headers, json={"title": "x", "task_type": "email"}
    )
    assert bad.status_code == 422
    bad_patch = await client.patch(
        f"/admin/tasks/{call['id']}", headers=headers, json={"task_type": "email"}
    )
    assert bad_patch.status_code == 422


async def test_task_type_filter(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    await _create(client, headers, title="plain")
    await _create(client, headers, title="the-call", task_type="call")
    await _create(client, headers, title="the-followup", task_type="follow_up")
    response = await client.get("/admin/tasks?task_type=call", headers=headers)
    assert {t["title"] for t in response.json()["items"]} == {"the-call"}
    everything = await client.get("/admin/tasks", headers=headers)
    assert everything.json()["total"] == 3  # no filter, no exclusion


# --- emails (the Prism model, exact) --------------------------------------------------


@pytest_asyncio.fixture
async def superadmin2(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"], email="root2@platform.io")


@pytest_asyncio.fixture
async def superadmin3(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"], email="root3@platform.io")


async def test_create_assigned_to_other_sends_one_mail(
    client: AsyncClient, superadmin: Agent, superadmin2: Agent, agent_headers: AuthHeaders
) -> None:
    email.outbox.clear()
    await _create(
        client,
        agent_headers(superadmin),
        title="Relancer le KYB",
        assigned_to_agent_id=str(superadmin2.id),
    )
    assert [m.to for m in email.outbox] == [superadmin2.email]
    assert "Relancer le KYB" in email.outbox[0].subject  # task_assigned template


async def test_self_assignment_sends_nothing(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    email.outbox.clear()
    await _create(client, agent_headers(superadmin))
    assert email.outbox == []  # the actor is never their own recipient


async def test_complete_by_assignee_mails_the_creator(
    client: AsyncClient, superadmin: Agent, superadmin2: Agent, agent_headers: AuthHeaders
) -> None:
    task = await _create(
        client,
        agent_headers(superadmin),
        title="Vérifier le KYB",
        assigned_to_agent_id=str(superadmin2.id),
    )
    email.outbox.clear()
    done = await client.post(
        f"/admin/tasks/{task['id']}/complete", headers=agent_headers(superadmin2)
    )
    assert done.status_code == 200
    assert [m.to for m in email.outbox] == [superadmin.email]  # the creator, once
    assert "mise à jour" in email.outbox[0].subject  # task_status_changed template


async def test_creator_completing_own_task_gets_no_mail(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    task = await _create(client, headers)
    email.outbox.clear()
    await client.post(f"/admin/tasks/{task['id']}/complete", headers=headers)
    assert email.outbox == []  # actor == creator == assignee: zero mail


async def test_reassignment_mails_new_assignee_and_creator_deduplicated(
    client: AsyncClient,
    superadmin: Agent,
    superadmin2: Agent,
    superadmin3: Agent,
    agent_headers: AuthHeaders,
) -> None:
    # Creator A, assignee A; actor B reassigns to C -> 2 mails {C, A}.
    task = await _create(client, agent_headers(superadmin))
    email.outbox.clear()
    patched = await client.patch(
        f"/admin/tasks/{task['id']}",
        headers=agent_headers(superadmin2),
        json={"assigned_to_agent_id": str(superadmin3.id)},
    )
    assert patched.status_code == 200
    assert sorted(m.to for m in email.outbox) == sorted([superadmin.email, superadmin3.email])
    # Creator A (assigned to C), actor B reassigns to A: the creator IS
    # the new assignee -> ONE deduplicated mail.
    other = await _create(
        client, agent_headers(superadmin), assigned_to_agent_id=str(superadmin3.id)
    )
    email.outbox.clear()
    await client.patch(
        f"/admin/tasks/{other['id']}",
        headers=agent_headers(superadmin2),
        json={"assigned_to_agent_id": str(superadmin.id)},
    )
    assert [m.to for m in email.outbox] == [superadmin.email]  # deduplicated
