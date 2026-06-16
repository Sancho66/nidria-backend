"""FEATURE 1 battery — the expat portal. The central test is the EXACT
field contract: the exclusion design (no notes, no raw journal, no
tags/source, no staffing, zero internal UUID in the timeline) is locked
by asserting the exact key sets — any future field leak breaks here."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase, MakeExternalContact
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.reminder_plugin import MakeReminder

SUMMARY_KEYS = {
    "id",
    "agency",
    "origin_country",
    "dest_country",
    "status",
    "steps_done",
    "steps_total",
    "created_at",
    "updated_at",
}
DETAIL_KEYS = SUMMARY_KEYS | {"referent", "timeline", "custom_field_definitions"}
STEP_KEYS = {
    "progress_id",
    "name",
    "position",
    "status",
    "estimated_days",
    "completed_at",
    "blocked_by",
    "responsible",
    # NEW WAVE 2: the concrete requirements the client can fill.
    "requirements",
    # NEW WAVE: lets the client phrase the right close message.
    "completion_mode",
    # VAGUE 5: comment thread badge.
    "comment_count",
    # Days-remaining counter (firm deadline or estimated-derived).
    "counter",
}
REQUIREMENT_KEYS = {
    "id",
    "kind",
    "reference",
    "scope",
    "status",
    "person_label",
    "value",
    "document_id",
    "target",  # vague C
}


@pytest.fixture
def portal_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def manager_agent(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["case_manager"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com")


async def _case_with_journey(
    portal_client: AsyncClient,
    agent: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    headers: dict[str, str],
) -> tuple[ClientCase, list[dict[str, object]]]:
    """Template A→B (B requires A), assigned; A started."""
    template = (await portal_client.post("/journeys", headers=headers, json={"name": "T"})).json()
    steps = []
    for name, estimated in [("Visa", 15), ("Residence card", None)]:
        response = await portal_client.post(
            f"/journeys/{template['id']}/steps",
            headers=headers,
            json={"name": name, "estimated_days": estimated},
        )
        steps.append(response.json())
    await portal_client.put(
        f"/journeys/{template['id']}/steps/{steps[1]['id']}/prerequisites",
        headers=headers,
        json={"prerequisite_step_ids": [steps[0]["id"]]},
    )
    case = await make_client_case(
        agency_id=agent.agency_id,
        principal_expat_user_id=expat.id,
        owner_agent_id=agent.id,
    )
    timeline = (
        await portal_client.post(
            f"/cases/{case.id}/journey",
            headers=headers,
            json={"journey_template_id": template["id"]},
        )
    ).json()
    return case, timeline


# --- the exact field contract -------------------------------------------------------


async def test_exact_field_contract(
    portal_client: AsyncClient,
    manager_agent: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    case, _ = await _case_with_journey(
        portal_client, manager_agent, expat, make_client_case, headers
    )
    # Internal-only material that must NOT leak:
    await portal_client.post(
        f"/cases/{case.id}/notes", headers=headers, json={"body": "internal note"}
    )
    await portal_client.patch(
        f"/cases/{case.id}", headers=headers, json={"tags": ["vip"], "source": "referral"}
    )

    listing = await portal_client.get("/expat/cases", headers=expat_headers(expat))
    assert listing.status_code == 200
    [summary] = listing.json()
    assert set(summary.keys()) == SUMMARY_KEYS
    assert set(summary["agency"].keys()) == {"name"}

    detail = (
        await portal_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))
    ).json()
    assert set(detail.keys()) == DETAIL_KEYS
    for step in detail["timeline"]:
        assert set(step.keys()) == STEP_KEYS  # zero internal UUID
        assert set(step["responsible"].keys()) == {"type", "name"}
    assert set(detail["referent"].keys()) == {"first_name", "last_name", "email"}


# --- list ---------------------------------------------------------------------------


async def test_multi_agency_expat_sees_all_their_cases(
    portal_client: AsyncClient,
    expat: ExpatUser,
    make_agency: MakeAgency,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    expat_headers: AuthHeaders,
) -> None:
    """The step-4 choice (no agency_id on expat_user) proven: one human,
    cases at two agencies, one portal."""
    agency_a = await make_agency(name="Reside Paraguay")
    agency_b = await make_agency(name="Domiciliation Bulgarie")
    await make_client_case(agency_id=agency_a.id, principal_expat_user_id=expat.id)
    await make_client_case(agency_id=agency_b.id, principal_expat_user_id=expat.id)
    stranger = await make_expat_user()
    await make_client_case(agency_id=agency_a.id, principal_expat_user_id=stranger.id)

    response = await portal_client.get("/expat/cases", headers=expat_headers(expat))
    assert response.status_code == 200
    agencies = {item["agency"]["name"] for item in response.json()}
    assert agencies == {"Reside Paraguay", "Domiciliation Bulgarie"}


async def test_list_step_counts(
    portal_client: AsyncClient,
    manager_agent: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    case, timeline = await _case_with_journey(
        portal_client, manager_agent, expat, make_client_case, headers
    )
    await portal_client.patch(
        f"/cases/{case.id}/steps/{timeline[0]['id']}", headers=headers, json={"status": "done"}
    )
    [summary] = (await portal_client.get("/expat/cases", headers=expat_headers(expat))).json()
    assert (summary["steps_done"], summary["steps_total"]) == (1, 2)


# --- detail -------------------------------------------------------------------------------


async def test_detail_projected_timeline_and_referent(
    portal_client: AsyncClient,
    manager_agent: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    case, _ = await _case_with_journey(
        portal_client, manager_agent, expat, make_client_case, headers
    )
    detail = (
        await portal_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))
    ).json()

    by_name = {step["name"]: step for step in detail["timeline"]}
    assert by_name["Visa"]["status"] == "todo"
    assert by_name["Residence card"]["status"] == "blocked"
    assert by_name["Residence card"]["blocked_by"] == ["Visa"]  # NAMES, no ids
    assert detail["referent"]["email"] == manager_agent.email
    assert detail["status"] == "prospect"  # raw data; labels are frontend


async def test_responsible_displayable(
    portal_client: AsyncClient,
    manager_agent: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(manager_agent)
    case, timeline = await _case_with_journey(
        portal_client, manager_agent, expat, make_client_case, headers
    )
    contact = await make_external_contact(case=case, name="Maitre Robert")
    step_a, step_b = timeline[0]["id"], timeline[1]["id"]
    await portal_client.put(
        f"/cases/{case.id}/steps/{step_a}/responsible",
        headers=headers,
        json={"responsible_type": "agent", "responsible_agent_id": str(manager_agent.id)},
    )
    await portal_client.put(
        f"/cases/{case.id}/steps/{step_b}/responsible",
        headers=headers,
        json={"responsible_type": "external", "responsible_external_id": str(contact.id)},
    )
    detail = (
        await portal_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))
    ).json()
    by_name = {step["name"]: step for step in detail["timeline"]}
    # Agent-responsible shows as "agency" with NO internal name.
    assert by_name["Visa"]["responsible"] == {"type": "agency", "name": None}
    assert by_name["Residence card"]["responsible"] == {
        "type": "external",
        "name": "Maitre Robert",
    }

    # Expat-responsible (the client themselves) → "you".
    await portal_client.put(
        f"/cases/{case.id}/steps/{step_a}/responsible",
        headers=headers,
        json={"responsible_type": "expat"},
    )
    refetched = (
        await portal_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))
    ).json()
    visa = next(s for s in refetched["timeline"] if s["name"] == "Visa")
    assert visa["responsible"] == {"type": "you", "name": None}


async def test_ownership_404_and_agent_token_401(
    portal_client: AsyncClient,
    manager_agent: Agent,
    expat: ExpatUser,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    case = await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
    )
    stranger = await make_expat_user()
    assert (
        await portal_client.get(f"/expat/cases/{case.id}", headers=expat_headers(stranger))
    ).status_code == 404
    assert (
        await portal_client.get("/expat/cases", headers=agent_headers(manager_agent))
    ).status_code == 401


# --- notifications (Q8) ----------------------------------------------------------------------


async def test_notifications_are_sent_in_app_reminders_only(
    portal_client: AsyncClient,
    manager_agent: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
    expat_headers: AuthHeaders,
) -> None:
    case = await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
    )
    visible = await make_reminder(
        case=case, channel="in_app", status="sent", message_body="Your step moved!"
    )
    await make_reminder(case=case, channel="in_app", status="to_approve")  # not sent
    await make_reminder(case=case, channel="mail", status="sent")  # wrong channel

    response = await portal_client.get(
        f"/expat/cases/{case.id}/notifications", headers=expat_headers(expat)
    )
    assert response.status_code == 200
    [notification] = response.json()
    assert notification["id"] == str(visible.id)
    assert notification["message_body"] == "Your step moved!"
    assert datetime.fromisoformat(notification["sent_at"]).tzinfo is not None
    assert datetime.fromisoformat(notification["sent_at"]) <= datetime.now(UTC)
