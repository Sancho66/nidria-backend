"""Lot pricing 14/09 — a PRICE ROTATION never reprices a running subscription.

The grid change (49→99, 99→150, 169→299 and the seats) is a rotation:
new Paddle prices, old ones ARCHIVED. An archived price keeps billing at
its amount (Paddle doctrine, proven sandbox 14/09: a sub on the archived
12900c Agence price still previews a 12900c renewal) — but OUR pushes
rebuild the item list, and the env now names the FRESH ids. Pushing the
env's base id for a line the subscription already carries would move the
client to the new grid, prorated immediately: exactly the silent
repricing the rotation must never do (the Nicolas case: Cabinet annuel
at 990, one accepted invitation away from a 1500 push).

Pinned here, against the real app:
(a) sync_seat_quantity keeps the subscription's OWN base price id (an
    archived one) and adds the NEW seat line at the current env id;
(b) the reader purchase (POST /billing/seats/add) keeps both the base
    and the existing reader line at their subscription ids;
(c) a subscription whose items carry no stable_key falls back to the
    env ids — the pre-rotation behaviour, unchanged;
(d) the helper itself: stable_key → id, nothing else read;
(e) THE GUARD (correctif immédiat 14/09): a kept price Paddle no longer lists
    as active makes the push FAIL LOUD — 409 `billing.kept_price_inactive`,
    internal alert mail, NOTHING pushed — never a silent skip, never the
    env's fresh id (the implicit repricing).
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.billing import paddle_client
from src.billing.billing_manager import BillingManager, _kept_price_ids
from src.core.config import get_settings
from src.core.exceptions import ConflictError
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

# The env after the rotation: FRESH ids everywhere.
PRICE_IDS = {
    "independant_mensuel": "pri_new_base_ind_m",
    "independant_annuel": "pri_new_base_ind_a",
    "seat_independant_mensuel": "pri_new_seat_ind_m",
    "seat_independant_annuel": "pri_new_seat_ind_a",
    "cabinet_mensuel": "pri_new_base_cab_m",
    "cabinet_annuel": "pri_new_base_cab_a",
    "agence_mensuel": "pri_new_base_age_m",
    "agence_annuel": "pri_new_base_age_a",
    "seat_cabinet_mensuel": "pri_new_seat_cab_m",
    "seat_cabinet_annuel": "pri_new_seat_cab_a",
    "seat_agence_mensuel": "pri_new_seat_age_m",
    "seat_agence_annuel": "pri_new_seat_age_a",
    "seat_reader_mensuel": "pri_new_seat_reader_m",
    "seat_reader_annuel": "pri_new_seat_reader_a",
}
# The ids the running subscription was sold on — archived since.
OLD_BASE_CAB_A = "pri_old_base_cab_a"
OLD_READER_A = "pri_old_seat_reader_a"


@pytest.fixture(autouse=True)
def paddle_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_PRICE_IDS", json.dumps(PRICE_IDS))
    monkeypatch.setenv("BILLING_CHECKOUT_ENABLED", "true")
    from src.billing import billing_manager

    billing_manager._SUBSCRIPTION_CACHE.clear()
    billing_manager._CATALOG_PRICES_CACHE = None
    get_settings.cache_clear()
    yield
    billing_manager._SUBSCRIPTION_CACHE.clear()
    billing_manager._CATALOG_PRICES_CACHE = None
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _item(price_id: str, stable_key: str | None, amount: str, quantity: int) -> dict[str, Any]:
    price: dict[str, Any] = {
        "id": price_id,
        "status": "archived",
        "unit_price": {"amount": amount, "currency_code": "EUR"},
    }
    if stable_key is not None:
        price["custom_data"] = {"stable_key": stable_key}
    return {"quantity": quantity, "price": price}


def _nicolas_subscription(*, with_reader: bool = False, keyed: bool = True) -> dict[str, Any]:
    """Cabinet ANNUEL sold on the pre-rotation price (99 000c), still
    active, renewing next year — the live case as of 14/09."""
    items = [_item(OLD_BASE_CAB_A, "cabinet_annuel" if keyed else None, "99000", 1)]
    if with_reader:
        items.append(_item(OLD_READER_A, "seat_reader_annuel" if keyed else None, "11988", 2))
    return {
        "id": "sub_nicolas",
        "status": "active",
        "currency_code": "EUR",
        "next_billed_at": "2027-07-28T13:03:58Z",
        "current_billing_period": {
            "starts_at": "2026-07-28T13:03:58Z",
            "ends_at": "2027-07-28T13:03:58Z",
        },
        "scheduled_change": None,
        "items": items,
        "next_transaction": {"details": {"totals": {"grand_total": "99000"}}},
    }


def _active(*price_ids: str) -> list[dict[str, Any]]:
    """What GET /prices?status=active&id=… answers for the kept ids."""
    return [{"id": pid, "status": "active"} for pid in price_ids]


async def _paddle_cabinet_annuel(
    db: AsyncSession, agency_id: uuid.UUID, *, readers: int = 0
) -> None:
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    agency.plan = "cabinet"
    agency.billing_cycle = "annuel"
    agency.converted_at = datetime.now(UTC)
    agency.billing_mode = "paddle"
    agency.billing_status = "active"
    agency.paddle_subscription_id = "sub_nicolas"
    agency.reader_seats_purchased = readers
    await db.commit()


# --- (a) the seat sync keeps the archived base, adds the seat at the new id ------------


async def test_seat_sync_keeps_the_subscription_base_price_across_a_rotation(
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4th manager on Cabinet annuel: the push carries the seat line at
    the CURRENT grid (new id) and the base at the id the subscription
    already has — never the env's fresh base (that would bill 1500 − 990
    prorated on the spot, without anyone asking for a plan change)."""
    await _paddle_cabinet_annuel(db_session, admin.agency_id)
    for i in range(3):  # 4 managers: 1 past the 3 included
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    monkeypatch.setattr(
        paddle_client.PaddleClient,
        "get_subscription",
        AsyncMock(return_value=_nicolas_subscription()),
    )
    # The kept price is verified ACTIVE in Paddle before the push (guard e).
    list_prices = AsyncMock(return_value=_active(OLD_BASE_CAB_A))
    monkeypatch.setattr(paddle_client.PaddleClient, "list_prices", list_prices)

    await BillingManager(db_session).sync_seat_quantity(admin.agency_id, increase=True)

    list_prices.assert_awaited_once_with(ids=[OLD_BASE_CAB_A])
    push.assert_awaited_once()
    items = {i["price_id"]: i["quantity"] for i in push.await_args.kwargs["items"]}
    assert items == {OLD_BASE_CAB_A: 1, "pri_new_seat_cab_a": 1}
    assert "pri_new_base_cab_a" not in items  # the repricing id, never pushed


# --- (b) the reader purchase keeps base AND reader line ---------------------------------


async def test_reader_purchase_keeps_every_existing_line_at_its_price(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """+1 reader on a sub that already carries 2 readers on the archived
    reader price: the pool goes to 3 on THAT id, the base stays on its
    own — the env's fresh ids are not used for lines that exist."""
    await _paddle_cabinet_annuel(db_session, admin.agency_id, readers=2)
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    monkeypatch.setattr(
        paddle_client.PaddleClient,
        "get_subscription",
        AsyncMock(return_value=_nicolas_subscription(with_reader=True)),
    )
    monkeypatch.setattr(
        paddle_client.PaddleClient,
        "list_prices",
        AsyncMock(return_value=_active(OLD_BASE_CAB_A, OLD_READER_A)),
    )

    response = await client.post(
        "/billing/seats/add", headers=agent_headers(admin), json={"reader": 1}
    )
    assert response.status_code == 200, response.text
    assert response.json()["reader"]["purchased"] == 3

    push.assert_awaited_once()
    items = {i["price_id"]: i["quantity"] for i in push.await_args.kwargs["items"]}
    assert items == {OLD_BASE_CAB_A: 1, OLD_READER_A: 3}


# --- (c) no stable_key on the items: the env ids, as before ---------------------------


async def test_items_without_stable_key_fall_back_to_the_env_ids(
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _paddle_cabinet_annuel(db_session, admin.agency_id)
    for i in range(3):
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    monkeypatch.setattr(
        paddle_client.PaddleClient,
        "get_subscription",
        AsyncMock(return_value=_nicolas_subscription(keyed=False)),
    )

    await BillingManager(db_session).sync_seat_quantity(admin.agency_id, increase=True)

    items = {i["price_id"]: i["quantity"] for i in push.await_args.kwargs["items"]}
    assert items == {"pri_new_base_cab_a": 1, "pri_new_seat_cab_a": 1}


# --- (e) THE GUARD: a kept price gone inactive fails loud, pushes nothing -------------


async def test_seat_sync_refuses_loudly_when_a_kept_price_is_no_longer_active(
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The base price Nicolas bills on was archived by mistake: Paddle's
    active listing no longer returns it. The push is REFUSED — stable code,
    the inactive ids named, an internal alert to the team — and NOTHING is
    pushed: neither the archived id nor the env's fresh one (that would be
    the implicit repricing)."""
    from src.core import email

    monkeypatch.setenv("BILLING_ALERT_ENABLED", "true")
    get_settings.cache_clear()
    await _paddle_cabinet_annuel(db_session, admin.agency_id)
    for i in range(3):
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    monkeypatch.setattr(
        paddle_client.PaddleClient,
        "get_subscription",
        AsyncMock(return_value=_nicolas_subscription()),
    )
    monkeypatch.setattr(paddle_client.PaddleClient, "list_prices", AsyncMock(return_value=[]))
    email.outbox.clear()

    with pytest.raises(ConflictError) as raised:
        await BillingManager(db_session).sync_seat_quantity(admin.agency_id, increase=True)

    assert raised.value.code == "billing.kept_price_inactive"
    assert raised.value.params == {
        "subscription_id": "sub_nicolas",
        "prices": {"cabinet_annuel": OLD_BASE_CAB_A},
    }
    push.assert_not_awaited()  # nothing pushed, nothing repriced
    alerts = [m for m in email.outbox if "kept price inactive" in m.subject]
    assert len(alerts) == 1
    assert OLD_BASE_CAB_A in alerts[0].body and "sub_nicolas" in alerts[0].body


async def test_reader_purchase_is_a_409_when_a_kept_price_is_no_longer_active(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same guard on the API face: the reader purchase answers 409 with the
    stable code, the pool is NOT changed, nothing is pushed."""
    await _paddle_cabinet_annuel(db_session, admin.agency_id, readers=2)
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    monkeypatch.setattr(
        paddle_client.PaddleClient,
        "get_subscription",
        AsyncMock(return_value=_nicolas_subscription(with_reader=True)),
    )
    # Only the base is still active: the reader line's price went missing.
    monkeypatch.setattr(
        paddle_client.PaddleClient, "list_prices", AsyncMock(return_value=_active(OLD_BASE_CAB_A))
    )

    response = await client.post(
        "/billing/seats/add", headers=agent_headers(admin), json={"reader": 1}
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "billing.kept_price_inactive"
    assert body["params"]["prices"] == {"seat_reader_annuel": OLD_READER_A}
    push.assert_not_awaited()
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    await db_session.refresh(agency)
    assert agency.reader_seats_purchased == 2  # the pool did not move


# --- (d) the helper -------------------------------------------------------------------


def test_kept_price_ids_reads_stable_keys_only() -> None:
    subscription = _nicolas_subscription(with_reader=True)
    assert _kept_price_ids(subscription) == {
        "cabinet_annuel": OLD_BASE_CAB_A,
        "seat_reader_annuel": OLD_READER_A,
    }
    assert _kept_price_ids(_nicolas_subscription(keyed=False)) == {}
    assert _kept_price_ids({}) == {}
