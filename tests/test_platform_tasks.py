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
from src.core import email, storage
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
    superadmin2: Agent,
    agency_admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    response = await client.get("/admin/operators", headers=agent_headers(superadmin))
    assert response.status_code == 200, response.text
    operators = response.json()
    # BOTH active operators, and ONLY them (the agency admin next door is out).
    assert {o["agent_id"] for o in operators} == {str(superadmin.id), str(superadmin2.id)}
    assert all(o["name"] for o in operators)
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


# --- the appointment block (Prism: scheduled_at + tz + calendar links) ----------------


async def test_schedule_validation_and_wall_clock_tz_edit(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    no_tz = await client.post(
        "/admin/tasks",
        headers=headers,
        json={"title": "x", "scheduled_at": "2026-08-01T14:00:00+00:00"},
    )
    assert no_tz.status_code == 422  # scheduled_at without timezone
    bad_tz = await client.post(
        "/admin/tasks",
        headers=headers,
        json={
            "title": "x",
            "scheduled_at": "2026-08-01T14:00:00+00:00",
            "scheduled_timezone": "Mars/Olympus",
        },
    )
    assert bad_tz.status_code == 422  # not an IANA zone
    meeting = await _create(
        client,
        headers,
        title="Call Eric",
        task_type="call",
        scheduled_at="2026-08-01T14:00:00+00:00",
        scheduled_timezone="Europe/Lisbon",
        duration_minutes=45,
        location="Zoom",
    )
    assert meeting["scheduled_timezone"] == "Europe/Lisbon"
    assert meeting["duration_minutes"] == 45 and meeting["location"] == "Zoom"

    # Prism exact: a timezone-only PATCH keeps the wall clock (15:00
    # Lisbon stays 15:00, now in New York) and moves the UTC instant.
    patched = await client.patch(
        f"/admin/tasks/{meeting['id']}",
        headers=headers,
        json={"scheduled_timezone": "America/New_York"},
    )
    assert patched.status_code == 200, patched.text
    from zoneinfo import ZoneInfo

    stored = datetime.fromisoformat(patched.json()["scheduled_at"])
    assert stored.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M") == "15:00"


async def test_calendar_link_three_formats(
    client: AsyncClient,
    superadmin: Agent,
    make_agency: MakeAgency,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(superadmin)
    agency = await make_agency(name="Expat Lisbonne")
    meeting = await _create(
        client,
        headers,
        title="Point KYB",
        agency_id=str(agency.id),
        scheduled_at="2026-08-01T14:00:00+00:00",
        scheduled_timezone="Europe/Lisbon",
        duration_minutes=45,
        location="Zoom",
    )
    response = await client.get(f"/admin/tasks/{meeting['id']}/calendar-link", headers=headers)
    assert response.status_code == 200, response.text
    link = response.json()
    assert link["title"] == "Point KYB - Expat Lisbonne"  # agency subject in the title
    # Google: floating LOCAL time (15:00 Lisbon in August) + ctz=.
    assert "dates=20260801T150000/20260801T154500" in link["google_url"]
    assert "ctz=Europe/Lisbon" in link["google_url"]  # quote() keeps the slash
    # Outlook: offset-baked ISO.
    assert "startdt=2026-08-01T15%3A00%3A00%2B01%3A00" in link["outlook_url"]
    # ICS: TZID-prefixed times, no VTIMEZONE block (Prism choice).
    assert "DTSTART;TZID=Europe/Lisbon:20260801T150000" in link["ics_content"]
    assert "DTEND;TZID=Europe/Lisbon:20260801T154500" in link["ics_content"]
    assert "SUMMARY:Point KYB - Expat Lisbonne" in link["ics_content"]
    assert "VTIMEZONE" not in link["ics_content"]


async def test_calendar_link_400_without_schedule(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    task = await _create(client, headers)
    response = await client.get(f"/admin/tasks/{task['id']}/calendar-link", headers=headers)
    assert response.status_code == 400


async def test_overdue_and_order_judge_due_at_only(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    """Prism exact: is_overdue, the overdue filter and the list order
    look at due_at ONLY — scheduled_at plays no role in either."""
    headers = agent_headers(superadmin)
    meeting = await _create(
        client,
        headers,
        title="past-meeting",
        scheduled_at=(_NOW - timedelta(days=2)).isoformat(),
        scheduled_timezone="Europe/Paris",
    )
    assert meeting["is_overdue"] is False  # past scheduled_at, no due_at: NOT overdue
    await _create(client, headers, title="due-later", due_at=(_NOW + timedelta(days=5)).isoformat())
    listed = await client.get("/admin/tasks", headers=headers)
    titles = [t["title"] for t in listed.json()["items"]]
    assert titles == ["due-later", "past-meeting"]  # due_at NULLS LAST, scheduled ignored
    overdue = await client.get("/admin/tasks?is_overdue=true", headers=headers)
    assert overdue.json()["items"] == []


# --- attachments (Prism port; limits aligned on case documents) -----------------------


def _pdf(name: str = "notes.pdf", size: int = 100) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, b"x" * size, "application/pdf")}


async def test_attachment_upload_list_download_delete(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    task = await _create(client, headers)
    uploaded = await client.post(
        f"/admin/tasks/{task['id']}/attachments", headers=headers, files=_pdf()
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["file_name"] == "notes.pdf" and body["size_bytes"] == 100
    assert body["uploaded_by_agent_id"] == str(superadmin.id)
    assert "storage_path" not in body  # internal key, never served

    listed = await client.get(f"/admin/tasks/{task['id']}/attachments", headers=headers)
    assert [a["id"] for a in listed.json()] == [body["id"]]

    download = await client.get(
        f"/admin/tasks/{task['id']}/attachments/{body['id']}/download", headers=headers
    )
    assert download.status_code == 200
    assert download.content == b"x" * 100
    assert "notes.pdf" in download.headers["content-disposition"]

    # The blob key is uuid-only under the platform prefix: the display
    # name never reaches the storage path.
    [path] = storage.mock_store.keys()
    assert path == f"platform-tasks/{task['id']}/{body['id']}"

    deleted = await client.delete(
        f"/admin/tasks/{task['id']}/attachments/{body['id']}", headers=headers
    )
    assert deleted.status_code == 204
    assert storage.mock_store == {}  # physical delete, not just the row
    assert (
        await client.get(f"/admin/tasks/{task['id']}/attachments", headers=headers)
    ).json() == []


async def test_attachment_unsupported_extension_is_415(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    task = await _create(client, headers)
    response = await client.post(
        f"/admin/tasks/{task['id']}/attachments",
        headers=headers,
        files={"file": ("script.exe", b"MZ", "application/octet-stream")},
    )
    assert response.status_code == 415, response.text
    assert storage.mock_store == {}  # nothing reached the bucket


async def test_attachment_oversize_is_413(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    task = await _create(client, headers)
    too_big = 10 * 1024 * 1024 + 1  # the case-documents cap, aligned
    response = await client.post(
        f"/admin/tasks/{task['id']}/attachments",
        headers=headers,
        files=_pdf(size=too_big),
    )
    assert response.status_code == 413, response.text
    assert storage.mock_store == {}


async def test_task_delete_removes_blobs_physically(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders, db_session: AsyncSession
) -> None:
    headers = agent_headers(superadmin)
    task = await _create(client, headers)
    for name in ("a.pdf", "b.png"):
        upload = await client.post(
            f"/admin/tasks/{task['id']}/attachments", headers=headers, files=_pdf(name=name)
        )
        assert upload.status_code == 201
    assert len(storage.mock_store) == 2
    assert (await client.delete(f"/admin/tasks/{task['id']}", headers=headers)).status_code == 204
    assert storage.mock_store == {}  # blobs gone, not only the CASCADE rows
    from shared.models.platform_task_attachment import PlatformTaskAttachment

    rows = (await db_session.execute(select(PlatformTaskAttachment))).scalars().all()
    assert rows == []


async def test_attachments_403_for_agency_admin(
    client: AsyncClient, superadmin: Agent, agency_admin: Agent, agent_headers: AuthHeaders
) -> None:
    task = await _create(client, agent_headers(superadmin))
    denied_headers = agent_headers(agency_admin)
    upload = await client.post(
        f"/admin/tasks/{task['id']}/attachments", headers=denied_headers, files=_pdf()
    )
    assert upload.status_code == 403
    listed = await client.get(f"/admin/tasks/{task['id']}/attachments", headers=denied_headers)
    assert listed.status_code == 403


async def test_attachment_of_another_task_is_404(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    task_a = await _create(client, headers, title="A")
    task_b = await _create(client, headers, title="B")
    upload = await client.post(
        f"/admin/tasks/{task_a['id']}/attachments", headers=headers, files=_pdf()
    )
    attachment_id = upload.json()["id"]
    crossed = await client.get(
        f"/admin/tasks/{task_b['id']}/attachments/{attachment_id}/download", headers=headers
    )
    assert crossed.status_code == 404  # scoped by task_id, no traversal
    crossed_delete = await client.delete(
        f"/admin/tasks/{task_b['id']}/attachments/{attachment_id}", headers=headers
    )
    assert crossed_delete.status_code == 404


async def test_attachments_live_on_done_task(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    """Prism has NO lock on done tasks (verified in the reference): a
    provided content stays visible for life, and upload/delete remain
    allowed — a platform task is not a client dossier."""
    headers = agent_headers(superadmin)
    task = await _create(client, headers, status="done")
    upload = await client.post(
        f"/admin/tasks/{task['id']}/attachments", headers=headers, files=_pdf()
    )
    assert upload.status_code == 201  # upload allowed on done
    attachment_id = upload.json()["id"]
    download = await client.get(
        f"/admin/tasks/{task['id']}/attachments/{attachment_id}/download", headers=headers
    )
    assert download.status_code == 200  # visible/downloadable on done
    deleted = await client.delete(
        f"/admin/tasks/{task['id']}/attachments/{attachment_id}", headers=headers
    )
    assert deleted.status_code == 204  # delete allowed on done
