"""Step deadline + days-remaining counter. Priority: firm due_at >
estimated_days-derived > none. Exposed identically on both faces, with
the started_at derivation BATCHED (one MIN over activity_log, no N+1)."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.progress import progress_repository
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def dl_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com")


async def _journey(
    client: AsyncClient, headers: dict[str, str], steps: list[tuple[str, int | None]]
) -> str:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    for name, est in steps:
        await client.post(
            f"/journeys/{tid}/steps", headers=headers, json={"name": name, "estimated_days": est}
        )
    return tid


async def _assign(
    client: AsyncClient, headers: dict[str, str], case_id: str, tid: str
) -> list[dict]:
    return (
        await client.post(
            f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()


def _step(detail: dict, pid: str) -> dict:
    return next(s for s in detail["progress"] if s["id"] == pid)


# --- due_at set / edit / clear (gate case.edit) --------------------------------------


async def test_due_at_settable_editable_clearable(
    dl_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid = await _journey(dl_client, ah, [("S1", 10)])
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = (await _assign(dl_client, ah, str(case.id), tid))[0]["id"]
    due = (datetime.now(UTC) + timedelta(days=5)).isoformat()

    set_ = await dl_client.patch(f"/cases/{case.id}/steps/{pid}", headers=ah, json={"due_at": due})
    assert set_.status_code == 200, set_.text
    assert set_.json()["due_at"] is not None

    # Clear it.
    cleared = await dl_client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=ah, json={"due_at": None}
    )
    assert cleared.status_code == 200
    assert cleared.json()["due_at"] is None
    assert cleared.json()["counter"]["source"] is None  # no deadline, not started → no gauge


async def test_due_at_gate_case_edit(
    dl_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid = await _journey(dl_client, ah, [("S1", 10)])
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = (await _assign(dl_client, ah, str(case.id), tid))[0]["id"]
    viewer = await make_agent(agency_id=admin.agency_id, role=system_roles["viewer"])
    denied = await dl_client.patch(
        f"/cases/{case.id}/steps/{pid}",
        headers=agent_headers(viewer),  # case.view only
        json={"due_at": datetime.now(UTC).isoformat()},
    )
    assert denied.status_code == 403


# --- counter resolution + priority ---------------------------------------------------


async def test_counter_estimated_when_started_no_deadline(
    dl_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid = await _journey(dl_client, ah, [("S1", 10)])
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = (await _assign(dl_client, ah, str(case.id), tid))[0]["id"]
    await dl_client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "in_progress"}
    )

    counter = _step((await dl_client.get(f"/cases/{case.id}", headers=ah)).json(), pid)["counter"]
    assert counter["source"] == "estimated"
    assert counter["days_remaining"] == 10  # started today + 10 estimated days
    assert counter["target_date"] is not None


async def test_counter_none_when_not_started_and_no_deadline(
    dl_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    # estimated_days present but step never started → no started_at → no gauge.
    tid = await _journey(dl_client, ah, [("S1", 10)])
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = (await _assign(dl_client, ah, str(case.id), tid))[0]["id"]
    counter = _step((await dl_client.get(f"/cases/{case.id}", headers=ah)).json(), pid)["counter"]
    assert counter == {"target_date": None, "days_remaining": None, "source": None}


async def test_counter_deadline_takes_priority_over_estimated(
    dl_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid = await _journey(dl_client, ah, [("S1", 10)])
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = (await _assign(dl_client, ah, str(case.id), tid))[0]["id"]
    await dl_client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "in_progress"}
    )
    # Firm deadline in 5 days — even though estimated would give 10.
    due = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    await dl_client.patch(f"/cases/{case.id}/steps/{pid}", headers=ah, json={"due_at": due})

    counter = _step((await dl_client.get(f"/cases/{case.id}", headers=ah)).json(), pid)["counter"]
    assert counter["source"] == "deadline"  # firm wins
    assert counter["days_remaining"] == 5


async def test_counter_negative_when_overdue(
    dl_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid = await _journey(dl_client, ah, [("S1", None)])
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = (await _assign(dl_client, ah, str(case.id), tid))[0]["id"]
    past = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    await dl_client.patch(f"/cases/{case.id}/steps/{pid}", headers=ah, json={"due_at": past})
    counter = _step((await dl_client.get(f"/cases/{case.id}", headers=ah)).json(), pid)["counter"]
    assert counter["source"] == "deadline"
    assert counter["days_remaining"] == -3  # overdue


# --- both faces identical ------------------------------------------------------------


async def test_counter_identical_both_faces(
    dl_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    tid = await _journey(dl_client, ah, [("S1", 7)])
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = (await _assign(dl_client, ah, str(case.id), tid))[0]["id"]
    await dl_client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "in_progress"}
    )

    agent_counter = _step((await dl_client.get(f"/cases/{case.id}", headers=ah)).json(), pid)[
        "counter"
    ]
    detail = (await dl_client.get(f"/expat/cases/{case.id}", headers=eh)).json()
    expat_counter = detail["timeline"][0]["counter"]
    assert agent_counter == expat_counter  # one computation, two faces


# --- batched started_at (no N+1) -----------------------------------------------------


async def test_started_at_is_batched_no_n_plus_one(
    dl_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeline with N steps must resolve started_at in ONE query, not
    N MIN queries — the flagged technical trap."""
    ah = agent_headers(admin)
    tid = await _journey(dl_client, ah, [("S1", 5), ("S2", 5), ("S3", 5)])
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    steps = await _assign(dl_client, ah, str(case.id), tid)
    for s in steps:
        await dl_client.patch(
            f"/cases/{case.id}/steps/{s['id']}", headers=ah, json={"status": "in_progress"}
        )

    original = progress_repository.ProgressRepository.started_ats
    calls: list[list] = []

    async def counting(self: object, ids: list) -> dict:
        calls.append(ids)
        return await original(self, ids)  # type: ignore[arg-type]

    monkeypatch.setattr(progress_repository.ProgressRepository, "started_ats", counting)
    detail = (await dl_client.get(f"/cases/{case.id}", headers=ah)).json()
    assert len(detail["progress"]) == 3
    assert len(calls) == 1  # ONE batched call for all 3 steps, not 3
    assert len(calls[0]) == 3  # the single call carried all progress ids
    # And every started step got its estimated counter.
    assert all(s["counter"]["source"] == "estimated" for s in detail["progress"])
