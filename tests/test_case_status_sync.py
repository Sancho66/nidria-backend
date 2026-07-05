"""Statut/étape coherence: current-step display + journey-driven status.

Covers: (a) list + detail expose current_step_name/_position (resolved,
"i/n"), NULL without a journey and when all validated, and the views
catalog gains the additive current_step column; (b) prospect + a step
worked → in_progress with the SYSTEM/auto trail; (c) all steps
validated → validated, reopening → in_progress; (d) a manually posed
status is NOT touched outside step transitions, but the next step event
applies the rules again; (e) a case without a journey is never
automated."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.rbac import Role
from shared.models.usage import UsageEvent
from src.views.views_schema import CASE_COLUMNS
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _make_journey(client: AsyncClient, headers: dict[str, str], steps: list[str]) -> str:
    journey = await client.post("/journeys", headers=headers, json={"name": "Parcours T"})
    assert journey.status_code == 201, journey.text
    for name in steps:
        added = await client.post(
            f"/journeys/{journey.json()['id']}/steps", headers=headers, json={"name": name}
        )
        assert added.status_code == 201, added.text
    return journey.json()["id"]


async def _make_case(
    client: AsyncClient, headers: dict[str, str], email: str, journey_id: str | None
) -> tuple[str, list[str]]:
    """(case_id, ordered progress ids)."""
    payload = {
        "first_name": "Test",
        "last_name": "Client",
        "email": email,
        **({"journey_template_id": journey_id} if journey_id else {}),
    }
    created = await client.post("/cases", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    case_id = created.json()["id"]
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    ordered = sorted(detail["progress"], key=lambda s: s["position"])
    return case_id, [s["id"] for s in ordered]


async def _patch_step(
    client: AsyncClient, headers: dict[str, str], case_id: str, pid: str, status: str
) -> None:
    patched = await client.patch(
        f"/cases/{case_id}/steps/{pid}", headers=headers, json={"status": status}
    )
    assert patched.status_code == 200, patched.text


async def _status(client: AsyncClient, headers: dict[str, str], case_id: str) -> str:
    return (await client.get(f"/cases/{case_id}", headers=headers)).json()["status"]


# --- (a) current step exposed in list + detail ------------------------------------------------


async def test_list_and_detail_expose_current_step(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    journey_id = await _make_journey(client, headers, ["Kickoff", "Dépôt", "Décision"])
    with_journey, pids = await _make_case(client, headers, "a@example.com", journey_id)
    without_journey, _ = await _make_case(client, headers, "b@example.com", None)

    # Step 1 validated → the current step is #2 of 3.
    await _patch_step(client, headers, with_journey, pids[0], "in_progress")
    await _patch_step(client, headers, with_journey, pids[0], "done")

    items = {i["id"]: i for i in (await client.get("/cases", headers=headers)).json()["items"]}
    assert items[with_journey]["current_step_name"] == "Dépôt"
    assert items[with_journey]["current_step_position"] == "2/3"
    assert items[without_journey]["current_step_name"] is None
    assert items[without_journey]["current_step_position"] is None

    detail = (await client.get(f"/cases/{with_journey}", headers=headers)).json()
    assert detail["current_step_name"] == "Dépôt"
    assert detail["current_step_position"] == "2/3"

    # Everything validated → NULLs again.
    for pid in pids[1:]:
        await _patch_step(client, headers, with_journey, pid, "in_progress")
        await _patch_step(client, headers, with_journey, pid, "done")
    items = {i["id"]: i for i in (await client.get("/cases", headers=headers)).json()["items"]}
    assert items[with_journey]["current_step_name"] is None
    assert items[with_journey]["current_step_position"] is None

    # The column catalog gained the additive entry.
    assert any(c.key == "current_step" for c in CASE_COLUMNS)


# --- (b) prospect + worked step → in_progress, SYSTEM/auto trail ------------------------------


async def test_prospect_moves_to_in_progress_on_first_step_activity(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    journey_id = await _make_journey(client, headers, ["Kickoff", "Dépôt"])
    case_id, pids = await _make_case(client, headers, "c@example.com", journey_id)
    assert await _status(client, headers, case_id) == "prospect"

    await _patch_step(client, headers, case_id, pids[0], "in_progress")
    assert await _status(client, headers, case_id) == "in_progress"

    log = (
        await db_session.execute(
            select(ActivityLog).where(
                ActivityLog.case_id == uuid.UUID(case_id),
                ActivityLog.action_type == "case.status_changed",
            )
        )
    ).scalar_one()
    assert log.actor_type == "system"
    assert log.details == {"old": "prospect", "new": "in_progress", "auto": True}
    event = (
        await db_session.execute(
            select(UsageEvent).where(
                UsageEvent.case_id == uuid.UUID(case_id),
                UsageEvent.event_type == "case.status_changed",
            )
        )
    ).scalar_one()
    assert event.actor_type == "system"
    assert event.details["auto"] is True


# --- (c) all validated → validated; reopening → in_progress -----------------------------------


async def test_all_done_then_reopen(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    journey_id = await _make_journey(client, headers, ["Kickoff", "Dépôt"])
    case_id, pids = await _make_case(client, headers, "d@example.com", journey_id)
    for pid in pids:
        await _patch_step(client, headers, case_id, pid, "in_progress")
        await _patch_step(client, headers, case_id, pid, "done")
    assert await _status(client, headers, case_id) == "validated"

    # Reopening a step of a finished dossier pulls it back to work.
    await _patch_step(client, headers, case_id, pids[1], "in_progress")
    assert await _status(client, headers, case_id) == "in_progress"


# --- (d) manual status holds between step events ----------------------------------------------


async def test_manual_status_holds_until_next_step_event(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    journey_id = await _make_journey(client, headers, ["Kickoff", "Dépôt"])
    case_id, pids = await _make_case(client, headers, "e@example.com", journey_id)
    await _patch_step(client, headers, case_id, pids[0], "in_progress")
    await _patch_step(client, headers, case_id, pids[0], "done")
    assert await _status(client, headers, case_id) == "in_progress"

    # Manual override wins between events…
    manual = await client.patch(f"/cases/{case_id}", headers=headers, json={"status": "submitted"})
    assert manual.status_code == 200
    # …a NON-step write (address) does not re-trigger the automaton…
    touched = await client.patch(f"/cases/{case_id}", headers=headers, json={"origin_city": "Lyon"})
    assert touched.status_code == 200
    assert await _status(client, headers, case_id) == "submitted"

    # …but the next step event applies the rules again (all done → validated).
    await _patch_step(client, headers, case_id, pids[1], "in_progress")
    assert await _status(client, headers, case_id) == "submitted"  # not all done: untouched
    await _patch_step(client, headers, case_id, pids[1], "done")
    assert await _status(client, headers, case_id) == "validated"


# --- (e) no journey → never automated ---------------------------------------------------------


async def test_no_journey_status_never_touched(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    case_id, _ = await _make_case(client, headers, "f@example.com", None)
    assert await _status(client, headers, case_id) == "prospect"
    touched = await client.patch(f"/cases/{case_id}", headers=headers, json={"origin_city": "Lyon"})
    assert touched.status_code == 200
    assert await _status(client, headers, case_id) == "prospect"
    manual = await client.patch(
        f"/cases/{case_id}", headers=headers, json={"status": "in_progress"}
    )
    assert manual.status_code == 200
    assert await _status(client, headers, case_id) == "in_progress"
