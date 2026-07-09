"""Agency-internal cost tracking (Reside). The THIRD nature — what the agency
NOTES FOR ITSELF — structurally absent from the expat and external faces. The
tests ARE the safety: no client/provider ever sees a cost (two independent
barriers, proven separately), cross-agency isolation, the two-permission split,
an exact Decimal total (no float), and case_export carrying no cost."""

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.core.rbac.permissions import Permission
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

# A distinctive label/amount that must NEVER surface on a client/provider face.
# Two decimals so it is valid under the default test currency (EUR).
SENTINEL = "COUTSECRETXYZ"
SENTINEL_AMOUNT = "987654.32"


@pytest.fixture
def costs_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(
    make_agent: MakeAgent, system_roles: dict[str, Role], db_session: AsyncSession
) -> Agent:
    # admin holds cost.view AND cost.manage (my proposal — flagged for Eric).
    agent = await make_agent(role=system_roles["admin"])
    # A currency is required to record costs — default the test agency to EUR
    # (the total test overrides it per-currency).
    await _set_currency(db_session, agent.agency_id, "EUR")
    return agent


async def _set_currency(db: AsyncSession, agency_id: uuid.UUID, code: str) -> None:
    await db.execute(update(Agency).where(Agency.id == agency_id).values(currency=code))
    await db.commit()


async def _case_with_steps(
    client: AsyncClient,
    agency_id: uuid.UUID,
    make_client_case: MakeClientCase,
    headers: dict,
    n_steps: int,
    principal_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, list[str]]:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    for i in range(n_steps):
        await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": f"Step {i}"})
    kwargs: dict = {"agency_id": agency_id}
    if principal_id is not None:
        kwargs["principal_expat_user_id"] = principal_id
    case = await make_client_case(**kwargs)
    timeline = (
        await client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    return case.id, [s["id"] for s in timeline]


async def _add_cost(
    client: AsyncClient, headers: dict, case_id: uuid.UUID, pid: str, amount: str, label: str
) -> dict:
    r = await client.post(
        f"/cases/{case_id}/steps/{pid}/costs",
        headers=headers,
        json={"amount": amount, "label": label},
    )
    assert r.status_code == 201, r.text
    return r.json()


# --- total exact, three steps, six lines, PYG (0 dec) and EUR (2 dec), no float ------


@pytest.mark.parametrize(
    "currency,amounts",
    [
        ("PYG", ["120000", "180000", "50000", "300000", "75000", "25000"]),
        ("EUR", ["120.50", "180.00", "50.25", "300.10", "75.99", "25.16"]),
    ],
)
async def test_total_is_exact_over_three_steps_six_lines(
    costs_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    currency: str,
    amounts: list[str],
) -> None:
    headers = agent_headers(admin)
    await _set_currency(db_session, admin.agency_id, currency)
    case_id, pids = await _case_with_steps(
        costs_client, admin.agency_id, make_client_case, headers, 3
    )
    for i, amount in enumerate(amounts):
        await _add_cost(costs_client, headers, case_id, pids[i % 3], amount, f"L{i}")

    body = (await costs_client.get(f"/cases/{case_id}/costs", headers=headers)).json()
    assert body["currency"] == currency
    assert len(body["lines"]) == 6
    # total + every amount are STRINGS (never a JSON float) — reconstruct exact
    # Decimals and compare by value.
    assert all(isinstance(line["amount"], str) for line in body["lines"])
    # Manual débours are REAL and unplanned: real_total sums them; planned_total
    # and variance are zero (no line carries a plan).
    assert isinstance(body["real_total"], str)
    assert Decimal(body["real_total"]) == sum(Decimal(a) for a in amounts)
    assert Decimal(body["planned_total"]) == 0
    assert Decimal(body["variance"]) == 0
    assert all(line["planned_amount"] is None for line in body["lines"])


# --- the two permissions: no cost.view → 403; view without manage → read-only --------


async def test_permission_split_view_and_manage(
    costs_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    make_role,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    case_id, pids = await _case_with_steps(
        costs_client, admin.agency_id, make_client_case, headers, 1
    )
    await _add_cost(costs_client, headers, case_id, pids[0], "10", "seed")

    # No cost.view at all → 403 on read.
    blind_role = await make_role(permissions=[Permission.CASE_VIEW], agency_id=admin.agency_id)
    blind = await make_agent(agency_id=admin.agency_id, role=blind_role)
    assert (
        await costs_client.get(f"/cases/{case_id}/costs", headers=agent_headers(blind))
    ).status_code == 403

    # cost.view but NOT cost.manage → reads, cannot write.
    viewer_role = await make_role(
        permissions=[Permission.CASE_VIEW, Permission.COST_VIEW], agency_id=admin.agency_id
    )
    viewer = await make_agent(agency_id=admin.agency_id, role=viewer_role)
    vh = agent_headers(viewer)
    assert (await costs_client.get(f"/cases/{case_id}/costs", headers=vh)).status_code == 200
    write = await costs_client.post(
        f"/cases/{case_id}/steps/{pids[0]}/costs", headers=vh, json={"amount": "5", "label": "x"}
    )
    assert write.status_code == 403


# --- cross-agency: agency B never sees agency A's costs ------------------------------


async def test_no_cross_agency_cost_access(
    costs_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    case_id, pids = await _case_with_steps(
        costs_client, admin.agency_id, make_client_case, headers, 1
    )
    await _add_cost(costs_client, headers, case_id, pids[0], "42", "secret")

    other_admin = await make_agent(role=system_roles["admin"])  # a DIFFERENT agency
    assert (
        await costs_client.get(f"/cases/{case_id}/costs", headers=agent_headers(other_admin))
    ).status_code == 404


# --- barrier 1: an external provider is 403 by the ALLOWLIST, before any permission --


async def test_external_provider_denied_by_allowlist_not_permission(
    costs_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The external agent is GIVEN cost.view — so if the permission were the
    gate, it would pass. It is 403 anyway: the is_external fail-closed allowlist
    denies FIRST (its message, not 'Missing permission')."""
    headers = agent_headers(admin)
    case_id, _ = await _case_with_steps(costs_client, admin.agency_id, make_client_case, headers, 1)

    ext_role = await make_role(permissions=[Permission.COST_VIEW], agency_id=admin.agency_id)
    external = await make_agent(agency_id=admin.agency_id, role=ext_role, is_external=True)
    resp = await costs_client.get(f"/cases/{case_id}/costs", headers=agent_headers(external))
    assert resp.status_code == 403
    # Proof it is the ALLOWLIST (not the permission check) that refused.
    assert "External providers have no access" in resp.json()["detail"]


# --- barrier 2: the external face never queries case_step_cost -----------------------


def test_external_schema_never_references_cost() -> None:
    """Structural: external_schema does not mention the cost table/model at all
    — the provider projection cannot carry a cost even by accident."""
    src = Path("src/external/external_schema.py").read_text(encoding="utf-8")
    # No table, no model, no field: "cost" appears nowhere in the provider
    # read contract.
    assert "cost" not in src.lower()


async def test_external_full_read_carries_no_cost(
    costs_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """A full read of the external face, on a case that HAS costs, never leaks
    the sentinel — across every /external/ GET route."""
    headers = agent_headers(admin)
    expat = await make_expat_user(email="c@x.io")
    case_id, pids = await _case_with_steps(
        costs_client, admin.agency_id, make_client_case, headers, 1, principal_id=expat.id
    )
    await _add_cost(costs_client, headers, case_id, pids[0], SENTINEL_AMOUNT, SENTINEL)

    ext_role = (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()
    external = await make_agent(agency_id=admin.agency_id, role=ext_role, is_external=True)
    await costs_client.post(
        f"/cases/{case_id}/external-assignments",
        headers=headers,
        json={"agent_id": str(external.id)},
    )
    eh = agent_headers(external)
    for path in _routes_with_prefix("/external/"):
        url = _fill_path(path, case_id)
        resp = await costs_client.get(url, headers=eh)
        assert SENTINEL not in resp.text and SENTINEL_AMOUNT not in resp.text, (path, resp.text)


# --- an expat never sees a cost, on ANY expat route (by comprehension) ---------------


async def test_expat_never_sees_a_cost_on_any_route(
    costs_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    expat = await make_expat_user(email="client@x.io")
    case_id, pids = await _case_with_steps(
        costs_client, admin.agency_id, make_client_case, headers, 1, principal_id=expat.id
    )
    await _add_cost(costs_client, headers, case_id, pids[0], SENTINEL_AMOUNT, SENTINEL)

    h = expat_headers(expat)
    routes = _routes_with_prefix("/expat/")
    assert len(routes) >= 4, routes
    for path in routes:
        url = _fill_path(path, case_id)
        resp = await costs_client.get(url, headers=h)
        # Whatever the status, the cost never appears — the expat face is blind
        # to it by construction (own table, own schema).
        assert SENTINEL not in resp.text and SENTINEL_AMOUNT not in resp.text, (path, resp.text)


# --- case_export (one of the 7) carries NO cost — proof the 7 stay intact ------------


async def test_case_export_contains_no_cost(
    costs_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    case_id, pids = await _case_with_steps(
        costs_client, admin.agency_id, make_client_case, headers, 1
    )
    await _add_cost(costs_client, headers, case_id, pids[0], SENTINEL_AMOUNT, SENTINEL)

    resp = await costs_client.get(f"/cases/{case_id}/export", headers=headers)
    assert resp.status_code == 200
    assert SENTINEL.encode() not in resp.content
    assert SENTINEL_AMOUNT.encode() not in resp.content


# --- helpers: route enumeration (comprehension) --------------------------------------


def _routes_with_prefix(prefix: str) -> list[str]:
    from fastapi.routing import APIRoute

    from src.main import app

    paths: set[str] = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(prefix) and "GET" in route.methods:
            paths.add(route.path)
    return sorted(paths)


def _fill_path(template: str, case_id: uuid.UUID) -> str:
    path = template.replace("{case_id}", str(case_id))
    while "{" in path:
        head, _, rest = path.partition("{")
        _param, _, tail = rest.partition("}")
        path = f"{head}{uuid.uuid4()}{tail}"
    return path


# --- currency: the missing half of a money feature (a cost has no unit without it) ---


async def test_recording_a_cost_requires_an_agency_currency(
    costs_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    # A FRESH agency has no currency (NULL) → recording a cost is a clean 409.
    fresh = await make_agent(role=system_roles["admin"])
    headers = agent_headers(fresh)
    case_id, pids = await _case_with_steps(
        costs_client, fresh.agency_id, make_client_case, headers, 1
    )
    resp = await costs_client.post(
        f"/cases/{case_id}/steps/{pids[0]}/costs",
        headers=headers,
        json={"amount": "10", "label": "x"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "cost.currency_required"


async def test_setting_currency_needs_agency_manage(
    costs_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    # admin holds agency.manage → 200 (no costs yet, change allowed).
    ok = await costs_client.patch(
        "/agencies/me", headers=agent_headers(admin), json={"currency": "USD"}
    )
    assert ok.status_code == 200
    # case_manager lacks agency.manage → 403 (never reaches the manager).
    cm = await make_agent(agency_id=admin.agency_id, role=system_roles["case_manager"])
    denied = await costs_client.patch(
        "/agencies/me", headers=agent_headers(cm), json={"currency": "USD"}
    )
    assert denied.status_code == 403


@pytest.mark.parametrize("bad", ["EURO", "eur", "XYZ", "E", "xof"])
async def test_invalid_currency_is_422(
    costs_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, bad: str
) -> None:
    resp = await costs_client.patch(
        "/agencies/me", headers=agent_headers(admin), json={"currency": bad}
    )
    assert resp.status_code == 422


async def test_changing_currency_with_costs_is_409(
    costs_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)  # agency currency = EUR (fixture)
    case_id, pids = await _case_with_steps(
        costs_client, admin.agency_id, make_client_case, headers, 1
    )
    await _add_cost(costs_client, headers, case_id, pids[0], "10", "seed")
    resp = await costs_client.patch("/agencies/me", headers=headers, json={"currency": "USD"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "cost.currency_change_forbidden"


async def test_changing_currency_without_costs_is_200(
    costs_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    # EUR set (fixture), no cost recorded → changing to USD is allowed.
    resp = await costs_client.patch(
        "/agencies/me", headers=agent_headers(admin), json={"currency": "USD"}
    )
    assert resp.status_code == 200


async def test_zero_decimal_currency_rejects_a_decimal_amount(
    costs_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    await _set_currency(db_session, admin.agency_id, "PYG")  # guaraní: 0 decimals
    case_id, pids = await _case_with_steps(
        costs_client, admin.agency_id, make_client_case, headers, 1
    )
    resp = await costs_client.post(
        f"/cases/{case_id}/steps/{pids[0]}/costs",
        headers=headers,
        json={"amount": "120.50", "label": "x"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "cost.amount_decimals"


async def test_three_decimal_currency_accepts_120_505(
    costs_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    await _set_currency(db_session, admin.agency_id, "TND")  # Tunisian dinar: 3 decimals
    case_id, pids = await _case_with_steps(
        costs_client, admin.agency_id, make_client_case, headers, 1
    )
    resp = await costs_client.post(
        f"/cases/{case_id}/steps/{pids[0]}/costs",
        headers=headers,
        json={"amount": "120.505", "label": "x"},
    )
    assert resp.status_code == 201


def test_currency_catalog_excludes_pseudo_currencies_and_is_pinned() -> None:
    from src.core.currencies import list_supported

    codes = {c.code for c in list_supported()}
    # Metals / test / accounting units are out (all carry exponent=None)...
    assert {"XAU", "XAG", "XPT", "XPD", "XTS", "XXX", "XDR", "XUA"}.isdisjoint(codes)
    # ...real X* currencies are kept.
    assert {"XOF", "XAF", "XCD", "XPF"} <= codes
    # PINNED: if the iso4217 library changes the set, this test says so.
    assert len(codes) == 165


async def test_currencies_endpoint_is_agent_only(
    costs_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    ok = await costs_client.get("/currencies", headers=agent_headers(admin))
    assert ok.status_code == 200
    body = ok.json()
    codes = {c["code"] for c in body}
    assert "XAU" not in codes and "XTS" not in codes and "XXX" not in codes
    assert "XOF" in codes
    assert all({"code", "name", "decimals"} <= set(c) for c in body)
    # No anonymous access (agent face).
    assert (await costs_client.get("/currencies")).status_code in (401, 403)


async def test_currency_is_readable_after_it_is_written(
    costs_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """A written field must be RE-READABLE: PATCH currency → GET /agencies/me
    reflects it; a fresh agency reads null (so Settings can detect 'not set')."""
    # A FRESH agency (no currency yet) reads null.
    fresh = await make_agent(role=system_roles["admin"])
    me_fresh = (await costs_client.get("/agencies/me", headers=agent_headers(fresh))).json()
    assert me_fresh["currency"] is None
    # PATCH sets it; GET reads exactly it back. (The spec said "BGN", but the
    # iso4217 2026 edition retired BGN — Bulgaria adopted the euro on
    # 2026-01-01 — so a still-active code proves the read/write round-trip.)
    patched = await costs_client.patch(
        "/agencies/me", headers=agent_headers(admin), json={"currency": "USD"}
    )
    assert patched.status_code == 200, patched.text
    me = (await costs_client.get("/agencies/me", headers=agent_headers(admin))).json()
    assert me["currency"] == "USD"
