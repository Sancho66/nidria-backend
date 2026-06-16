"""Client fulfillment of CASE-level requirements (sections chantier,
vague C2) — a bounded write to a client_case column. The security wave:
every assertion re-reads the DB after commit, not just the status code,
because the border must hold in REALITY, not in intention.

Borders: (a) get_case_for_expat → 404, (b) declaration on a step of THIS
case → 404, (c) step IN_PROGRESS → 409, (d) the column is the
DECLARATION's case_field, NEVER the payload — the client can write ONLY
that one column."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def cf_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


async def _step(
    client: AsyncClient, headers: dict[str, str], *, completion_mode: str = "agency_validation"
) -> tuple[str, str]:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    step = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "Collecte", "completion_mode": completion_mode},
        )
    ).json()
    return tid, step["id"]


async def _declare(
    client: AsyncClient, headers: dict[str, str], tid: str, sid: str, ref: str
) -> None:
    r = await client.post(
        f"/journeys/{tid}/steps/{sid}/case-requirements", headers=headers, json={"case_field": ref}
    )
    assert r.status_code == 201, r.text


async def _assign(
    client: AsyncClient, headers: dict[str, str], case_id: str, tid: str, *, activate: bool = True
) -> str:
    steps = (
        await client.post(
            f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    pid = steps[0]["id"]
    if activate:
        started = await client.patch(
            f"/cases/{case_id}/steps/{pid}", headers=headers, json={"status": "in_progress"}
        )
        assert started.status_code == 200
    return pid


async def _find_case_req(
    client: AsyncClient, expat_headers: AuthHeaders, expat: ExpatUser, case_id: str, ref: str
) -> str:
    detail = (await client.get(f"/expat/cases/{case_id}", headers=expat_headers(expat))).json()
    for step in detail["timeline"]:
        for req in step["requirements"]:
            if req["target"] == "case" and req["reference"] == ref:
                return req["id"]
    raise AssertionError(f"case requirement {ref!r} not exposed")


async def _reload(db: AsyncSession, case_id: uuid.UUID) -> ClientCase:
    # populate_existing → force a fresh SELECT and overwrite the
    # identity-map instance with the COMMITTED values (read the truth),
    # without expire_all() which would expire the fixtures' objects.
    case = await db.get(ClientCase, case_id, populate_existing=True)
    assert case is not None
    return case


# --- THE evasion test (re-reads the DB, not the status code) -------------------------


async def test_payload_cannot_target_another_column(
    cf_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """The case-req declares dest_city. A bricolaged payload tries to also
    set owner_agent_id / status / origin_country. ONLY dest_city moves —
    proven by re-reading client_case after commit."""
    headers = agent_headers(admin)
    tid, sid = await _step(cf_client, headers)
    await _declare(cf_client, headers, tid, sid, "dest_city")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, origin_country="FR"
    )
    await _assign(cf_client, headers, str(case.id), tid)
    creq_id = await _find_case_req(cf_client, expat_headers, expat, str(case.id), "dest_city")

    before = await _reload(db_session, case.id)
    before_status, before_owner, before_origin = (
        before.status,
        before.owner_agent_id,
        before.origin_country,
    )

    resp = await cf_client.put(
        f"/expat/cases/{case.id}/case-requirements/{creq_id}",
        headers=expat_headers(expat),
        json={
            "value": "Lyon",
            # bricolage — these keys must be IGNORED (schema carries only `value`).
            "owner_agent_id": str(uuid.uuid4()),
            "status": "closed",
            "origin_country": "XX",
            "dest_city": "HACKED",  # even a matching key: value comes from `value`, not here
        },
    )
    assert resp.status_code == 200, resp.text

    after = await _reload(db_session, case.id)
    assert after.dest_city == "Lyon"  # ONLY the declared column, value from `value`
    assert after.status == before_status  # untouched
    assert after.owner_agent_id == before_owner  # untouched
    assert after.origin_country == before_origin == "FR"  # untouched


# --- cross-client --------------------------------------------------------------------


async def test_cross_client_404_no_write(
    cf_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(cf_client, headers)
    await _declare(cf_client, headers, tid, sid, "dest_city")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, dest_city=None
    )
    await _assign(cf_client, headers, str(case.id), tid)
    creq_id = await _find_case_req(cf_client, expat_headers, expat, str(case.id), "dest_city")

    other = await make_expat_user(email="intruder@example.com")
    resp = await cf_client.put(
        f"/expat/cases/{case.id}/case-requirements/{creq_id}",
        headers=expat_headers(other),  # NOT the principal
        json={"value": "Lyon"},
    )
    assert resp.status_code == 404
    after = await _reload(db_session, case.id)
    assert after.dest_city is None  # nothing written


# --- step not active -----------------------------------------------------------------


async def test_inactive_step_409_no_write(
    cf_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(cf_client, headers)
    await _declare(cf_client, headers, tid, sid, "dest_city")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, dest_city=None
    )
    await _assign(cf_client, headers, str(case.id), tid, activate=False)  # step stays TODO
    creq_id = await _find_case_req(cf_client, expat_headers, expat, str(case.id), "dest_city")

    resp = await cf_client.put(
        f"/expat/cases/{case.id}/case-requirements/{creq_id}",
        headers=expat_headers(expat),
        json={"value": "Lyon"},
    )
    assert resp.status_code == 409
    after = await _reload(db_session, case.id)
    assert after.dest_city is None


async def test_unknown_case_requirement_404(
    cf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    resp = await cf_client.put(
        f"/expat/cases/{case.id}/case-requirements/{uuid.uuid4()}",
        headers=expat_headers(expat),
        json={"value": "Lyon"},
    )
    assert resp.status_code == 404


# --- invalid value -------------------------------------------------------------------


async def test_invalid_value_422_no_write(
    cf_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """origin_country has a ^[A-Z]{2}$ pattern (reused from CaseUpdateRequest):
    'FRANCE' → 422, and the column is NOT written."""
    headers = agent_headers(admin)
    tid, sid = await _step(cf_client, headers)
    await _declare(cf_client, headers, tid, sid, "origin_country")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, origin_country=None
    )
    await _assign(cf_client, headers, str(case.id), tid)
    creq_id = await _find_case_req(cf_client, expat_headers, expat, str(case.id), "origin_country")

    resp = await cf_client.put(
        f"/expat/cases/{case.id}/case-requirements/{creq_id}",
        headers=expat_headers(expat),
        json={"value": "FRANCE"},
    )
    assert resp.status_code == 422
    after = await _reload(db_session, case.id)
    assert after.origin_country is None  # nothing written


# --- happy path + auto-completion ----------------------------------------------------


async def test_happy_path_writes_and_auto_completes(
    cf_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(cf_client, headers, completion_mode="auto")
    await _declare(cf_client, headers, tid, sid, "dest_country")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, dest_country=None
    )
    await _assign(cf_client, headers, str(case.id), tid)
    creq_id = await _find_case_req(cf_client, expat_headers, expat, str(case.id), "dest_country")

    resp = await cf_client.put(
        f"/expat/cases/{case.id}/case-requirements/{creq_id}",
        headers=expat_headers(expat),
        json={"value": "PY"},
    )
    assert resp.status_code == 200
    after = await _reload(db_session, case.id)
    assert after.dest_country == "PY"  # written to client_case
    # auto step, all reqs met → auto-completed.
    detail = (await cf_client.get(f"/cases/{case.id}", headers=headers)).json()
    step = next(s for s in detail["progress"] if s["template_step_id"] == sid)
    assert step["status"] == "done"


# --- best-effort: a mail failure never blocks the write ------------------------------


async def test_mail_failure_does_not_block_write(
    cf_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agency_validation: filling the case-req arms a ready-to-validate
    mail. Even if sending THROWS, the value is committed (send_pending is
    best-effort, after commit)."""
    import src.progress.progress_manager as pm

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("smtp down")

    monkeypatch.setattr(pm, "send_email", _boom)

    headers = agent_headers(admin)
    tid, sid = await _step(cf_client, headers, completion_mode="agency_validation")
    await _declare(cf_client, headers, tid, sid, "dest_city")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, dest_city=None
    )
    await _assign(cf_client, headers, str(case.id), tid)
    creq_id = await _find_case_req(cf_client, expat_headers, expat, str(case.id), "dest_city")

    resp = await cf_client.put(
        f"/expat/cases/{case.id}/case-requirements/{creq_id}",
        headers=expat_headers(expat),
        json={"value": "Lyon"},
    )
    assert resp.status_code == 200  # the mail failure was swallowed
    after = await _reload(db_session, case.id)
    assert after.dest_city == "Lyon"  # the value IS written despite the mail throwing
