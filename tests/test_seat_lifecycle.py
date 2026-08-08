"""Constat 08/08 — the seat LIFECYCLE: included, decrease, cancellation.

Backs the constat's answers not already pinned by test_reader_seat /
test_offboarding / test_billing_paddle, against the real app:

(1) readers never pull the manager quantity NEGATIVE: with managers under
    the included tier and a purchased reader pool, the pushed item list
    carries NO manager seat item (absent = 0, never −1) and the reader
    item is the pool;
(2) the trial pushes NOTHING to Paddle: invitation acceptance,
    deactivation and reactivation in trial never call
    update_subscription_items (there is no subscription to push to);
(3) the first checkout of a converting trial agency with readers is the
    EXACT amount: base×1 + reader×N, no manager seat item (the S2 rule
    priced at entry — the conversion UI must announce it);
(4) deactivating a READER returns the seat to the POOL: free +1, the
    Paddle quantity untouched (the pool keeps billing until RELEASED);
(5) reactivation is symmetric per type: a reader takes a FREE pool seat
    (named 409 when none is free) and never pushes; a manager re-pushes
    the mirror UP (prorated_immediately), same rule as a new acceptance.
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.invitation import AgentInvitation
from shared.models.rbac import Role
from src.billing import paddle_client
from src.billing.billing_manager import BillingManager
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

PRICE_IDS = {
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
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setenv("PADDLE_PRICE_IDS", json.dumps(PRICE_IDS))
    monkeypatch.setenv("BILLING_CHECKOUT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _paddle_activate(db: AsyncSession, agency_id: uuid.UUID, *, pool: int = 0) -> None:
    """Wire a LIVE paddle subscription directly (the webhook's outcome)."""
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    agency.plan = "cabinet"
    agency.billing_cycle = "mensuel"
    agency.converted_at = datetime.now(UTC)
    agency.billing_mode = "paddle"
    agency.billing_status = "active"
    agency.paddle_subscription_id = f"sub_{uuid.uuid4().hex[:10]}"
    agency.reader_seats_purchased = pool
    await db.commit()


async def _seats(client: AsyncClient, headers: dict[str, str]) -> dict:
    me = await client.get("/agencies/me", headers=headers)
    assert me.status_code == 200, me.text
    return me.json()["subscription"]["seats"]


def _mock_push(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    return push


# --- (1) the clamp: readers never pull the manager quantity negative -------------------


async def test_readers_never_pull_the_manager_quantity_negative(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 manager + 3 pooled readers on a 3-included plan: billed is 0 (a
    clamp, not −2) and the push carries NO manager seat item at all —
    the reader item alone rides beside the base plan."""
    await _paddle_activate(db_session, admin.agency_id, pool=3)
    for i in range(3):
        await make_agent(
            role=system_roles["viewer"],
            agency_id=admin.agency_id,
            email=f"lecteur-{i}@example.com",
            seat_type="reader",
        )
    push = _mock_push(monkeypatch)

    await BillingManager(db_session).sync_seat_quantity(admin.agency_id, increase=False)

    push.assert_awaited_once()
    items = {i["price_id"]: i["quantity"] for i in push.await_args.kwargs["items"]}
    assert items == {"pri_base_cab_m": 1, "pri_seat_reader_m": 3}  # no manager item = 0

    seats = await _seats(client, agent_headers(admin))
    assert seats["managers"] == 1 and seats["billed"] == 0
    assert seats["reader"] == {"purchased": 3, "used": 3, "free": 0}


# --- (2) the trial never pushes --------------------------------------------------------


async def test_trial_member_gestures_never_touch_paddle(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trial: 3 seats total across types and ZERO Paddle traffic — the
    acceptance, the deactivation and the reactivation all cross the sync
    call sites, and none reaches update_subscription_items."""
    push = _mock_push(monkeypatch)
    headers = agent_headers(admin)

    invited = await client.post(
        "/agencies/me/invitations",
        headers=headers,
        json={"email": "trial-m@example.com", "role_id": str(system_roles["member"].id)},
    )
    assert invited.status_code == 201, invited.text
    invitation = (
        await db_session.execute(
            select(AgentInvitation).where(AgentInvitation.email == "trial-m@example.com")
        )
    ).scalar_one()
    accepted = await client.post(
        "/agencies/invitations/accept",
        json={
            "token": invitation.token,
            "password": "TrialPassword1!",
            "first_name": "T",
            "last_name": "M",
        },
    )
    assert accepted.status_code == 200, accepted.text
    manager = (
        await db_session.execute(select(Agent).where(Agent.email == "trial-m@example.com"))
    ).scalar_one()
    await make_agent(
        role=system_roles["viewer"],
        agency_id=admin.agency_id,
        email="trial-r@example.com",
        seat_type="reader",
    )

    gone = await client.post(f"/agencies/me/members/{manager.id}/deactivate", headers=headers)
    assert gone.status_code == 200, gone.text
    back = await client.post(f"/agencies/me/members/{manager.id}/reactivate", headers=headers)
    assert back.status_code == 204, back.text

    assert push.await_count == 0  # no subscription, no push — ever, in trial
    seats = await _seats(client, headers)
    assert seats["members"] == 3 and seats["max"] == 3
    assert seats["reader"] == {"purchased": 0, "used": 1, "free": 0}


# --- (3) conversion with trial readers: the exact first amount -------------------------


async def test_checkout_amount_is_exact_for_a_trial_agency_with_readers(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The S2 surprise, priced: converting with 1 manager + 2 trial
    readers charges base×1 + reader×2 and NOTHING else — no manager seat
    item (1 manager sits inside the included tier). This exact list is
    what the conversion UI must announce."""
    for i in range(2):
        await make_agent(
            role=system_roles["viewer"],
            agency_id=admin.agency_id,
            email=f"essai-{i}@example.com",
            seat_type="reader",
        )
    create_txn = AsyncMock(return_value={"id": "txn_lifecycle_1"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create_txn)

    checkout = await client.post(
        "/billing/checkout",
        headers=agent_headers(admin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert checkout.status_code == 200, checkout.text
    assert create_txn.await_args.kwargs["items"] == [
        {"price_id": "pri_base_cab_m", "quantity": 1},
        {"price_id": "pri_seat_reader_m", "quantity": 2},
    ]


# --- (4) reader deactivation returns the seat to the pool ------------------------------


async def test_reader_deactivation_frees_a_pool_seat_without_any_push(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pool semantics: N purchased seats, freely attributed. A reader
    leaving frees a seat (+1 free), bills nothing differently, pushes
    nothing — the pool keeps billing until RELEASED (seats/remove)."""
    await _paddle_activate(db_session, admin.agency_id, pool=2)
    readers = [
        await make_agent(
            role=system_roles["viewer"],
            agency_id=admin.agency_id,
            email=f"pool-{i}@example.com",
            seat_type="reader",
        )
        for i in range(2)
    ]
    push = _mock_push(monkeypatch)
    headers = agent_headers(admin)

    gone = await client.post(f"/agencies/me/members/{readers[0].id}/deactivate", headers=headers)
    assert gone.status_code == 200, gone.text

    assert push.await_count == 0  # the billed pool is untouched by occupancy
    seats = await _seats(client, headers)
    assert seats["reader"] == {"purchased": 2, "used": 1, "free": 1}


# --- (5) reactivation, symmetric per type ----------------------------------------------


async def test_reader_reactivation_takes_a_free_pool_seat_or_409(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coming back consumes a POOL seat, same rule as accepting a reader
    invitation: full pool → named 409; a bought seat opens the door; the
    reactivation itself never pushes (occupancy is not billing)."""
    await _paddle_activate(db_session, admin.agency_id, pool=1)
    headers = agent_headers(admin)
    returning = await make_agent(
        role=system_roles["viewer"],
        agency_id=admin.agency_id,
        email="revenant@example.com",
        seat_type="reader",
    )
    gone = await client.post(f"/agencies/me/members/{returning.id}/deactivate", headers=headers)
    assert gone.status_code == 200, gone.text
    await make_agent(  # the freed seat is taken while they are away
        role=system_roles["viewer"],
        agency_id=admin.agency_id,
        email="occupant@example.com",
        seat_type="reader",
    )
    push = _mock_push(monkeypatch)

    blocked = await client.post(f"/agencies/me/members/{returning.id}/reactivate", headers=headers)
    assert blocked.status_code == 409, blocked.text
    body = blocked.json()
    assert body["code"] == "subscription.reader_seat_limit"
    assert body["params"] == {"purchased": 1, "used": 1}

    bought = await client.post("/billing/seats/add", headers=headers, json={"reader": 1})
    assert bought.status_code == 200, bought.text
    assert push.await_count == 1  # the PURCHASE pushes…
    push.reset_mock()

    back = await client.post(f"/agencies/me/members/{returning.id}/reactivate", headers=headers)
    assert back.status_code == 204, back.text
    assert push.await_count == 0  # …the reactivation does not
    seats = await _seats(client, headers)
    assert seats["reader"] == {"purchased": 2, "used": 2, "free": 0}


async def test_manager_reactivation_pushes_the_mirror_up(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror is symmetric: deactivation pushed the quantity down at
    next cycle (test_offboarding); the reactivation pushes it back UP,
    prorated immediately — the returning seat is billed like a new one."""
    await _paddle_activate(db_session, admin.agency_id)
    headers = agent_headers(admin)
    extras = [
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"mgr-{i}@example.com"
        )
        for i in range(4)
    ]  # 5 managers on cabinet (3 included) → billed 2
    push = _mock_push(monkeypatch)

    gone = await client.post(f"/agencies/me/members/{extras[0].id}/deactivate", headers=headers)
    assert gone.status_code == 200, gone.text
    assert push.await_args.kwargs["proration_billing_mode"] == "full_next_billing_period"
    push.reset_mock()

    back = await client.post(f"/agencies/me/members/{extras[0].id}/reactivate", headers=headers)
    assert back.status_code == 204, back.text
    push.assert_awaited_once()
    kwargs = push.await_args.kwargs
    assert kwargs["proration_billing_mode"] == "prorated_immediately"
    items = {i["price_id"]: i["quantity"] for i in kwargs["items"]}
    assert items["pri_seat_cab_m"] == 2  # 5 actifs − 3 included, restored
