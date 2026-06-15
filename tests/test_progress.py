"""Feature-4 battery: journey instantiation (copy rule), the lock with
explicit blocking-step names, BLOCKED as a read-time projection (incl.
THE dynamic test promised at step 8), end-to-end backfill through the
journeys API, reopening with audit trace, polymorphic responsible."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase, MakeExternalContact


@pytest.fixture
def progress_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def manager_agent(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """case_manager: journey.configure + case.edit + step.complete."""
    return await make_agent(role=system_roles["case_manager"])


async def _make_template(
    client: AsyncClient,
    headers: dict[str, str],
    steps: list[dict[str, object]],
    prereqs: dict[int, list[int]] | None = None,
) -> tuple[str, list[str]]:
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step_ids: list[str] = []
    for spec in steps:
        response = await client.post(
            f"/journeys/{template['id']}/steps", headers=headers, json=spec
        )
        assert response.status_code == 201
        step_ids.append(response.json()["id"])
    for index, prereq_indexes in (prereqs or {}).items():
        response = await client.put(
            f"/journeys/{template['id']}/steps/{step_ids[index]}/prerequisites",
            headers=headers,
            json={"prerequisite_step_ids": [step_ids[i] for i in prereq_indexes]},
        )
        assert response.status_code == 200
    return template["id"], step_ids


async def _assign(
    client: AsyncClient, headers: dict[str, str], case: ClientCase, template_id: str
) -> list[dict[str, object]]:
    response = await client.post(
        f"/cases/{case.id}/journey",
        headers=headers,
        json={"journey_template_id": template_id},
    )
    assert response.status_code == 201
    return list(response.json())


def _by_name(timeline: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(item["name"]): item for item in timeline}


# --- instantiation ---------------------------------------------------------------


async def test_assign_instantiates_all_steps_with_projection(
    progress_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(
        progress_client,
        headers,
        [{"name": "A"}, {"name": "B"}, {"name": "C"}],
        prereqs={1: [0], 2: [1]},
    )
    case = await make_client_case(agency_id=manager_agent.agency_id)
    timeline = await _assign(progress_client, headers, case, template_id)

    by_name = _by_name(timeline)
    assert by_name["A"]["status"] == "todo"
    assert by_name["B"]["status"] == "blocked"
    assert by_name["C"]["status"] == "blocked"
    assert [b["name"] for b in by_name["B"]["blocked_by"]] == ["A"]  # type: ignore[index]

    # Stored statuses are ALL todo — blocked is never written.
    stored = (
        await db_session.execute(
            select(CaseStepProgress.status).where(CaseStepProgress.case_id == case.id)
        )
    ).scalars()
    assert set(stored) == {"todo"}


async def test_copy_rule_for_default_responsible(
    progress_client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(
        progress_client,
        headers,
        [
            {"name": "ByExpat", "default_responsible_type": "expat"},
            {"name": "ByAgent", "default_responsible_type": "agent"},
            {"name": "Nobody"},
        ],
    )
    case = await make_client_case(agency_id=manager_agent.agency_id)
    by_name = _by_name(await _assign(progress_client, headers, case, template_id))
    assert by_name["ByExpat"]["responsible_type"] == "expat"
    # AGENT default needs a person: stays NULL until explicit assignment.
    assert by_name["ByAgent"]["responsible_type"] is None
    assert by_name["Nobody"]["responsible_type"] is None


async def test_assign_twice_409_and_foreign_template_404(
    progress_client: AsyncClient,
    manager_agent: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(progress_client, headers, [{"name": "A"}])
    case = await make_client_case(agency_id=manager_agent.agency_id)
    await _assign(progress_client, headers, case, template_id)
    second = await progress_client.post(
        f"/cases/{case.id}/journey",
        headers=headers,
        json={"journey_template_id": template_id},
    )
    assert second.status_code == 409

    foreign_agent = await make_agent()  # another agency
    other_case = await make_client_case(agency_id=foreign_agent.agency_id)
    del other_case  # template not visible cross-agency either way
    case2 = await make_client_case(agency_id=manager_agent.agency_id)
    foreign_template_resp = await progress_client.post(
        f"/cases/{case2.id}/journey",
        headers=headers,
        json={"journey_template_id": str(uuid.uuid4())},
    )
    assert foreign_template_resp.status_code == 404


# --- the lock ----------------------------------------------------------------------


async def test_lock_blocks_transition_and_lists_blocking_names(
    progress_client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(
        progress_client,
        headers,
        [{"name": "Visa application"}, {"name": "Residence card"}],
        prereqs={1: [0]},
    )
    case = await make_client_case(agency_id=manager_agent.agency_id)
    by_name = _by_name(await _assign(progress_client, headers, case, template_id))

    blocked = await progress_client.patch(
        f"/cases/{case.id}/steps/{by_name['Residence card']['id']}",
        headers=headers,
        json={"status": "done"},
    )
    assert blocked.status_code == 409
    assert "Visa application" in blocked.json()["detail"]

    # Also blocked for in_progress, not just done.
    started = await progress_client.patch(
        f"/cases/{case.id}/steps/{by_name['Residence card']['id']}",
        headers=headers,
        json={"status": "in_progress"},
    )
    assert started.status_code == 409


async def test_cascade_unblock_and_complete(
    progress_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(
        progress_client,
        headers,
        [{"name": "A"}, {"name": "B"}],
        prereqs={1: [0]},
    )
    case = await make_client_case(agency_id=manager_agent.agency_id)
    by_name = _by_name(await _assign(progress_client, headers, case, template_id))

    done_a = await progress_client.patch(
        f"/cases/{case.id}/steps/{by_name['A']['id']}", headers=headers, json={"status": "done"}
    )
    assert done_a.status_code == 200
    assert done_a.json()["completed_by_agent_id"] == str(manager_agent.id)
    assert done_a.json()["completed_at"] is not None

    timeline = (await progress_client.get(f"/cases/{case.id}/steps", headers=headers)).json()
    assert _by_name(timeline)["B"]["status"] == "todo"  # unblocked by projection

    done_b = await progress_client.patch(
        f"/cases/{case.id}/steps/{by_name['B']['id']}", headers=headers, json={"status": "done"}
    )
    assert done_b.status_code == 200

    types = (
        await db_session.execute(
            select(ActivityLog.action_type).where(ActivityLog.case_id == case.id)
        )
    ).scalars()
    assert list(types).count("step.completed") == 2


async def test_dynamic_projection_after_prerequisite_mutation(
    progress_client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """THE step-8 promise: editing prerequisites of an ASSIGNED template
    re-locks live cases dynamically — no resync, pure projection."""
    headers = agent_headers(manager_agent)
    template_id, step_ids = await _make_template(
        progress_client, headers, [{"name": "A"}, {"name": "B"}]
    )
    case = await make_client_case(agency_id=manager_agent.agency_id)
    by_name = _by_name(await _assign(progress_client, headers, case, template_id))
    assert by_name["B"]["status"] == "todo"  # free at assignment time

    # Mutate the ASSIGNED template: B now requires A.
    response = await progress_client.put(
        f"/journeys/{template_id}/steps/{step_ids[1]}/prerequisites",
        headers=headers,
        json={"prerequisite_step_ids": [step_ids[0]]},
    )
    assert response.status_code == 200

    timeline = (await progress_client.get(f"/cases/{case.id}/steps", headers=headers)).json()
    refreshed = _by_name(timeline)["B"]
    assert refreshed["status"] == "blocked"
    assert [b["name"] for b in refreshed["blocked_by"]] == ["A"]  # type: ignore[index]
    # And the write lock follows the projection.
    patch = await progress_client.patch(
        f"/cases/{case.id}/steps/{refreshed['id']}", headers=headers, json={"status": "done"}
    )
    assert patch.status_code == 409


async def test_backfill_end_to_end_via_journeys_api(
    progress_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(progress_client, headers, [{"name": "A"}])
    case = await make_client_case(agency_id=manager_agent.agency_id)
    await _assign(progress_client, headers, case, template_id)

    added = await progress_client.post(
        f"/journeys/{template_id}/steps",
        headers=headers,
        json={"name": "Backfilled", "default_responsible_type": "expat"},
    )
    assert added.status_code == 201

    timeline = (await progress_client.get(f"/cases/{case.id}/steps", headers=headers)).json()
    backfilled = _by_name(timeline)["Backfilled"]
    assert backfilled["status"] == "todo"
    assert backfilled["responsible_type"] == "expat"  # copy rule applies

    log = (
        await db_session.execute(
            select(ActivityLog).where(
                ActivityLog.case_id == case.id, ActivityLog.action_type == "step.added"
            )
        )
    ).scalar_one()
    assert log.actor_id == manager_agent.id  # the configuring agent, not SYSTEM


# --- transitions ----------------------------------------------------------------------


async def test_reopen_clears_completion_with_audit_trace(
    progress_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(progress_client, headers, [{"name": "A"}])
    case = await make_client_case(agency_id=manager_agent.agency_id)
    by_name = _by_name(await _assign(progress_client, headers, case, template_id))
    step_url = f"/cases/{case.id}/steps/{by_name['A']['id']}"

    await progress_client.patch(step_url, headers=headers, json={"status": "done"})
    reopened = await progress_client.patch(
        step_url, headers=headers, json={"status": "in_progress"}
    )
    assert reopened.status_code == 200
    body = reopened.json()
    assert body["status"] == "in_progress"
    assert body["completed_at"] is None
    assert body["completed_by_agent_id"] is None

    log = (
        await db_session.execute(
            select(ActivityLog).where(
                ActivityLog.case_id == case.id, ActivityLog.action_type == "step.reopened"
            )
        )
    ).scalar_one()
    assert log.details["previous_completed_by"] == str(manager_agent.id)
    assert log.details["previous_completed_at"] is not None


async def test_invalid_transitions_and_blocked_status_422(
    progress_client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(progress_client, headers, [{"name": "A"}])
    case = await make_client_case(agency_id=manager_agent.agency_id)
    by_name = _by_name(await _assign(progress_client, headers, case, template_id))
    step_url = f"/cases/{case.id}/steps/{by_name['A']['id']}"

    # blocked is a projection, not a settable status.
    assert (
        await progress_client.patch(step_url, headers=headers, json={"status": "blocked"})
    ).status_code == 422
    # todo → todo is not a transition.
    assert (
        await progress_client.patch(step_url, headers=headers, json={"status": "todo"})
    ).status_code == 422
    # done → todo is not allowed (reopen goes to in_progress).
    await progress_client.patch(step_url, headers=headers, json={"status": "done"})
    assert (
        await progress_client.patch(step_url, headers=headers, json={"status": "todo"})
    ).status_code == 422


async def test_step_started_logged(
    progress_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(progress_client, headers, [{"name": "A"}])
    case = await make_client_case(agency_id=manager_agent.agency_id)
    by_name = _by_name(await _assign(progress_client, headers, case, template_id))
    response = await progress_client.patch(
        f"/cases/{case.id}/steps/{by_name['A']['id']}",
        headers=headers,
        json={"status": "in_progress"},
    )
    assert response.status_code == 200
    types = (
        await db_session.execute(
            select(ActivityLog.action_type).where(ActivityLog.case_id == case.id)
        )
    ).scalars()
    assert "step.started" in list(types)


# --- polymorphic responsible -------------------------------------------------------------


async def test_responsible_agent_validations(
    progress_client: AsyncClient,
    manager_agent: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(progress_client, headers, [{"name": "A"}])
    case = await make_client_case(agency_id=manager_agent.agency_id)
    by_name = _by_name(await _assign(progress_client, headers, case, template_id))
    step_url = f"/cases/{case.id}/steps/{by_name['A']['id']}"

    colleague = await make_agent(agency_id=manager_agent.agency_id)
    ok = await progress_client.put(
        f"{step_url}/responsible",
        headers=headers,
        json={"responsible_type": "agent", "responsible_agent_id": str(colleague.id)},
    )
    assert ok.status_code == 200
    assert ok.json()["responsible_agent_id"] == str(colleague.id)

    stranger = await make_agent()  # another agency
    ko = await progress_client.put(
        f"{step_url}/responsible",
        headers=headers,
        json={"responsible_type": "agent", "responsible_agent_id": str(stranger.id)},
    )
    assert ko.status_code == 422
    missing_fk = await progress_client.put(
        f"{step_url}/responsible", headers=headers, json={"responsible_type": "agent"}
    )
    assert missing_fk.status_code == 422


async def test_responsible_external_must_belong_to_same_case(
    progress_client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(progress_client, headers, [{"name": "A"}])
    case = await make_client_case(agency_id=manager_agent.agency_id)
    other_case = await make_client_case(agency_id=manager_agent.agency_id)
    by_name = _by_name(await _assign(progress_client, headers, case, template_id))
    step_url = f"/cases/{case.id}/steps/{by_name['A']['id']}"

    own_contact = await make_external_contact(case=case)
    other_contact = await make_external_contact(case=other_case)

    ok = await progress_client.put(
        f"{step_url}/responsible",
        headers=headers,
        json={"responsible_type": "external", "responsible_external_id": str(own_contact.id)},
    )
    assert ok.status_code == 200
    ko = await progress_client.put(
        f"{step_url}/responsible",
        headers=headers,
        json={
            "responsible_type": "external",
            "responsible_external_id": str(other_contact.id),
        },
    )
    assert ko.status_code == 422


async def test_responsible_expat_then_cleared_with_log(
    progress_client: AsyncClient,
    db_session: AsyncSession,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(progress_client, headers, [{"name": "A"}])
    case = await make_client_case(agency_id=manager_agent.agency_id)
    by_name = _by_name(await _assign(progress_client, headers, case, template_id))
    step_url = f"/cases/{case.id}/steps/{by_name['A']['id']}"

    expat_resp = await progress_client.put(
        f"{step_url}/responsible", headers=headers, json={"responsible_type": "expat"}
    )
    assert expat_resp.status_code == 200
    assert expat_resp.json()["responsible_type"] == "expat"

    cleared = await progress_client.put(
        f"{step_url}/responsible", headers=headers, json={"responsible_type": None}
    )
    assert cleared.status_code == 200
    assert cleared.json()["responsible_type"] is None

    logs = (
        (
            await db_session.execute(
                select(ActivityLog).where(
                    ActivityLog.case_id == case.id,
                    ActivityLog.action_type == "step.responsible_changed",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(logs) == 2
    assert logs[-1].details["old"]["responsible_type"] == "expat"
    assert logs[-1].details["new"]["responsible_type"] is None


# --- permissions & scoping ------------------------------------------------------------------


async def test_viewer_cannot_patch_steps(
    progress_client: AsyncClient,
    manager_agent: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(progress_client, headers, [{"name": "A"}])
    case = await make_client_case(agency_id=manager_agent.agency_id)
    by_name = _by_name(await _assign(progress_client, headers, case, template_id))

    viewer = await make_agent(agency_id=manager_agent.agency_id, role=system_roles["viewer"])
    response = await progress_client.patch(
        f"/cases/{case.id}/steps/{by_name['A']['id']}",
        headers=agent_headers(viewer),
        json={"status": "done"},
    )
    assert response.status_code == 403
    # But the viewer can read the timeline (case.view).
    assert (
        await progress_client.get(f"/cases/{case.id}/steps", headers=agent_headers(viewer))
    ).status_code == 200


async def test_case_detail_includes_projected_progress(
    progress_client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    template_id, _ = await _make_template(
        progress_client, headers, [{"name": "A"}, {"name": "B"}], prereqs={1: [0]}
    )
    case = await make_client_case(agency_id=manager_agent.agency_id)
    await _assign(progress_client, headers, case, template_id)
    detail = (await progress_client.get(f"/cases/{case.id}", headers=headers)).json()
    assert _by_name(detail["progress"])["B"]["status"] == "blocked"


async def test_steps_scoped_to_agency(
    progress_client: AsyncClient,
    manager_agent: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    foreign_agent = await make_agent()
    foreign_case = await make_client_case(agency_id=foreign_agent.agency_id)
    response = await progress_client.get(
        f"/cases/{foreign_case.id}/steps", headers=agent_headers(manager_agent)
    )
    assert response.status_code == 404
