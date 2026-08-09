"""Lot quote (08/08) — POST /billing/seats/quote, the composition dry-run.

The basket (panier d'invitations) never computes a price front-side; this
endpoint serves the whole arithmetic, READ-ONLY, from the DECLARED catalog:

(a) the exact Nicolas case: +2 managers (1 covered by the included tier)
    and +5 readers (3 on free pool seats, 2 to buy) → 60.98, with the
    annual line (49.15, −19 %) — rotated reader grid 09/08;
(b) nothing free: every requested seat is priced, and the gesture proves
    ZERO Paddle traffic and ZERO writes;
(c) trial: the SAME named 409 as add/remove (tranché 08/08 — a quote
    prices a billable composition, the trial has nothing to bill);
(d) annual cycle: annual rates served, NO annual_equivalent line;
(e) a fully absorbed composition quotes 0.00 with no annual line (no
    discount over zero);
(f) contract guards: empty composition → named 422, sur_mesure → named
    409, agency.manage gate (member → 403; the reader sweep has its row).
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from shared.models.usage import UsageEvent
from src.billing import paddle_client
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"])


async def _convert(
    client: AsyncClient,
    superadmin: Agent,
    headers: AuthHeaders,
    agency_id: uuid.UUID,
    *,
    plan: str = "cabinet",
    cycle: str = "mensuel",
) -> None:
    response = await client.patch(
        f"/agencies/{agency_id}/subscription",
        headers=headers(superadmin),
        json={"plan": plan, "billing_cycle": cycle},
    )
    assert response.status_code == 200, response.text


async def _set_pool(db: AsyncSession, agency_id: uuid.UUID, pool: int) -> None:
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    agency.reader_seats_purchased = pool
    await db.commit()


def _quote(client: AsyncClient, headers: dict[str, str], body: dict):
    return client.post("/billing/seats/quote", headers=headers, json=body)


# --- (a) the Nicolas case, exact -------------------------------------------------------


async def test_nicolas_case_exact_figures(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    """Cabinet mensuel, 2 managers already (1 included seat left), pool of
    3 free reader seats: quoting +2 managers +5 readers, to the cent —
    including the annual line (rates /12, rounded once) — at the ROTATED
    reader grid (09/08: 12.99 mensuel / 119.88 annuel)."""
    await _convert(client, superadmin, agent_headers, admin.agency_id)
    await make_agent(
        role=system_roles["member"], agency_id=admin.agency_id, email="second@example.com"
    )
    await _set_pool(db_session, admin.agency_id, 3)

    quoted = await _quote(client, agent_headers(admin), {"manager": 2, "reader": 5})
    assert quoted.status_code == 200, quoted.text
    assert quoted.json() == {
        "currency": "EUR",
        "billing_cycle": "mensuel",
        "manager": {
            "requested": 2,
            "from_included": 1,
            "to_bill": 1,
            "unit_price": "35.00",
            "recurring_add": "35.00",
        },
        "reader": {
            "requested": 5,
            "from_free": 3,
            "to_buy": 2,
            "unit_price": "12.99",
            "recurring_add": "25.98",
        },
        "total_recurring_add": "60.98",
        # annual: (35000 + 2 × 11988) / 12 = 4914.67c → 49.15 ; discount
        # (60.98 − 49.15) / 60.98 → 19 % ; saved_per_year: exact cents
        # (60.98 × 12 = 731.76) − 589.76 = 142.00 — no rounding drift.
        "annual_equivalent": {
            "total_recurring_add": "49.15",
            "discount_percent": 19,
            "saved_per_year": "142.00",
        },
    }


# --- (b) nothing free: everything priced, zero traffic, zero writes --------------------


async def test_quote_without_free_seats_prices_all_and_touches_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recap when nothing is already paid (panier-02): every requested
    reader is to_buy. On a LIVE paddle subscription, the quote still makes
    ZERO Paddle calls and writes NOTHING (no pool change, no usage event)."""
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.plan = "cabinet"
    agency.billing_cycle = "mensuel"
    agency.converted_at = datetime.now(UTC)
    agency.billing_mode = "paddle"
    agency.billing_status = "active"
    agency.paddle_subscription_id = "sub_quote_dry"
    await db_session.commit()
    for i in range(2):  # 3 managers total: the included tier is FULL
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    push = AsyncMock(return_value={})
    txn = AsyncMock(return_value={"id": "txn_never"})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", txn)

    quoted = await _quote(client, agent_headers(admin), {"reader": 2})
    assert quoted.status_code == 200, quoted.text
    body = quoted.json()
    assert body["manager"] == {
        "requested": 0,
        "from_included": 0,
        "to_bill": 0,
        "unit_price": "35.00",
        "recurring_add": "0.00",
    }
    assert body["reader"] == {
        "requested": 2,
        "from_free": 0,
        "to_buy": 2,
        "unit_price": "12.99",
        "recurring_add": "25.98",
    }
    assert body["total_recurring_add"] == "25.98"
    # 2 × 119.88 / 12 = 19.98 ; (25.98 − 19.98) / 25.98 → 23 % ;
    # saved_per_year = 25.98 × 12 − 239.76 = 72.00 (exact cents).
    assert body["annual_equivalent"] == {
        "total_recurring_add": "19.98",
        "discount_percent": 23,
        "saved_per_year": "72.00",
    }

    assert push.await_count == 0 and txn.await_count == 0  # dry-run, by contract
    await db_session.refresh(agency)
    assert agency.reader_seats_purchased == 0  # nothing bought
    events = (
        await db_session.execute(
            select(func.count(UsageEvent.id)).where(UsageEvent.agency_id == admin.agency_id)
        )
    ).scalar_one()
    assert events == 0  # nothing traced either: a quote is not a gesture


# --- (c) trial: the named 409, tranché ------------------------------------------------


async def test_trial_quote_is_the_same_named_409_as_the_pool_gestures(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Tranché 08/08: in trial the quote answers 409
    `billing.seats_require_subscription` — the SAME code as seats/add and
    seats/remove (one rule for the three billing gestures). A quote prices
    a billable composition; the trial has nothing to bill — the front
    shows « Inclus pendant l'essai. » without calling this endpoint."""
    refused = await _quote(client, agent_headers(admin), {"manager": 1, "reader": 1})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "billing.seats_require_subscription"


# --- (d) annual cycle: annual rates, no equivalent line --------------------------------


async def test_annual_cycle_serves_annual_rates_without_equivalent_line(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """On an ANNUAL cycle the recurring amounts ARE the annual rates
    (350.00 / 119.88) and the annual_equivalent line does not exist —
    there is nothing to upsell."""
    await _convert(client, superadmin, agent_headers, admin.agency_id, cycle="annuel")

    quoted = await _quote(client, agent_headers(admin), {"manager": 3, "reader": 1})
    assert quoted.status_code == 200, quoted.text
    body = quoted.json()
    assert body["billing_cycle"] == "annuel"
    assert body["manager"] == {
        "requested": 3,
        "from_included": 2,  # 1 manager on 3 included: 2 seats of headroom
        "to_bill": 1,
        "unit_price": "350.00",
        "recurring_add": "350.00",
    }
    assert body["reader"] == {
        "requested": 1,
        "from_free": 0,
        "to_buy": 1,
        "unit_price": "119.88",
        "recurring_add": "119.88",
    }
    assert body["total_recurring_add"] == "469.88"
    assert body["annual_equivalent"] is None


async def test_seven_readers_quote_at_the_rotated_grid(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """The rotation's named case (09/08): +7 readers on a monthly cycle →
    90.93 €/mois, 69.93 €/mois in annual equivalent (7 × 119.88 / 12,
    EXACT), −23 % — the percent is COMPUTED, never a hardcoded string."""
    await _convert(client, superadmin, agent_headers, admin.agency_id)

    quoted = await _quote(client, agent_headers(admin), {"reader": 7})
    assert quoted.status_code == 200, quoted.text
    body = quoted.json()
    assert body["reader"]["to_buy"] == 7
    assert body["reader"]["unit_price"] == "12.99"
    assert body["total_recurring_add"] == "90.93"
    # 7 × 11988 / 12 = 6993c exact ; (90.93 − 69.93) / 90.93 → 23 % ;
    # saved = 90.93 × 12 − 839.16 = 252.00.
    assert body["annual_equivalent"] == {
        "total_recurring_add": "69.93",
        "discount_percent": 23,
        "saved_per_year": "252.00",
    }


# --- (e) fully absorbed: 0.00, no annual line ------------------------------------------


async def test_fully_absorbed_composition_quotes_zero_without_annual_line(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """A composition fitting entirely in the included tier + free pool
    seats costs 0.00 — served as a real quote (the front says « aucun coût
    aujourd'hui »), with NO annual_equivalent (no discount over zero)."""
    await _convert(client, superadmin, agent_headers, admin.agency_id)
    await _set_pool(db_session, admin.agency_id, 2)

    quoted = await _quote(client, agent_headers(admin), {"manager": 1, "reader": 2})
    assert quoted.status_code == 200, quoted.text
    body = quoted.json()
    assert body["manager"]["to_bill"] == 0 and body["reader"]["to_buy"] == 0
    assert body["total_recurring_add"] == "0.00"
    assert body["annual_equivalent"] is None


# --- (f) contract guards ---------------------------------------------------------------


async def test_quote_contract_guards(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    headers = agent_headers(admin)
    await _convert(client, superadmin, agent_headers, admin.agency_id)

    # Empty composition: the named 422 (before any state read).
    empty = await _quote(client, headers, {})
    assert empty.status_code == 422, empty.text
    assert empty.json()["code"] == "billing.quote_composition_required"

    # Zero quantities die at the schema (ge=1), like add/remove.
    zero = await _quote(client, headers, {"reader": 0})
    assert zero.status_code == 422, zero.text

    # agency.manage gate: a member is refused by the matrix (the reader
    # sweep in test_reader_gating carries the viewer row).
    member = await make_agent(
        role=system_roles["member"], agency_id=admin.agency_id, email="member@example.com"
    )
    forbidden = await _quote(client, agent_headers(member), {"reader": 1})
    assert forbidden.status_code == 403, forbidden.text


async def test_sur_mesure_has_no_grid_to_quote_against(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """sur_mesure is a hand-written devis: no declared seat grid → named
    409, never a 500 on a missing catalog key."""
    await _convert(client, superadmin, agent_headers, admin.agency_id, plan="sur_mesure")

    refused = await _quote(client, agent_headers(admin), {"manager": 1})
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["code"] == "billing.quote_unavailable"
    assert body["params"] == {"plan": "sur_mesure"}
