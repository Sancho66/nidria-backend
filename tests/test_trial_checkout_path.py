"""Incident 14/09 (« aucune agence ne peut plus s'abonner ») — THE witness
that was missing: an agency WITHOUT a Paddle subscription that chooses a
plan gets a CHECKOUT, on an active trial AND on an expired one (under the
billing wall: POST /billing/checkout is the wall's exit, allowlisted).

Pins the whole contract the subscription page relies on, cold:
- GET /billing/subscription answers the TRIAL 409 `billing.not_paddle_managed`
  with `checkout_enabled: true` (the expected state, not a failure);
- POST /billing/checkout creates the transaction with the chosen plan's
  base price id — every plan, both cycles;
- the plan-change gestures are NOT the path for a subscription-less agency
  (403 wall on an expired trial) — a front that calls them there is on the
  wrong route, and this file says which route is right.

Reproduced 14/09 on v0.137.1 and v0.138.0: identical, no regression —
this file exists so the next incident report can be checked in 20 s.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.billing import paddle_client
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
PLANS = ("independant", "cabinet", "agence")
CYCLES = ("mensuel", "annuel")


@pytest.fixture(autouse=True)
def paddle_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_PRICE_IDS", json.dumps(PRICE_IDS))
    monkeypatch.setenv("BILLING_CHECKOUT_ENABLED", "true")
    from src.billing import billing_manager

    billing_manager._SUBSCRIPTION_CACHE.clear()
    billing_manager._CATALOG_PRICES_CACHE = None
    monkeypatch.setattr(paddle_client.PaddleClient, "list_prices", AsyncMock(return_value=[]))
    get_settings.cache_clear()
    yield
    billing_manager._SUBSCRIPTION_CACHE.clear()
    billing_manager._CATALOG_PRICES_CACHE = None
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _set_trial(db: AsyncSession, agency_id: uuid.UUID, ends_at: datetime) -> None:
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    assert agency.converted_at is None and agency.paddle_subscription_id is None
    agency.trial_ends_at = ends_at
    await db.commit()


@pytest.mark.parametrize(
    "trial_state",
    ["active", "expired"],
)
async def test_subscription_less_agency_choosing_a_plan_gets_a_checkout(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
    trial_state: str,
) -> None:
    now = datetime.now(UTC)
    ends_at = now + timedelta(days=15) if trial_state == "active" else now - timedelta(days=3)
    await _set_trial(db_session, admin.agency_id, ends_at)
    headers = agent_headers(admin)

    # 1. The page's cold read: the lock truth, then the billing 409 —
    #    the EXPECTED trial state, carrying the checkout switch.
    me = (await client.get("/agencies/me", headers=headers)).json()["subscription"]
    assert me["plan"] is None
    assert me["is_blocked"] is (trial_state == "expired")
    assert me["blocked_reason"] == ("trial_expired" if trial_state == "expired" else None)

    state = await client.get("/billing/subscription", headers=headers)
    assert state.status_code == 409, state.text
    body = state.json()
    assert body["code"] == "billing.not_paddle_managed"
    assert body["params"]["checkout_enabled"] is True
    assert body["params"]["trial_ends_at"] is not None

    # 2. Choosing a plan = POST /billing/checkout → a transaction on the
    #    plan's base price, active trial or expired (the wall's exit).
    create_txn = AsyncMock(side_effect=lambda **kw: {"id": f"txn_{uuid.uuid4().hex[:8]}"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create_txn)
    for plan in PLANS:
        for cycle in CYCLES:
            response = await client.post(
                "/billing/checkout",
                headers=headers,
                json={"plan": plan, "billing_cycle": cycle},
            )
            assert response.status_code == 200, (plan, cycle, response.text)
            assert response.json()["transaction_id"].startswith("txn_")
            assert response.json()["paddle_env"] == "sandbox"
            items = create_txn.await_args.kwargs["items"]
            assert items[0] == {"price_id": PRICE_IDS[f"{plan}_{cycle}"], "quantity": 1}
            assert create_txn.await_args.kwargs["custom_data"] == {
                "agency_id": str(admin.agency_id)
            }
    assert create_txn.await_count == len(PLANS) * len(CYCLES)


async def test_plan_change_is_not_the_route_for_a_subscription_less_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan-change gestures belong to the SUBSCRIBED card. Without a
    subscription they never reach Paddle: 403 wall on an expired trial,
    named 409s on an active one — and the checkout stays open regardless."""
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    headers = agent_headers(admin)
    body = {"target_plan": "cabinet", "billing_cycle": "mensuel"}

    await _set_trial(db_session, admin.agency_id, datetime.now(UTC) + timedelta(days=15))
    quote = await client.post("/billing/plan-change/quote", headers=headers)
    assert quote.status_code == 409 and quote.json()["code"] == "billing.seats_require_subscription"
    change = await client.post("/billing/plan-change", headers=headers, json=body)
    assert change.status_code == 409 and change.json()["code"] == "billing.not_paddle_managed"
    assert change.json()["params"]["checkout_enabled"] is True

    await _set_trial(db_session, admin.agency_id, datetime.now(UTC) - timedelta(days=3))
    for path in ("/billing/plan-change/quote", "/billing/plan-change"):
        walled = await client.post(path, headers=headers, json=body)
        assert walled.status_code == 403, (path, walled.text)
        assert walled.json()["code"] == "billing.subscription_required"
    push.assert_not_awaited()
