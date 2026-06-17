""" "Action validée par" (refonte completion_mode) — the completion engine on
all 4 validator branches + the client/external validate endpoints (D3).

Non-regression is the criterion: `none` self-completes (= ex-`auto`), `agent`
waits for the agency (= ex-`agency_validation`). New: `expat`/`external` never
auto-complete — only their dedicated validate action closes them; an agent can
ALWAYS force-close (operational safety); and the RGPD evasion (a non-designated
actor cannot validate) is refused server-side, not front-masked.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def vc(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"], first_name="Own", last_name="Er")


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


@pytest_asyncio.fixture
async def external_role(db_session: AsyncSession, rbac_baseline: None) -> Role:
    return (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()


@pytest_asyncio.fixture
async def external(make_agent: MakeAgent, admin: Agent, external_role: Role) -> Agent:
    return await make_agent(
        agency_id=admin.agency_id,
        role=external_role,
        is_external=True,
        email="prov@ext.com",
        first_name="Robert",
        last_name="Lawyer",
    )


# --- builders ------------------------------------------------------------------------


async def _journey_step(
    vc: AsyncClient, ah: dict[str, str], *, validated_by_type: str | None, with_req: bool = True
) -> tuple[str, str]:
    tid = (await vc.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    body: dict = {"name": "Collecte"}
    if validated_by_type is not None:
        body["validated_by_type"] = validated_by_type
    sid = (await vc.post(f"/journeys/{tid}/steps", headers=ah, json=body)).json()["id"]
    if with_req:
        await vc.post(
            f"/journeys/{tid}/steps/{sid}/requirements",
            headers=ah,
            json={"kind": "base_field", "reference": "passport_number", "scope": "principal"},
        )
    return tid, sid


async def _start_case(
    vc: AsyncClient,
    ah: dict[str, str],
    make_client_case: MakeClientCase,
    admin: Agent,
    principal: ExpatUser,
    tid: str,
) -> tuple[ClientCase, str]:
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    steps = (
        await vc.post(f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid})
    ).json()
    pid = steps[0]["id"]
    # Activate the step (materializes its requirements, allows completion).
    await vc.patch(f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "in_progress"})
    return case, pid


async def _fill_passport(
    vc: AsyncClient, expat_headers: AuthHeaders, expat: ExpatUser, case_id: str, pid: str
) -> dict:
    detail = (await vc.get(f"/expat/cases/{case_id}", headers=expat_headers(expat))).json()
    step = next(s for s in detail["timeline"] if s["progress_id"] == pid)
    rid = step["requirements"][0]["id"]
    put = await vc.put(
        f"/expat/cases/{case_id}/requirements/{rid}",
        headers=expat_headers(expat),
        json={"value": "AB12345"},
    )
    assert put.status_code == 200, put.text
    return put.json()


def _agency_step(vc_json: dict, pid: str) -> dict:
    return next(s for s in vc_json["timeline"] if s["progress_id"] == pid)


# --- NON-REGRESSION: none self-completes, agent waits --------------------------------


async def test_none_validator_self_completes(
    vc: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """'none' (= ex completion_mode 'auto'): all requirements met → DONE."""
    ah = agent_headers(admin)
    tid, _ = await _journey_step(vc, ah, validated_by_type="none")
    case, pid = await _start_case(vc, ah, make_client_case, admin, expat, tid)
    detail = await _fill_passport(vc, expat_headers, expat, str(case.id), pid)
    assert _agency_step(detail, pid)["status"] == "done"


async def test_agent_validator_waits_then_agency_closes(
    vc: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """'agent' (= ex 'agency_validation'): met → NOT auto-closed; the agency
    closes via the existing PATCH done. UNCHANGED behaviour."""
    ah = agent_headers(admin)
    tid, _ = await _journey_step(vc, ah, validated_by_type="agent")
    case, pid = await _start_case(vc, ah, make_client_case, admin, expat, tid)
    detail = await _fill_passport(vc, expat_headers, expat, str(case.id), pid)
    assert _agency_step(detail, pid)["status"] == "in_progress"  # waits
    closed = await vc.patch(f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "done"})
    assert closed.status_code == 200
    assert closed.json()["status"] == "done"


# --- D3: client validates -------------------------------------------------------------


async def test_expat_validator_waits_then_client_validates(
    vc: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid, _ = await _journey_step(vc, ah, validated_by_type="expat")
    case, pid = await _start_case(vc, ah, make_client_case, admin, expat, tid)
    # Filling requirements does NOT close an expat-validated step.
    detail = await _fill_passport(vc, expat_headers, expat, str(case.id), pid)
    step = _agency_step(detail, pid)
    assert step["status"] == "in_progress"
    assert step["can_validate"] is True  # the client sees the button

    # The client validates → DONE.
    done = await vc.post(
        f"/expat/cases/{case.id}/steps/{pid}/validate", headers=expat_headers(expat)
    )
    assert done.status_code == 200
    assert _agency_step(done.json(), pid)["status"] == "done"


async def test_agent_can_always_force_close_expat_validated(
    vc: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Operational safety: the validator config never locks the agency out
    of closing a step manually (unblocking a stuck dossier)."""
    ah = agent_headers(admin)
    tid, _ = await _journey_step(vc, ah, validated_by_type="expat", with_req=False)
    case, pid = await _start_case(vc, ah, make_client_case, admin, expat, tid)
    closed = await vc.patch(f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "done"})
    assert closed.status_code == 200
    assert closed.json()["status"] == "done"


async def test_expat_cannot_validate_agency_step(
    vc: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """A step validated by the agency is not the client's to close → 409."""
    ah = agent_headers(admin)
    tid, _ = await _journey_step(vc, ah, validated_by_type="agent", with_req=False)
    case, pid = await _start_case(vc, ah, make_client_case, admin, expat, tid)
    resp = await vc.post(
        f"/expat/cases/{case.id}/steps/{pid}/validate", headers=expat_headers(expat)
    )
    assert resp.status_code == 409


async def test_expat_cannot_validate_foreign_case(
    vc: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid, _ = await _journey_step(vc, ah, validated_by_type="expat", with_req=False)
    case, pid = await _start_case(vc, ah, make_client_case, admin, expat, tid)
    stranger = await make_expat_user(email="stranger@example.com")
    resp = await vc.post(
        f"/expat/cases/{case.id}/steps/{pid}/validate", headers=expat_headers(stranger)
    )
    assert resp.status_code == 404  # ownership border, never reveals existence


# --- D3: provider validates + RGPD evasion -------------------------------------------


async def _designate_external_validator(
    vc: AsyncClient, ah: dict[str, str], case_id: str, pid: str, external: Agent
) -> None:
    assigned = await vc.post(
        f"/cases/{case_id}/external-assignments", headers=ah, json={"agent_id": str(external.id)}
    )
    assert assigned.status_code == 201, assigned.text
    r = await vc.put(
        f"/cases/{case_id}/steps/{pid}/validator",
        headers=ah,
        json={"validated_by_type": "external", "validated_by_agent_id": str(external.id)},
    )
    assert r.status_code == 200, r.text


async def test_external_validator_validates(
    vc: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    external: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), agent_headers(external)
    tid, _ = await _journey_step(vc, ah, validated_by_type="agent", with_req=False)
    case, pid = await _start_case(vc, ah, make_client_case, admin, expat, tid)
    await _designate_external_validator(vc, ah, str(case.id), pid, external)

    # The provider sees the validate flag on its timeline…
    detail = (await vc.get(f"/external/cases/{case.id}", headers=eh)).json()
    assert _agency_step(detail, pid)["can_validate"] is True
    # …and closes the step.
    done = await vc.post(f"/external/cases/{case.id}/steps/{pid}/validate", headers=eh)
    assert done.status_code == 200
    assert _agency_step(done.json(), pid)["status"] == "done"


async def test_non_designated_external_cannot_validate(
    vc: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    external: Agent,
    make_agent: MakeAgent,
    external_role: Role,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """EVASION (server-side): another assigned provider — NOT the step's
    designated validator — gets 404, never closes, never learns the validator."""
    ah = agent_headers(admin)
    other = await make_agent(
        agency_id=admin.agency_id, role=external_role, is_external=True, email="other@ext.com"
    )
    tid, _ = await _journey_step(vc, ah, validated_by_type="agent", with_req=False)
    case, pid = await _start_case(vc, ah, make_client_case, admin, expat, tid)
    await _designate_external_validator(vc, ah, str(case.id), pid, external)
    # `other` is assigned to the case but is NOT the step's validator.
    await vc.post(
        f"/cases/{case.id}/external-assignments", headers=ah, json={"agent_id": str(other.id)}
    )
    detail = (await vc.get(f"/external/cases/{case.id}", headers=agent_headers(other))).json()
    assert _agency_step(detail, pid)["can_validate"] is False  # no button
    resp = await vc.post(
        f"/external/cases/{case.id}/steps/{pid}/validate", headers=agent_headers(other)
    )
    assert resp.status_code == 404  # refused server-side


async def test_set_external_validator_requires_assignment(
    vc: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    external: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Naming an UNASSIGNED provider validator is refused (wave-B coherence:
    no validator without dossier access)."""
    ah = agent_headers(admin)
    tid, _ = await _journey_step(vc, ah, validated_by_type="agent", with_req=False)
    case, pid = await _start_case(vc, ah, make_client_case, admin, expat, tid)
    resp = await vc.put(
        f"/cases/{case.id}/steps/{pid}/validator",
        headers=ah,
        json={"validated_by_type": "external", "validated_by_agent_id": str(external.id)},
    )
    assert resp.status_code == 422  # assign first
