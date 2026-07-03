"""Point 8 (Eric) — propagation of template-ADDED requirements to existing
dossiers. A requirement added to an assigned template used to exist only as
a definition: steps already activated (materialized at activation, frozen)
never gained the concrete instance, so the client saw nothing — even after
a reopen.

Covers: (a) immediate backfill on IN_PROGRESS steps — instance created, the
client sees it, the activation requirement_request mail is reused (one per
affected dossier); case_field declarations stay template-live and appear
immediately; (b) DONE steps untouched on add (never made incomplete),
catch-up at reopen with already-provided answers intact; (c) TODO steps
untouched (everything materializes at activation); (d) idempotence — re-add
/ double reopen never duplicates, and the composition freeze stands (a
later-added person gains nothing on frozen definitions); (e) archived cases
and other agencies never touched; (f) gating — an active step that was
ready to validate becomes incomplete again."""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core import email
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeCasePerson, MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

REQUEST_MAIL_SUBJECT = "informations sont attendues"  # requirement_request, fr


@pytest.fixture
def rp_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


# --- helpers -------------------------------------------------------------------------


async def _template_with_step(client: AsyncClient, headers: dict[str, str]) -> tuple[str, str]:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    step = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "Collecte", "completion_mode": "agency_validation"},
        )
    ).json()
    return tid, step["id"]


async def _add_req(
    client: AsyncClient, headers: dict[str, str], tid: str, sid: str, **body: object
) -> dict:
    if body.get("kind") in ("base_field", "custom_field"):
        await client.post(
            f"/journeys/{tid}/fields",
            headers=headers,
            json={"kind": body["kind"], "reference": body["reference"]},
        )
    r = await client.post(f"/journeys/{tid}/steps/{sid}/requirements", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _assign(client: AsyncClient, headers: dict[str, str], case_id: str, tid: str) -> str:
    steps = (
        await client.post(
            f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    return steps[0]["id"]


async def _set_status(
    client: AsyncClient, headers: dict[str, str], case_id: str, pid: str, status: str
) -> dict:
    r = await client.patch(
        f"/cases/{case_id}/steps/{pid}", headers=headers, json={"status": status}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _assign_start(
    client: AsyncClient, headers: dict[str, str], case_id: str, tid: str
) -> str:
    pid = await _assign(client, headers, case_id, tid)
    await _set_status(client, headers, case_id, pid, "in_progress")
    return pid


async def _rows(db_session: AsyncSession, pid: str) -> list[CaseStepRequirement]:
    stmt = (
        select(CaseStepRequirement)
        .where(CaseStepRequirement.case_step_progress_id == uuid.UUID(pid))
        .order_by(CaseStepRequirement.created_at)
    )
    return list((await db_session.execute(stmt)).scalars())


async def _timeline_entry(
    client: AsyncClient, headers: dict[str, str], case_id: str, pid: str
) -> dict:
    detail = await client.get(f"/cases/{case_id}", headers=headers)
    return next(s for s in detail.json()["progress"] if s["id"] == pid)


def _request_mails() -> list[email.OutboxEmail]:
    return [m for m in email.outbox if REQUEST_MAIL_SUBJECT in m.subject]


# --- (a) immediate propagation to IN_PROGRESS steps ----------------------------------


async def test_add_requirement_propagates_to_active_step_and_client_sees_it(
    rp_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(rp_client, headers)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    pid = await _assign_start(rp_client, headers, str(case.id), tid)
    email.outbox.clear()

    await _add_req(
        rp_client, headers, tid, sid, kind="document", reference="Preuve", scope="principal"
    )

    # The concrete instance exists on the ACTIVE step (backfill, no reopen).
    rows = await _rows(db_session, pid)
    assert [(r.kind, r.reference, r.status) for r in rows] == [("document", "Preuve", "pending")]

    # The client sees it in their space.
    detail = await rp_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))
    reqs = detail.json()["timeline"][0]["requirements"]
    assert [(r["kind"], r["reference"], r["status"]) for r in reqs] == [
        ("document", "Preuve", "pending")
    ]

    # The activation requirement_request mechanism is reused: ONE mail,
    # to the dossier's principal.
    assert [m.to for m in _request_mails()] == [expat.email]

    # The propagation is journaled on the dossier.
    activity = await rp_client.get(f"/cases/{case.id}/activity", headers=headers)
    assert any(e["action_type"] == "step.requirement_added" for e in activity.json()["items"])


async def test_add_case_requirement_is_live_on_active_step(
    rp_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """case_field declarations have no instance table — they are evaluated
    live against the template, so an addition reaches existing dossiers by
    construction (no backfill involved)."""
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(rp_client, headers)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    await _assign_start(rp_client, headers, str(case.id), tid)

    # Strict membership: declare the case field in the Informations tab first.
    declared = await rp_client.post(
        f"/journeys/{tid}/case-fields", headers=headers, json={"case_field": "dest_city"}
    )
    assert declared.status_code == 201, declared.text
    r = await rp_client.post(
        f"/journeys/{tid}/steps/{sid}/case-requirements",
        headers=headers,
        json={"case_field": "dest_city"},
    )
    assert r.status_code == 201, r.text

    detail = await rp_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))
    reqs = detail.json()["timeline"][0]["requirements"]
    assert [(r["kind"], r["reference"], r["target"]) for r in reqs] == [
        ("case_field", "dest_city", "case")
    ]


# --- (b) DONE untouched, reopen catches up, answers intact ---------------------------


async def test_done_step_untouched_then_reopen_syncs_with_answers_intact(
    rp_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(rp_client, headers)
    await _add_req(
        rp_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="principal",
    )
    case = await make_client_case(agency_id=admin.agency_id)
    pid = await _assign_start(rp_client, headers, str(case.id), tid)

    # Provide the existing answer, then close the step.
    entry = await _timeline_entry(rp_client, headers, str(case.id), pid)
    person_id = entry["requirements"][0]["person_id"]
    await rp_client.patch(
        f"/cases/{case.id}/persons/{person_id}", headers=headers, json={"passport_number": "X-42"}
    )
    await _set_status(rp_client, headers, str(case.id), pid, "done")

    await _add_req(
        rp_client, headers, tid, sid, kind="document", reference="Preuve", scope="principal"
    )

    # A validated step is never made incomplete: no instance appeared.
    assert [(r.kind, r.reference) for r in await _rows(db_session, pid)] == [
        ("base_field", "passport_number")
    ]

    # Reopen → the missing instance materializes; the provided answer is intact.
    await _set_status(rp_client, headers, str(case.id), pid, "in_progress")
    entry = await _timeline_entry(rp_client, headers, str(case.id), pid)
    by_ref = {r["reference"]: r for r in entry["requirements"]}
    assert set(by_ref) == {"passport_number", "Preuve"}
    assert by_ref["passport_number"]["status"] == "provided"
    assert by_ref["passport_number"]["value"] == "X-42"
    assert by_ref["Preuve"]["status"] == "pending"


# --- (c) TODO steps: nothing at add, everything at activation ------------------------


async def test_todo_step_untouched_until_activation(
    rp_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(rp_client, headers)
    case = await make_client_case(agency_id=admin.agency_id)
    pid = await _assign(rp_client, headers, str(case.id), tid)  # stays TODO

    await _add_req(
        rp_client, headers, tid, sid, kind="document", reference="Preuve", scope="principal"
    )
    assert await _rows(db_session, pid) == []  # nothing propagated to a TODO step

    await _set_status(rp_client, headers, str(case.id), pid, "in_progress")
    rows = await _rows(db_session, pid)
    assert [(r.kind, r.reference) for r in rows] == [("document", "Preuve")]


# --- (d) idempotence + composition freeze ---------------------------------------------


async def test_propagation_and_resync_never_duplicate(
    rp_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_case_person: MakeCasePerson,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(rp_client, headers)
    await _add_req(
        rp_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="principal",
    )
    case = await make_client_case(agency_id=admin.agency_id)
    await make_case_person(case=case, full_name="Spouse")
    pid = await _assign_start(rp_client, headers, str(case.id), tid)
    assert len(await _rows(db_session, pid)) == 1  # passport × principal

    # Propagation: each_person document → principal + spouse = 2 new rows.
    await _add_req(
        rp_client, headers, tid, sid, kind="document", reference="Preuve", scope="each_person"
    )
    assert len(await _rows(db_session, pid)) == 3

    # Composition freeze: a later-added person gains nothing on frozen
    # definitions — and double reopen re-syncs are no-ops.
    await make_case_person(case=case, full_name="LateComer")
    for _ in range(2):
        await _set_status(rp_client, headers, str(case.id), pid, "done")
        await _set_status(rp_client, headers, str(case.id), pid, "in_progress")
    rows = await _rows(db_session, pid)
    assert len(rows) == 3
    assert len({(r.person_id, r.kind, r.reference) for r in rows}) == 3  # no dup key


# --- (e) archived cases and other agencies are never touched -------------------------


async def test_archived_and_foreign_cases_never_touched(
    rp_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(rp_client, headers)
    case = await make_client_case(agency_id=admin.agency_id)
    pid = await _assign_start(rp_client, headers, str(case.id), tid)

    # An archived case of the SAME template, step left in_progress.
    archived = await make_client_case(agency_id=admin.agency_id)
    archived_pid = await _assign_start(rp_client, headers, str(archived.id), tid)
    archived.deleted_at = datetime.now(UTC)
    await db_session.commit()

    # Another agency with its own template + active case.
    admin_b = await make_agent(role=system_roles["admin"])
    headers_b = agent_headers(admin_b)
    tid_b, _sid_b = await _template_with_step(rp_client, headers_b)
    case_b = await make_client_case(agency_id=admin_b.agency_id)
    pid_b = await _assign_start(rp_client, headers_b, str(case_b.id), tid_b)

    email.outbox.clear()
    await _add_req(
        rp_client, headers, tid, sid, kind="document", reference="Preuve", scope="principal"
    )

    assert len(await _rows(db_session, pid)) == 1  # the live case, and only it
    assert await _rows(db_session, archived_pid) == []
    assert await _rows(db_session, pid_b) == []
    assert len(_request_mails()) == 1  # one affected dossier → one mail


# --- (f) gating: ready-to-validate becomes incomplete again --------------------------


async def test_ready_to_validate_step_becomes_incomplete_on_new_requirement(
    rp_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_with_step(rp_client, headers)
    await _add_req(
        rp_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="principal",
    )
    case = await make_client_case(agency_id=admin.agency_id)
    pid = await _assign_start(rp_client, headers, str(case.id), tid)

    entry = await _timeline_entry(rp_client, headers, str(case.id), pid)
    person_id = entry["requirements"][0]["person_id"]
    await rp_client.patch(
        f"/cases/{case.id}/persons/{person_id}", headers=headers, json={"passport_number": "X-42"}
    )
    entry = await _timeline_entry(rp_client, headers, str(case.id), pid)
    assert entry["all_requirements_met"] is True  # ready to validate

    await _add_req(
        rp_client, headers, tid, sid, kind="document", reference="Preuve", scope="principal"
    )
    entry = await _timeline_entry(rp_client, headers, str(case.id), pid)
    assert entry["all_requirements_met"] is False  # incomplete again (existing gating)
    assert entry["status"] == "in_progress"  # the step itself is untouched
