"""Planned costs on journey template steps (Reside). A planned cost lives on the
TEMPLATE; at instantiation it becomes a REAL case_step_cost line carrying planned
+ real side by side, with a dead trace to its origin. The tests ARE the safety:
no propagation (editing/deleting a planned cost never touches a live dossier),
the three totals exact, no client/provider ever sees a planned cost (comprehension
sweep), sample clones carry none, and the same currency/permission rules as real
costs."""

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from shared.models.journey_step_cost import JourneyStepCost
from shared.models.rbac import Role
from src.core.rbac.permissions import Permission
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

# A planned amount/label that must NEVER surface on a client/provider face.
SENTINEL = "PREVUSECRETXYZ"
SENTINEL_AMOUNT = "424242.00"


@pytest.fixture
def pc_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


async def _set_currency(db: AsyncSession, agency_id: uuid.UUID, code: str | None) -> None:
    await db.execute(update(Agency).where(Agency.id == agency_id).values(currency=code))
    await db.commit()


@pytest_asyncio.fixture
async def admin(
    make_agent: MakeAgent, system_roles: dict[str, Role], db_session: AsyncSession
) -> Agent:
    # admin holds cost.view AND cost.manage AND journey.configure.
    agent = await make_agent(role=system_roles["admin"])
    await _set_currency(db_session, agent.agency_id, "EUR")
    return agent


# --- helpers -------------------------------------------------------------------------


async def _template(client: AsyncClient, headers: dict, name: str = "T") -> str:
    return (await client.post("/journeys", headers=headers, json={"name": name})).json()["id"]


async def _add_step(client: AsyncClient, headers: dict, tid: str, name: str) -> str:
    r = await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _add_planned(
    client: AsyncClient, headers: dict, tid: str, sid: str, amount: str, label: str
) -> dict:
    r = await client.post(
        f"/journeys/{tid}/steps/{sid}/planned-costs",
        headers=headers,
        json={"amount": amount, "label": label},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _assign(
    client: AsyncClient,
    headers: dict,
    make_client_case: MakeClientCase,
    agency_id: uuid.UUID,
    tid: str,
    principal_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, list[str]]:
    """Assign the template to a fresh case; return (case_id, progress_ids). The
    assign response IS the instantiated timeline (each step id = a progress id)."""
    kwargs: dict = {"agency_id": agency_id}
    if principal_id is not None:
        kwargs["principal_expat_user_id"] = principal_id
    case = await make_client_case(**kwargs)
    r = await client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
    )
    assert r.status_code == 201, r.text
    return case.id, [s["id"] for s in r.json()]


async def _costs(client: AsyncClient, headers: dict, case_id: uuid.UUID) -> dict:
    r = await client.get(f"/cases/{case_id}/costs", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# --- 1. instantiation produces a line with planned_amount set and amount empty -------


async def test_planned_cost_instantiates_a_line_planned_set_amount_empty(
    pc_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    tid = await _template(pc_client, h)
    sid = await _add_step(pc_client, h, tid, "Step")
    await _add_planned(pc_client, h, tid, sid, "120.00", "Timbre fiscal")

    case_id, _pids = await _assign(pc_client, h, make_client_case, admin.agency_id, tid)
    body = await _costs(pc_client, h, case_id)

    assert len(body["lines"]) == 1
    line = body["lines"][0]
    assert Decimal(line["planned_amount"]) == Decimal("120")  # frozen from the template
    assert line["amount"] is None  # real is EMPTY until the agency pays
    assert line["label"] == "Timbre fiscal"
    assert line["source_template_cost_id"] is not None  # a trace to the origin
    # planned_total counts it; real_total ignores an unpaid line; no variance yet.
    assert Decimal(body["planned_total"]) == Decimal("120")
    assert Decimal(body["real_total"]) == 0
    assert Decimal(body["variance"]) == 0


# --- 2. the agency enters the real: the variance appears -----------------------------


async def test_entering_the_real_amount_surfaces_the_variance(
    pc_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    tid = await _template(pc_client, h)
    sid = await _add_step(pc_client, h, tid, "Step")
    await _add_planned(pc_client, h, tid, sid, "100.00", "Droit")
    case_id, _pids = await _assign(pc_client, h, make_client_case, admin.agency_id, tid)

    cost_id = (await _costs(pc_client, h, case_id))["lines"][0]["id"]
    patched = await pc_client.patch(
        f"/cases/{case_id}/costs/{cost_id}", headers=h, json={"amount": "130.00"}
    )
    assert patched.status_code == 200, patched.text

    body = await _costs(pc_client, h, case_id)
    assert Decimal(body["planned_total"]) == Decimal("100")
    assert Decimal(body["real_total"]) == Decimal("130")
    assert Decimal(body["variance"]) == Decimal("30")  # 130 − 100, the line has both


# --- 3. NO propagation: editing a planned cost touches no existing case --------------


async def test_editing_a_planned_cost_does_not_touch_an_existing_case(
    pc_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    tid = await _template(pc_client, h)
    sid = await _add_step(pc_client, h, tid, "Step")
    pc_id = (await _add_planned(pc_client, h, tid, sid, "100.00", "Droit"))["id"]
    case_id, _pids = await _assign(pc_client, h, make_client_case, admin.agency_id, tid)

    # Change the TEMPLATE planned cost AFTER the case exists.
    edit = await pc_client.patch(
        f"/journeys/{tid}/planned-costs/{pc_id}", headers=h, json={"amount": "999.00"}
    )
    assert edit.status_code == 200, edit.text

    # The dossier line is UNMOVED: planned_amount is a frozen copy, not a lookup.
    line = (await _costs(pc_client, h, case_id))["lines"][0]
    assert Decimal(line["planned_amount"]) == Decimal("100")


# --- 4. deleting a planned cost destroys no case line; planned_amount survives -------


async def test_deleting_a_planned_cost_leaves_case_lines_intact(
    pc_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    tid = await _template(pc_client, h)
    sid = await _add_step(pc_client, h, tid, "Step")
    pc_id = (await _add_planned(pc_client, h, tid, sid, "100.00", "Droit"))["id"]
    case_id, _pids = await _assign(pc_client, h, make_client_case, admin.agency_id, tid)

    deleted = await pc_client.delete(f"/journeys/{tid}/planned-costs/{pc_id}", headers=h)
    assert deleted.status_code == 200, deleted.text

    # The line SURVIVES with its frozen planned_amount; only the trace is cut.
    body = await _costs(pc_client, h, case_id)
    assert len(body["lines"]) == 1
    line = body["lines"][0]
    assert Decimal(line["planned_amount"]) == Decimal("100")
    assert line["source_template_cost_id"] is None  # SET NULL on template delete
    assert Decimal(body["planned_total"]) == Decimal("100")


# --- 5. a manual line has planned_amount NULL; planned_total ignores it --------------


async def test_manual_line_has_no_plan_and_is_ignored_by_planned_total(
    pc_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    tid = await _template(pc_client, h)
    await _add_step(pc_client, h, tid, "Step")  # NO planned cost on the template
    case_id, pids = await _assign(pc_client, h, make_client_case, admin.agency_id, tid)
    assert (await _costs(pc_client, h, case_id))["lines"] == []  # no lines yet

    # A débours nobody forecast — added by hand on the case.
    r = await pc_client.post(
        f"/cases/{case_id}/steps/{pids[0]}/costs",
        headers=h,
        json={"amount": "40.00", "label": "Imprévu"},
    )
    assert r.status_code == 201, r.text

    body = await _costs(pc_client, h, case_id)
    line = body["lines"][0]
    assert line["planned_amount"] is None  # no plan, no origin
    assert line["source_template_cost_id"] is None
    assert Decimal(line["amount"]) == Decimal("40")
    assert Decimal(body["planned_total"]) == 0  # ignored by the planned total
    assert Decimal(body["real_total"]) == Decimal("40")


# --- 6. the three totals exact: 3 steps, 6 lines, 2 unplanned, 1 unpaid --------------


async def test_three_totals_exact_on_mixed_case(
    pc_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Planned A/B/C/D (D left unpaid), manual E/F. planned_total = 100+200+50+80
    = 430; real_total = 110+190+60+40+25 = 425; variance = (110−100)+(190−200)+
    (60−50) = 10 (only A/B/C have both). Note variance ≠ real−planned (−5): the
    per-both-lines definition, proven."""
    h = agent_headers(admin)
    tid = await _template(pc_client, h)
    s0 = await _add_step(pc_client, h, tid, "S0")
    s1 = await _add_step(pc_client, h, tid, "S1")
    await _add_step(pc_client, h, tid, "S2")
    await _add_planned(pc_client, h, tid, s0, "100.00", "A")
    await _add_planned(pc_client, h, tid, s0, "200.00", "B")
    await _add_planned(pc_client, h, tid, s1, "50.00", "C")
    await _add_planned(pc_client, h, tid, s1, "80.00", "D")  # will stay unpaid
    case_id, pids = await _assign(pc_client, h, make_client_case, admin.agency_id, tid)

    lines = {line["label"]: line for line in (await _costs(pc_client, h, case_id))["lines"]}
    for label, real in (("A", "110.00"), ("B", "190.00"), ("C", "60.00")):
        r = await pc_client.patch(
            f"/cases/{case_id}/costs/{lines[label]['id']}", headers=h, json={"amount": real}
        )
        assert r.status_code == 200, r.text
    # D stays unpaid (amount None). Two manual débours (no plan).
    for amount, label in (("40.00", "E"), ("25.00", "F")):
        r = await pc_client.post(
            f"/cases/{case_id}/steps/{pids[0]}/costs",
            headers=h,
            json={"amount": amount, "label": label},
        )
        assert r.status_code == 201, r.text

    body = await _costs(pc_client, h, case_id)
    assert len(body["lines"]) == 6
    assert Decimal(body["planned_total"]) == Decimal("430")
    assert Decimal(body["real_total"]) == Decimal("425")
    assert Decimal(body["variance"]) == Decimal("10")


# --- INVARIANT: Σ des variances de ligne (non nulles) == variance du total ----------


async def test_per_line_variance_sums_to_the_total_variance(
    pc_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The contract invariant: the sum of the lines' non-null variances equals
    the total's variance. One rule (line_variance) behind both views, so they
    can never diverge. Also proves the per-line écart is SIGNED (a real below
    plan → negative) and null when a line lacks one of the two amounts."""
    h = agent_headers(admin)
    tid = await _template(pc_client, h)
    s0 = await _add_step(pc_client, h, tid, "S0")
    await _add_planned(pc_client, h, tid, s0, "100.00", "P1")
    await _add_planned(pc_client, h, tid, s0, "200.00", "P2")  # stays unpaid
    case_id, pids = await _assign(pc_client, h, make_client_case, admin.agency_id, tid)

    lines = {line["label"]: line for line in (await _costs(pc_client, h, case_id))["lines"]}
    # Pay P1 BELOW its plan → a signed, negative écart. P2 stays unpaid (null).
    r = await pc_client.patch(
        f"/cases/{case_id}/costs/{lines['P1']['id']}", headers=h, json={"amount": "90.00"}
    )
    assert r.status_code == 200, r.text
    # An unplanned manual débours (no plan → null écart).
    r = await pc_client.post(
        f"/cases/{case_id}/steps/{pids[0]}/costs", headers=h, json={"amount": "50.00", "label": "M"}
    )
    assert r.status_code == 201, r.text

    body = await _costs(pc_client, h, case_id)
    by = {line["label"]: line for line in body["lines"]}
    assert isinstance(by["P1"]["variance"], str)
    assert Decimal(by["P1"]["variance"]) == Decimal("-10")  # signed: real below plan
    assert by["P2"]["variance"] is None  # unpaid line → no écart
    assert by["M"]["variance"] is None  # unplanned débours → no écart

    # THE invariant — the two views of the same number are identical.
    line_sum = sum(
        (Decimal(line["variance"]) for line in body["lines"] if line["variance"] is not None),
        Decimal(0),
    )
    assert line_sum == Decimal(body["variance"]) == Decimal("-10")


# --- 7. an expat never sees a planned cost, on ANY route (comprehension) -------------


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


async def test_expat_never_sees_a_planned_cost_on_any_route(
    pc_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    expat = await make_expat_user(email="client@x.io")
    tid = await _template(pc_client, h)
    sid = await _add_step(pc_client, h, tid, "Step")
    await _add_planned(pc_client, h, tid, sid, SENTINEL_AMOUNT, SENTINEL)
    case_id, _pids = await _assign(
        pc_client, h, make_client_case, admin.agency_id, tid, principal_id=expat.id
    )

    eh = expat_headers(expat)
    routes = _routes_with_prefix("/expat/")
    assert len(routes) >= 4, routes
    for path in routes:
        url = _fill_path(path, case_id)
        resp = await pc_client.get(url, headers=eh)
        # Neither the planned amount nor its label ever reaches the client — the
        # expat face reads case_step_progress, never a cost table.
        assert SENTINEL not in resp.text and SENTINEL_AMOUNT not in resp.text, (path, resp.text)


# --- 8. a provider never sees a planned cost either ---------------------------------


async def test_external_never_sees_a_planned_cost_on_any_route(
    pc_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    expat = await make_expat_user(email="c@x.io")
    tid = await _template(pc_client, h)
    sid = await _add_step(pc_client, h, tid, "Step")
    await _add_planned(pc_client, h, tid, sid, SENTINEL_AMOUNT, SENTINEL)
    case_id, _pids = await _assign(
        pc_client, h, make_client_case, admin.agency_id, tid, principal_id=expat.id
    )

    ext_role = (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()
    external = await make_agent(agency_id=admin.agency_id, role=ext_role, is_external=True)
    await pc_client.post(
        f"/cases/{case_id}/external-assignments", headers=h, json={"agent_id": str(external.id)}
    )
    eh = agent_headers(external)
    for path in _routes_with_prefix("/external/"):
        url = _fill_path(path, case_id)
        resp = await pc_client.get(url, headers=eh)
        assert SENTINEL not in resp.text and SENTINEL_AMOUNT not in resp.text, (path, resp.text)


# --- 9. cloning: a sample carries none; an agency journey carries its own ------------


async def test_cloning_a_sample_creates_no_planned_cost(
    pc_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """A library sample is unreachable for cost writes, so it never HAS a planned
    cost. We insert one directly anyway (a hypothetical) and prove the clone
    guard drops it: a shared model never seeds an agency's costs."""
    sample = JourneyTemplate(id=uuid.uuid4(), agency_id=None, is_sample=True, name="Sample")
    db_session.add(sample)
    step = JourneyTemplateStep(id=uuid.uuid4(), template_id=sample.id, name="S", position=0)
    db_session.add(step)
    db_session.add(JourneyStepCost(step_id=step.id, amount=Decimal("77.00"), label="LEAK"))
    await db_session.commit()

    h = agent_headers(admin)
    clone = await pc_client.post(f"/journeys/{sample.id}/clone", headers=h)
    assert clone.status_code == 201, clone.text
    detail = (await pc_client.get(f"/journeys/{clone.json()['id']}", headers=h)).json()
    assert all(s["planned_costs"] == [] for s in detail["steps"])  # the guard held


async def test_cloning_an_agency_journey_carries_its_planned_costs(
    pc_client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    tid = await _template(pc_client, h, name="Own")
    sid = await _add_step(pc_client, h, tid, "Step")
    await _add_planned(pc_client, h, tid, sid, "120.00", "Timbre")

    clone = await pc_client.post(f"/journeys/{tid}/clone", headers=h)
    assert clone.status_code == 201, clone.text
    detail = (await pc_client.get(f"/journeys/{clone.json()['id']}", headers=h)).json()
    planned = [pc for s in detail["steps"] for pc in s["planned_costs"]]
    assert len(planned) == 1
    assert planned[0]["label"] == "Timbre"
    assert Decimal(planned[0]["amount"]) == Decimal("120")


# --- 10. no agency currency → 409 at planned cost entry -----------------------------


async def test_planned_cost_requires_an_agency_currency(
    pc_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    fresh = await make_agent(role=system_roles["admin"])  # a FRESH agency: no currency
    h = agent_headers(fresh)
    tid = await _template(pc_client, h)
    sid = await _add_step(pc_client, h, tid, "Step")
    resp = await pc_client.post(
        f"/journeys/{tid}/steps/{sid}/planned-costs",
        headers=h,
        json={"amount": "10.00", "label": "x"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "cost.currency_required"  # same rule, same code


# --- 11. without cost.view, journey edit returns no planned cost ---------------------


async def test_journey_edit_without_cost_view_hides_the_planned_section(
    pc_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    tid = await _template(pc_client, h)
    sid = await _add_step(pc_client, h, tid, "Step")
    await _add_planned(pc_client, h, tid, sid, "120.00", "Timbre")
    # The owner (cost.view) sees the section.
    owner_detail = (await pc_client.get(f"/journeys/{tid}", headers=h)).json()
    assert len(owner_detail["steps"][0]["planned_costs"]) == 1

    # A colleague who edits journeys but has NO cost.view: same route, empty section.
    blind_role = await make_role(
        permissions=[Permission.JOURNEY_CONFIGURE], agency_id=admin.agency_id
    )
    blind = await make_agent(agency_id=admin.agency_id, role=blind_role)
    blind_detail = (await pc_client.get(f"/journeys/{tid}", headers=agent_headers(blind))).json()
    assert blind_detail["steps"][0]["planned_costs"] == []  # never learns it exists
    # And the raw payload does not leak the amount anywhere.
    assert "120" not in str(blind_detail["steps"][0]["planned_costs"])


# --- structural comprehension: neither client schema mentions a cost ----------------


def test_client_schemas_never_reference_cost_or_planned() -> None:
    from pathlib import Path

    for path in ("src/expat/expat_schema.py", "src/external/external_schema.py"):
        src = Path(path).read_text(encoding="utf-8").lower()
        assert "cost" not in src and "planned" not in src, path
