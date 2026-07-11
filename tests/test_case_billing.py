"""Billed price on a dossier (Reside: "know what is left at the end"). ONE
price per case on client_case (the costs are the detail); written under
cost.manage, read under cost.view — and WITHOUT cost.view the `billing` block
is ABSENT from the agent detail payload (no key, no null hint). The margin is
SERVED (billed − real costs), only when every real cost shares the price's
currency; the expat/external blindness is proven by the extended comprehension
sweeps in test_costs.py."""

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.core.rbac.permissions import Permission
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest.fixture
def billing_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


async def _set_currency(db: AsyncSession, agency_id: uuid.UUID, code: str | None) -> None:
    await db.execute(update(Agency).where(Agency.id == agency_id).values(currency=code))
    await db.commit()


@pytest_asyncio.fixture
async def admin(
    make_agent: MakeAgent, system_roles: dict[str, Role], db_session: AsyncSession
) -> Agent:
    agent = await make_agent(role=system_roles["admin"])
    await _set_currency(db_session, agent.agency_id, "EUR")
    return agent


async def _create_case(client: AsyncClient, headers: dict, **extra: object) -> dict:
    payload: dict = {
        "first_name": "Jean",
        "last_name": "Martin",
        "email": f"client-{uuid.uuid4().hex[:8]}@x.io",
        **extra,
    }
    r = await client.post("/cases", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def _detail(client: AsyncClient, headers: dict, case_id: str) -> dict:
    r = await client.get(f"/cases/{case_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _case_with_step(
    client: AsyncClient,
    agency_id: uuid.UUID,
    make_client_case: MakeClientCase,
    headers: dict,
) -> tuple[str, str]:
    """A case with one instantiated step (to hang real cost lines on)."""
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "Step"})
    case = await make_client_case(agency_id=agency_id)
    timeline = (
        await client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    return str(case.id), timeline[0]["id"]


# --- creation with a price -----------------------------------------------------------


async def test_create_case_with_billed_price(
    billing_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    h = agent_headers(admin)
    created = await _create_case(billing_client, h, billed_amount="1500.00")
    # The create/PATCH responses (CaseResponse) never carry the price — the
    # cost.view-gated read is the detail's `billing` block.
    assert "billed_amount" not in created and "billing" not in created

    billing = (await _detail(billing_client, h, created["id"]))["billing"]
    assert isinstance(billing["billed_amount"], str)  # money as a STRING
    assert Decimal(billing["billed_amount"]) == Decimal("1500")
    assert billing["billed_currency"] == "EUR"  # agency default (prefill rule)
    # No real cost yet → the margin is the full price, same currency.
    assert Decimal(billing["margin"]) == Decimal("1500")
    assert billing["margin_unavailable_reason"] is None


# --- edition: set, re-denominate, clear ------------------------------------------------


async def test_edit_billed_price(
    billing_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    h = agent_headers(admin)
    case_id = (await _create_case(billing_client, h))["id"]

    r = await billing_client.patch(
        f"/cases/{case_id}", headers=h, json={"billed_amount": "2000.00", "billed_currency": "USD"}
    )
    assert r.status_code == 200, r.text
    billing = (await _detail(billing_client, h, case_id))["billing"]
    assert (Decimal(billing["billed_amount"]), billing["billed_currency"]) == (
        Decimal("2000"),
        "USD",
    )

    # Re-denominate ALONE: the amount survives, only the currency moves.
    r = await billing_client.patch(f"/cases/{case_id}", headers=h, json={"billed_currency": "EUR"})
    assert r.status_code == 200, r.text
    billing = (await _detail(billing_client, h, case_id))["billing"]
    assert (Decimal(billing["billed_amount"]), billing["billed_currency"]) == (
        Decimal("2000"),
        "EUR",
    )

    # Clear: billed_amount=null wipes the price entirely (both fields).
    r = await billing_client.patch(f"/cases/{case_id}", headers=h, json={"billed_amount": None})
    assert r.status_code == 200, r.text
    billing = (await _detail(billing_client, h, case_id))["billing"]
    assert billing == {
        "billed_amount": None,
        "billed_currency": None,
        "margin": None,
        "margin_unavailable_reason": None,
    }

    # A currency without an amount is half a price → refused.
    r = await billing_client.patch(f"/cases/{case_id}", headers=h, json={"billed_currency": "EUR"})
    assert r.status_code == 422
    assert r.json()["code"] == "case.billed_currency_without_amount"


# --- without cost.view: ABSENT from the payload, and the write is 403 -----------------


async def test_without_cost_view_billing_is_absent_and_write_forbidden(
    billing_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    case_id = (await _create_case(billing_client, h, billed_amount="1500.00"))["id"]

    # case.view + case.edit but NO cost permission: reads the dossier
    # normally — the billing KEY does not exist in the payload (not null).
    blind_role = await make_role(
        permissions=[Permission.CASE_VIEW, Permission.CASE_EDIT], agency_id=admin.agency_id
    )
    blind = await make_agent(agency_id=admin.agency_id, role=blind_role)
    bh = agent_headers(blind)
    detail = await billing_client.get(f"/cases/{case_id}", headers=bh)
    assert detail.status_code == 200, detail.text
    assert "billing" not in detail.json()
    assert "billed" not in detail.text and "margin" not in detail.text

    # And the write path: case.edit without cost.manage → 403 on the price
    # (while a normal edit still works).
    ok = await billing_client.patch(f"/cases/{case_id}", headers=bh, json={"source": "salon"})
    assert ok.status_code == 200, ok.text
    denied = await billing_client.patch(
        f"/cases/{case_id}", headers=bh, json={"billed_amount": "9.00"}
    )
    assert denied.status_code == 403


# --- margin: exact in mono-currency, null with the reason in multi-currency ----------


async def test_margin_exact_single_currency(
    billing_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    case_id, pid = await _case_with_step(billing_client, admin.agency_id, make_client_case, h)
    r = await billing_client.patch(
        f"/cases/{case_id}", headers=h, json={"billed_amount": "2000.00"}
    )
    assert r.status_code == 200, r.text
    for amount in ("300.50", "199.50"):
        r = await billing_client.post(
            f"/cases/{case_id}/steps/{pid}/costs", headers=h, json={"amount": amount, "label": "c"}
        )
        assert r.status_code == 201, r.text

    # Both surfaces serve the SAME margin (one rule: case_margin).
    billing = (await _detail(billing_client, h, case_id))["billing"]
    assert Decimal(billing["margin"]) == Decimal("1500")
    costs = (await billing_client.get(f"/cases/{case_id}/costs", headers=h)).json()
    assert Decimal(costs["margin"]) == Decimal("1500")
    assert costs["billed_amount"] == billing["billed_amount"]
    assert costs["margin_unavailable_reason"] is None


async def test_margin_null_with_reason_when_currencies_mixed(
    billing_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    case_id, pid = await _case_with_step(billing_client, admin.agency_id, make_client_case, h)
    r = await billing_client.patch(
        f"/cases/{case_id}", headers=h, json={"billed_amount": "2000.00", "billed_currency": "EUR"}
    )
    assert r.status_code == 200, r.text
    # One real cost paid in ANOTHER currency → no margin, and the reason says why.
    r = await billing_client.post(
        f"/cases/{case_id}/steps/{pid}/costs",
        headers=h,
        json={"amount": "900000", "label": "c", "currency": "PYG"},
    )
    assert r.status_code == 201, r.text

    billing = (await _detail(billing_client, h, case_id))["billing"]
    assert billing["margin"] is None
    assert billing["margin_unavailable_reason"] == "mixed_currencies"
    costs = (await billing_client.get(f"/cases/{case_id}/costs", headers=h)).json()
    assert costs["margin"] is None
    assert costs["margin_unavailable_reason"] == "mixed_currencies"


# --- currency discipline: invalid code, per-currency decimals, no currency at all ----


async def test_invalid_billed_currency_is_422(
    billing_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    h = agent_headers(admin)
    case_id = (await _create_case(billing_client, h))["id"]
    for bad in ("EURO", "eur", "XYZ"):
        r = await billing_client.patch(
            f"/cases/{case_id}", headers=h, json={"billed_amount": "10.00", "billed_currency": bad}
        )
        assert r.status_code == 422, (bad, r.text)


async def test_billed_decimals_follow_the_billed_currency(
    billing_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    h = agent_headers(admin)
    case_id = (await _create_case(billing_client, h))["id"]
    # Guaraní: 0 decimals → 120.50 refused; euro accepts it — same rule
    # (check_amount_decimals) as the cost lines, reused.
    r = await billing_client.patch(
        f"/cases/{case_id}", headers=h, json={"billed_amount": "120.50", "billed_currency": "PYG"}
    )
    assert r.status_code == 422 and r.json()["code"] == "cost.amount_decimals"
    r = await billing_client.patch(
        f"/cases/{case_id}", headers=h, json={"billed_amount": "120.50", "billed_currency": "EUR"}
    )
    assert r.status_code == 200, r.text


async def test_billed_without_any_currency_is_409(
    billing_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    fresh = await make_agent(role=system_roles["admin"])  # agency without a currency
    h = agent_headers(fresh)
    r = await billing_client.post(
        "/cases",
        headers=h,
        json={
            "first_name": "J",
            "last_name": "M",
            "email": "j@x.io",
            "billed_amount": "10.00",
        },
    )
    assert r.status_code == 409
    assert r.json()["code"] == "cost.currency_required"  # same rule, same code
