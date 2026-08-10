"""Lot 09/08 — le plan INDÉPENDANT (49/490, 1 siège gestionnaire inclus,
siège additionnel à 50/500, lecteurs au SKU transverse).

The price is engineered so that Indépendant + 1 manager = Cabinet = 99
exactly — the step up is a PROPOSAL served by the quote, never a wall.

Covers, against the real app:
(a) the quote on an Indépendant agency: 1 extra manager at 50.00, a reader
    at the transverse rate, the annual equivalent — and the PROPOSED
    switch (upgrade_alternative: stay 99.00 vs Cabinet 99.00, 3 included);
(b) transition a (trial → Indépendant): the checkout bills against the
    TARGET plan's included tier — a 3-seat trial pays 2 seats at entry
    (the latent trial-tier bug, fixed and pinned for cabinet too);
(c) transition b (Cabinet → Indépendant, downgrade): the mirror re-derives
    (included 1) — 3 managers cost 49 + 2×50, the sync pushes 2 seats;
(d) transition c (Indépendant → Cabinet, the wanted path): the 50 € seats
    resorb into the 3 included — the seat line leaves the push;
(e) the annual grid (490 base, 500 seat) with the per-type discount.
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.billing import paddle_client
from src.billing.billing_manager import BillingManager
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

PRICE_IDS = {
    "independant_mensuel": "pri_base_ind_m",
    "independant_annuel": "pri_base_ind_a",
    "seat_independant_mensuel": "pri_seat_ind_m",
    "seat_independant_annuel": "pri_seat_ind_a",
    "cabinet_mensuel": "pri_base_cab_m",
    "cabinet_annuel": "pri_base_cab_a",
    "agence_mensuel": "pri_base_age_m",
    "agence_annuel": "pri_base_age_a",
    "seat_cabinet_mensuel": "pri_seat_cab_m",
    "seat_cabinet_annuel": "pri_seat_cab_a",
    "seat_agence_mensuel": "pri_seat_age_m",
    "seat_agence_annuel": "pri_seat_age_a",
    "seat_reader_mensuel": "pri_seat_reader_m",
    "seat_reader_annuel": "pri_seat_reader_a",
}


@pytest.fixture(autouse=True)
def paddle_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_PRICE_IDS", json.dumps(PRICE_IDS))
    monkeypatch.setenv("BILLING_CHECKOUT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
    plan: str = "independant",
    cycle: str = "mensuel",
) -> None:
    response = await client.patch(
        f"/agencies/{agency_id}/subscription",
        headers=headers(superadmin),
        json={"plan": plan, "billing_cycle": cycle},
    )
    assert response.status_code == 200, response.text


def _quote(client: AsyncClient, headers: dict[str, str], body: dict):
    return client.post("/billing/seats/quote", headers=headers, json=body)


# --- (a) the quote + the PROPOSED switch -----------------------------------------------


async def test_independant_quote_serves_the_proposed_switch(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commercial heart: pricing the 2nd manager on Indépendant serves
    BOTH paths — stay = 49 + 50 = 99.00, switch = Cabinet 99.00 with 3
    included. Same price, one more seat: Cabinet becomes the evidence, the
    quote never blocks."""
    await _convert(client, superadmin, agent_headers, admin.agency_id)
    from src.billing.billing_manager import BillingManager as BM

    monkeypatch.setattr(BM, "_fetch_subscription", AsyncMock(side_effect=RuntimeError("manual")))

    quoted = await _quote(client, agent_headers(admin), {"manager": 1})
    assert quoted.status_code == 200, quoted.text
    body = quoted.json()
    assert body["manager"] == {
        "requested": 1,
        "from_included": 0,  # the single included seat is worn by the admin
        "to_bill": 1,
        "unit_price": "50.00",
        "recurring_add": "50.00",
        # (600 − 500) / 600 → 17 % — the per-type REAL discount, computed
        "annual_discount_percent": 17,
    }
    assert body["total_recurring_add"] == "50.00"
    assert body["upgrade_alternative"] == {
        "plan": "cabinet",
        "included_managers": 3,
        "stay_total_recurring": "99.00",  # 49 + 50 : the engineered equality
        "switch_total_recurring": "99.00",  # Cabinet base, 2 managers on 3 included
    }


async def test_independant_quote_with_reader_and_no_switch_below_included(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """A reader alone stays on the transverse SKU (unlimited, no wall) and
    proposes NO switch (the single included manager seat is not exceeded);
    the readers ride identically on both plans anyway."""
    await _convert(client, superadmin, agent_headers, admin.agency_id)

    quoted = await _quote(client, agent_headers(admin), {"reader": 1})
    assert quoted.status_code == 200, quoted.text
    body = quoted.json()
    assert body["reader"]["unit_price"] == "12.99"  # transverse, unchanged
    assert body["reader"]["to_buy"] == 1
    assert body["total_recurring_add"] == "12.99"
    assert body["upgrade_alternative"] is None  # no manager beyond included

    # Mixed: the 2nd manager brings the proposal, readers on BOTH totals.
    mixed = await _quote(client, agent_headers(admin), {"manager": 1, "reader": 1})
    assert mixed.status_code == 200, mixed.text
    body = mixed.json()
    assert body["total_recurring_add"] == "62.99"  # 50.00 + 12.99
    assert body["upgrade_alternative"] == {
        "plan": "cabinet",
        "included_managers": 3,
        "stay_total_recurring": "111.99",  # 49 + 50 + 12.99
        "switch_total_recurring": "111.99",  # 99 + 12.99 — readers cancel out
    }


async def test_independant_annual_grid(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Annual: 490 base (2 months off, the house mechanics), seat 500/an —
    the equality holds annually too (490 + 500 = 990 = Cabinet annuel)."""
    await _convert(client, superadmin, agent_headers, admin.agency_id, cycle="annuel")

    quoted = await _quote(client, agent_headers(admin), {"manager": 1})
    assert quoted.status_code == 200, quoted.text
    body = quoted.json()
    assert body["manager"]["unit_price"] == "500.00"
    assert body["manager"]["annual_discount_percent"] is None  # annual cycle
    assert body["upgrade_alternative"] == {
        "plan": "cabinet",
        "included_managers": 3,
        "stay_total_recurring": "990.00",  # 490 + 500
        "switch_total_recurring": "990.00",  # Cabinet annuel
    }


# --- (b) transition a: trial → Indépendant, billed against the TARGET tier -------------


async def test_trial_checkout_bills_against_the_target_included_tier(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3-manager trial converting to Indépendant PASSES (no wall) and is
    billed 2 seats at entry — the checkout derives against the included
    tier of the plan BEING BOUGHT, not the trial tier of 3 (the latent
    bug this lot closes). Cabinet stays base-only (regression pin)."""
    for i in range(2):
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    create_txn = AsyncMock(return_value={"id": "txn_ind_1"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create_txn)
    headers = agent_headers(admin)

    checkout = await client.post(
        "/billing/checkout",
        headers=headers,
        json={"plan": "independant", "billing_cycle": "mensuel"},
    )
    assert checkout.status_code == 200, checkout.text
    assert create_txn.await_args.kwargs["items"] == [
        {"price_id": "pri_base_ind_m", "quantity": 1},
        {"price_id": "pri_seat_ind_m", "quantity": 2},  # 3 managers − 1 included
    ]

    checkout = await client.post(
        "/billing/checkout", headers=headers, json={"plan": "cabinet", "billing_cycle": "mensuel"}
    )
    assert checkout.status_code == 200, checkout.text
    assert create_txn.await_args.kwargs["items"] == [
        {"price_id": "pri_base_cab_m", "quantity": 1}  # 3 managers on 3 included
    ]


# --- (c)+(d) transitions b and c: the mirror re-derives across plan changes ------------


async def test_plan_changes_rederive_the_seat_mirror(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cabinet → Indépendant (downgrade): 3 managers re-derive against 1
    included — the push carries seat_independant ×2 (49 + 2×50 = 149: the
    screen's comparison stops the client, never a refusal). Indépendant →
    Cabinet (the wanted path): the 50 € seats RESORB into the 3 included —
    the seat line leaves the push entirely."""
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.plan = "cabinet"
    agency.billing_cycle = "mensuel"
    agency.converted_at = datetime.now(UTC)
    agency.billing_mode = "paddle"
    agency.billing_status = "active"
    agency.paddle_subscription_id = "sub_transitions"
    await db_session.commit()
    for i in range(2):  # 3 managers total
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    headers = agent_headers(admin)

    seats = (await client.get("/agencies/me", headers=headers)).json()["subscription"]["seats"]
    assert seats["included"] == 3 and seats["billed"] == 0  # Cabinet: all included

    # DOWNGRADE (b): the plan flips (the Paddle webhook's outcome) — the
    # mirror re-derives against included=1 at the very next sync.
    agency.plan = "independant"
    await db_session.commit()
    seats = (await client.get("/agencies/me", headers=headers)).json()["subscription"]["seats"]
    assert seats["included"] == 1 and seats["billed"] == 2  # 49 + 2×50 = 149
    await BillingManager(db_session).sync_seat_quantity(admin.agency_id, increase=True)
    items = {i["price_id"]: i["quantity"] for i in push.await_args.kwargs["items"]}
    assert items == {"pri_base_ind_m": 1, "pri_seat_ind_m": 2}

    # UPGRADE (c): back to Cabinet — the 50 € seats resorb, the seat line
    # leaves the push (absent item = 0, the mirror's way).
    push.reset_mock()
    agency.plan = "cabinet"
    await db_session.commit()
    await BillingManager(db_session).sync_seat_quantity(admin.agency_id, increase=True)
    items = {i["price_id"]: i["quantity"] for i in push.await_args.kwargs["items"]}
    assert items == {"pri_base_cab_m": 1}  # resorbed: 3 managers on 3 included


# --- la GRILLE complète : six faces, selectable servi (lot 10/08) ----------------------


async def test_plan_change_quote_serves_the_whole_grid(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    """UN appel rend les TROIS plans × DEUX cycles avec la composition
    courante (Cabinet mensuel, 3 gestionnaires) — plus l'équivalent
    mensuel des faces annuelles, pour comparer sans diviser."""
    await _convert(client, superadmin, agent_headers, admin.agency_id, plan="cabinet")
    for i in range(2):  # 3 managers
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"pc{i}@example.com"
        )

    quoted = await client.post("/billing/plan-change/quote", headers=agent_headers(admin))
    assert quoted.status_code == 200, quoted.text
    body = quoted.json()
    assert body["current_plan"] == "cabinet" and body["current_cycle"] == "mensuel"
    assert len(body["options"]) == 6  # jamais six appels côté front

    faces = {(o["plan"], o["billing_cycle"]): o for o in body["options"]}
    # Indépendant: 1 inclus → 2 sièges à 50 (le downgrade coûteux, dit).
    assert faces[("independant", "mensuel")]["total_recurring"] == "149.00"
    assert faces[("independant", "mensuel")]["manager_seats_billed"] == 2
    assert faces[("independant", "mensuel")]["monthly_equivalent"] is None
    assert faces[("independant", "annuel")]["total_recurring"] == "1490.00"
    assert faces[("independant", "annuel")]["monthly_equivalent"] == "124.17"
    # Cabinet: 3 inclus → rien de facturé, la face courante.
    assert faces[("cabinet", "mensuel")]["total_recurring"] == "99.00"
    assert faces[("cabinet", "annuel")]["total_recurring"] == "990.00"
    assert faces[("cabinet", "annuel")]["monthly_equivalent"] == "82.50"
    assert faces[("agence", "mensuel")]["total_recurring"] == "129.00"
    assert faces[("agence", "annuel")]["monthly_equivalent"] == "107.50"


async def test_plan_change_quote_serves_the_offer_rule(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """La règle d'offre est SERVIE, jamais devinée : la face courante
    (même plan, même cycle) n'est pas sélectionnable et le dit ; le MÊME
    plan dans l'AUTRE cycle l'est (c'est la bascule de cycle) ; tout le
    reste l'est."""
    await _convert(client, superadmin, agent_headers, admin.agency_id, plan="cabinet")

    body = (await client.post("/billing/plan-change/quote", headers=agent_headers(admin))).json()
    faces = {(o["plan"], o["billing_cycle"]): o for o in body["options"]}

    current = faces[("cabinet", "mensuel")]
    assert current["is_current"] is True
    assert current["selectable"] is False
    assert current["reason"] == "current_plan_and_cycle"

    cycle_switch = faces[("cabinet", "annuel")]  # LA bascule de cycle offerte
    assert cycle_switch["is_current"] is False
    assert cycle_switch["selectable"] is True and cycle_switch["reason"] is None

    assert all(
        faces[key]["selectable"] is True for key in faces if key != ("cabinet", "mensuel")
    )  # une seule face fermée, jamais deux


async def test_plan_change_quote_guards(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Essai → 409 (rien à comparer sans abonnement) ; sur_mesure → 409
    nommée (pas de grille self-serve où asseoir l'agence)."""
    headers = agent_headers(admin)
    trial = await client.post("/billing/plan-change/quote", headers=headers)
    assert trial.status_code == 409, trial.text
    assert trial.json()["code"] == "billing.seats_require_subscription"

    await _convert(client, superadmin, agent_headers, admin.agency_id, plan="sur_mesure")
    sur_mesure = await client.post("/billing/plan-change/quote", headers=headers)
    assert sur_mesure.status_code == 409, sur_mesure.text
    assert sur_mesure.json()["code"] == "billing.quote_unavailable"


# --- L'EXÉCUTION : un seul PATCH Paddle (constat prouvé sandbox 10/08) -----------------


async def _paddle_agency(db: AsyncSession, agency_id: uuid.UUID, *, plan: str, cycle: str) -> None:
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    agency.plan = plan
    agency.billing_cycle = cycle
    agency.converted_at = datetime.now(UTC)
    agency.billing_mode = "paddle"
    agency.billing_status = "active"
    agency.paddle_subscription_id = "sub_plan_change"
    await db.commit()


async def test_plan_change_executes_in_one_patch_both_directions(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le geste qui manquait : plan ET cycle en UN update Paddle, prorata
    immédiat, l'état local écrit APRÈS Paddle. Descente Cabinet →
    Indépendant (3 gestionnaires → 2 sièges à 50), puis remontée (les
    sièges se résorbent : la ligne quitte l'envoi)."""
    await _paddle_agency(db_session, admin.agency_id, plan="cabinet", cycle="mensuel")
    for i in range(2):  # 3 managers
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"ex{i}@example.com"
        )
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    headers = agent_headers(admin)

    down = await client.post(
        "/billing/plan-change",
        headers=headers,
        json={"target_plan": "independant", "billing_cycle": "mensuel"},
    )
    assert down.status_code == 200, down.text
    assert down.json() == {
        "plan": "independant",
        "billing_cycle": "mensuel",
        "manager_seats_billed": 2,
        "reader_seats": 0,
        "total_recurring": "149.00",
    }
    push.assert_awaited_once()
    assert push.await_args.kwargs["proration_billing_mode"] == "prorated_immediately"
    items = {i["price_id"]: i["quantity"] for i in push.await_args.kwargs["items"]}
    assert items == {"pri_base_ind_m": 1, "pri_seat_ind_m": 2}
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    await db_session.refresh(agency)
    assert agency.plan == "independant" and agency.billing_cycle == "mensuel"

    # Remontée : les sièges à 50 se résorbent dans les 3 inclus.
    push.reset_mock()
    up = await client.post(
        "/billing/plan-change",
        headers=headers,
        json={"target_plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert up.status_code == 200, up.text
    assert up.json()["total_recurring"] == "99.00"
    items = {i["price_id"]: i["quantity"] for i in push.await_args.kwargs["items"]}
    assert items == {"pri_base_cab_m": 1}  # la ligne siège a disparu


async def test_plan_change_switches_the_cycle_in_the_same_gesture(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La bascule de cycle qu'Alexandre veut offrir : même plan, cycle
    annuel — un seul PATCH (constat sandbox : Paddle porte les deux)."""
    await _paddle_agency(db_session, admin.agency_id, plan="cabinet", cycle="mensuel")
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)

    switched = await client.post(
        "/billing/plan-change",
        headers=agent_headers(admin),
        json={"target_plan": "cabinet", "billing_cycle": "annuel"},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["billing_cycle"] == "annuel"
    assert switched.json()["total_recurring"] == "990.00"
    items = {i["price_id"]: i["quantity"] for i in push.await_args.kwargs["items"]}
    assert items == {"pri_base_cab_a": 1}
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    await db_session.refresh(agency)
    assert agency.billing_cycle == "annuel"


async def test_plan_change_guards(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Essai → la CONVERSION (409 not_paddle_managed, l'état d'essai du
    front) ; sur_mesure → refus nommé ; même plan + même cycle → 422 ;
    et AUCUN appel Paddle sur un refus."""
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    headers = agent_headers(admin)

    trial = await client.post(
        "/billing/plan-change",
        headers=headers,
        json={"target_plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert trial.status_code == 409, trial.text
    assert trial.json()["code"] == "billing.not_paddle_managed"  # → le checkout

    await _paddle_agency(db_session, admin.agency_id, plan="cabinet", cycle="mensuel")
    sur_mesure = await client.post(
        "/billing/plan-change",
        headers=headers,
        json={"target_plan": "sur_mesure", "billing_cycle": "mensuel"},
    )
    assert sur_mesure.status_code == 409, sur_mesure.text
    assert sur_mesure.json()["code"] == "billing.plan_change_unavailable"

    same = await client.post(
        "/billing/plan-change",
        headers=headers,
        json={"target_plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert same.status_code == 422, same.text
    assert same.json()["code"] == "billing.plan_change_same_plan"
    assert push.await_count == 0  # un refus ne touche jamais Paddle
