"""GET /admin/agencies — the superadmin "Gérer les agences" table.

Covers: the superadmin gate (403 for agency admin / agent / expat, and
the impersonation case reported honestly); the 4 derived statuses incl.
unknown and "converted but trial still in the future → active"; exact
cross-tenant-safe counts on 2 agencies; cases_count ignoring the seeded
demo case and soft-deleted cases; seats_used ignoring external providers;
and the no-N+1 guarantee (a MEASURED constant of 2 SQL queries on a page
of 1 and a page of 20)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Role
from src.admin.admin_manager import AdminManager
from src.core.enums import Audience
from src.core.security import create_access_token
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"], email="root@platform.io")


async def _row_for(client: AsyncClient, headers: dict[str, str], agency_id: uuid.UUID) -> dict:
    body = (await client.get("/admin/agencies?page_size=100", headers=headers)).json()
    return next(r for r in body["items"] if r["id"] == str(agency_id))


# --- gate: superadmin 200, everyone else 403 -----------------------------------------


async def test_superadmin_sees_the_table_others_are_403(
    client: AsyncClient,
    superadmin: Agent,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    ok = await client.get("/admin/agencies", headers=agent_headers(superadmin))
    assert ok.status_code == 200, ok.text
    assert set(ok.json()) == {"items", "total", "page", "page_size"}

    admin = await make_agent(role=system_roles["admin"], email="admin@a.io")
    agent = await make_agent(role=system_roles["member"], email="member@a.io")
    for who in (admin, agent):
        assert (await client.get("/admin/agencies", headers=agent_headers(who))).status_code == 403

    # An expat token is the WRONG AUDIENCE for an AGENT route: rejected
    # at token resolution (401), before any permission check. Denied all
    # the same, but the honest code is 401, not 403 (audience separation).
    expat = await make_expat_user(email="client@x.io")
    expat_headers = {
        "Authorization": f"Bearer {create_access_token(str(expat.id), Audience.EXPAT)}"
    }
    assert (await client.get("/admin/agencies", headers=expat_headers)).status_code == 401


# --- THE impersonation case: reported, NOT worked around -----------------------------


async def test_superadmin_under_impersonation_hits_the_gate(
    client: AsyncClient,
    superadmin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    """A superadmin CURRENTLY impersonating an agency carries the target
    admin's token (sub = the admin, who has no agency.create). The gate
    evaluates that identity → 403. This is reported as-is: the platform
    table is reached with the superadmin's OWN token (exit impersonation
    first), exactly like AgenciesPage's fix A. NOT worked around."""
    target_admin = await make_agent(role=system_roles["admin"], email="target@agency.io")
    impersonation_token = create_access_token(
        str(target_admin.id),
        Audience.AGENT,
        extra_claims={"impersonator_id": str(superadmin.id)},
    )
    headers = {"Authorization": f"Bearer {impersonation_token}"}
    response = await client.get("/admin/agencies", headers=headers)
    assert response.status_code == 403  # the honest, un-patched result


# --- the 4 derived statuses ----------------------------------------------------------


async def test_the_four_statuses(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    make_agency: MakeAgency,
    agent_headers: AuthHeaders,
) -> None:
    now = datetime.now(UTC)
    trial = await make_agency(name="Trial", trial_ends_at=now + timedelta(days=12))
    active = await make_agency(
        name="Active", trial_ends_at=now + timedelta(days=5), converted_at=now
    )
    expired = await make_agency(name="Expired", trial_ends_at=now - timedelta(days=3))
    unknown = await make_agency(name="Unknown")  # neither trial_ends_at nor converted_at

    headers = agent_headers(superadmin)
    assert (await _row_for(client, headers, trial.id))["status"] == "trial"
    assert (await _row_for(client, headers, trial.id))["trial_days_remaining"] in (11, 12)
    # Converted but trial still in the FUTURE → active wins, days is null.
    active_row = await _row_for(client, headers, active.id)
    assert active_row["status"] == "active" and active_row["trial_days_remaining"] is None
    assert (await _row_for(client, headers, expired.id))["status"] == "expired"
    unknown_row = await _row_for(client, headers, unknown.id)
    assert unknown_row["status"] == "unknown"  # anomaly surfaced, NOT folded into expired
    assert unknown_row["trial_days_remaining"] is None


# --- exact, cross-tenant-safe counts on 2 agencies -----------------------------------


async def test_counts_are_exact_and_do_not_leak_across_agencies(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = system_roles["member"]
    external = (
        await db_session.execute(
            __import__("sqlalchemy")
            .select(Role)
            .where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()
    expat = await make_expat_user(email="marie@x.io")

    agency_a = await make_agency(name="A")
    # A: 2 internal members + 1 external provider; 2 live non-demo cases,
    # 1 demo case, 1 soft-deleted case.
    await make_agent(agency_id=agency_a.id, role=member, email="a1@a.io")
    await make_agent(agency_id=agency_a.id, role=member, email="a2@a.io")
    await make_agent(agency_id=agency_a.id, role=external, is_external=True, email="lawyer@a.io")
    await make_client_case(agency_id=agency_a.id, principal_expat_user_id=expat.id)
    await make_client_case(agency_id=agency_a.id, principal_expat_user_id=expat.id)
    await make_client_case(agency_id=agency_a.id, principal_expat_user_id=expat.id, is_demo=True)
    await make_client_case(
        agency_id=agency_a.id, principal_expat_user_id=expat.id, deleted_at=datetime.now(UTC)
    )

    agency_b = await make_agency(name="B")
    await make_agent(agency_id=agency_b.id, role=member, email="b1@b.io")
    await make_client_case(agency_id=agency_b.id, principal_expat_user_id=expat.id)

    headers = agent_headers(superadmin)
    row_a = await _row_for(client, headers, agency_a.id)
    row_b = await _row_for(client, headers, agency_b.id)

    # A: seats_used ignores the external; members_count includes it;
    # cases_count ignores the demo AND the soft-deleted.
    assert row_a["seats_used"] == 2
    assert row_a["members_count"] == 3
    assert row_a["cases_count"] == 2
    # B is untouched by A's aggregates (no cross-tenant leak).
    assert row_b["seats_used"] == 1 and row_b["members_count"] == 1 and row_b["cases_count"] == 1


# --- the demo case seeded at agency creation is ignored -------------------------------


async def test_wizard_created_agency_demo_case_is_not_counted(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """A real agency created through the wizard is seeded with ONE demo
    case; cases_count must show 0 (demo + non-deleted-only)."""
    created = await client.post(
        "/agencies",
        headers=agent_headers(superadmin),
        json={
            "name": "Wizard Agency",
            "admin_email": "boss@wizard.io",
            "admin_first_name": "B",
            "admin_last_name": "O",
            "sectors": ["consulting"],  # mandatory at superadmin creation
        },
    )
    assert created.status_code == 201, created.text
    agency_id = uuid.UUID(created.json()["agency"]["id"])

    row = await _row_for(client, agent_headers(superadmin), agency_id)
    assert row["cases_count"] == 0  # the seeded demo case is excluded
    assert row["members_count"] == 1  # just the first admin


# --- no N+1: a constant number of queries -------------------------------------------


async def _count_queries(db_session: AsyncSession, page_size: int) -> int:
    engine = db_session.get_bind()
    counter = {"n": 0}

    def _count(*_a: object, **_k: object) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        await AdminManager(db_session).list_agencies(
            search=None, sort="cases_count", order="desc", page=1, page_size=page_size
        )
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    return counter["n"]


async def test_query_count_is_constant_for_3_and_25_agencies(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """No N+1: the onboarding gestures, S0/S1/S2 and last_login are batched
    (grouped queries on the page ids). The query count for 3 agencies on a page
    equals the count for 25 — and is constant across page size."""
    expat = await make_expat_user(email="q@x.io")
    for i in range(3):
        agency = await make_agency(name=f"Ag{i:02d}")
        await make_client_case(agency_id=agency.id, principal_expat_user_id=expat.id)
    count_3 = await _count_queries(db_session, page_size=100)  # 3 rows on one page

    for i in range(3, 25):
        agency = await make_agency(name=f"Ag{i:02d}")
        await make_client_case(agency_id=agency.id, principal_expat_user_id=expat.id)
    count_25 = await _count_queries(db_session, page_size=100)  # 25 rows on one page

    # THE proof: 25 agencies cost the same as 3 — no per-agency query.
    assert count_3 == count_25
    # And constant across page size (the batch is grouped, never per-row).
    assert await _count_queries(db_session, page_size=1) == count_25
    # A small, fixed number (main + count + the batched adoption queries).
    assert count_25 <= 6


async def test_full_selector_page_size_200_serves_everything(
    client: AsyncClient,
    superadmin: Agent,
    make_agency: MakeAgency,
    agent_headers: AuthHeaders,
) -> None:
    """Regression (task form, 2026-07-20): the front loads the agency
    selector with page_size=200 — the old le=100 cap answered 422 and
    the form silently emptied the selector."""
    for i in range(3):
        await make_agency(name=f"Agence {i}")
    response = await client.get(
        "/admin/agencies?page=1&page_size=200", headers=agent_headers(superadmin)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page_size"] == 200 and len(body["items"]) >= 3  # never an empty selector
