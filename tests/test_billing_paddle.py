"""Paddle billing (Merchant of Record) — the merge conditions. Everything is
webhook-driven (no cron); the Paddle client is MOCKED everywhere (zero network
call); the signature is real HMAC over the raw body; handlers converge on
out-of-order deliveries and a manual agency is never written by a webhook."""

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.invitation import AgentInvitation
from shared.models.paddle_event import PaddleWebhookEvent
from shared.models.rbac import Role
from shared.models.usage import UsageEvent
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

SECRET = "test-webhook-secret"
PRICE_IDS = {
    "cabinet_mensuel": "pri_base_cab_m",
    "cabinet_annuel": "pri_base_cab_a",
    "agence_mensuel": "pri_base_age_m",
    "agence_annuel": "pri_base_age_a",
    "seat_cabinet_mensuel": "pri_seat_cab_m",
    "seat_cabinet_annuel": "pri_seat_cab_a",
    "seat_agence_mensuel": "pri_seat_age_m",
    "seat_agence_annuel": "pri_seat_age_a",
}


@pytest.fixture(autouse=True)
def paddle_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("PADDLE_PRICE_IDS", json.dumps(PRICE_IDS))
    # The offer is OPEN in this harness (closed by default in real life);
    # the kill-switch tests close it explicitly.
    monkeypatch.setenv("BILLING_CHECKOUT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _sign(raw: bytes, *, secret: str = SECRET, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    digest = hmac.new(secret.encode(), f"{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={digest}"


def _envelope(
    event_type: str,
    *,
    agency_id: uuid.UUID | None,
    occurred_at: datetime | None = None,
    items: list[dict[str, Any]] | None = None,
    status: str | None = None,
    subscription_id: str = "sub_123",
    event_id: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
        "event_type": event_type,
        "occurred_at": (occurred_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "data": {
            "id": subscription_id,
            "customer_id": "ctm_123",
            "status": status,
            "custom_data": {"agency_id": str(agency_id)} if agency_id else {},
            "items": items
            if items is not None
            else [{"price": {"id": PRICE_IDS["cabinet_mensuel"]}, "quantity": 1}],
        },
    }


async def _post(client: AsyncClient, envelope: dict[str, Any], *, signature: str | None = "auto"):
    raw = json.dumps(envelope).encode()
    headers = {"content-type": "application/json"}
    if signature == "auto":
        headers["Paddle-Signature"] = _sign(raw)
    elif signature is not None:
        headers["Paddle-Signature"] = signature
    return await client.post("/billing/webhooks/paddle", content=raw, headers=headers)


async def _agency(db: AsyncSession, agency_id: uuid.UUID) -> Agency:
    db.expire_all()
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    return agency


async def _event_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(PaddleWebhookEvent))).scalar_one()


# --- signature invalide -> 401, rien d'ecrit ------------------------------------------


async def test_invalid_signature_is_401_and_writes_nothing(
    client: AsyncClient, db_session: AsyncSession, admin: Agent
) -> None:
    aid = admin.agency_id
    envelope = _envelope("subscription.activated", agency_id=aid)
    raw = json.dumps(envelope).encode()

    # No header, wrong secret, and a stale (replayed) timestamp: all 401.
    assert (await _post(client, envelope, signature=None)).status_code == 401
    assert (await _post(client, envelope, signature=_sign(raw, secret="wrong"))).status_code == 401
    assert (
        await _post(client, envelope, signature=_sign(raw, ts=int(time.time()) - 3600))
    ).status_code == 401
    assert await _event_count(db_session) == 0  # nothing stored
    agency = await _agency(db_session, aid)
    assert agency.converted_at is None and agency.billing_mode == "manual"


# --- event_id rejoue -> no-op 200 ------------------------------------------------------


async def test_replayed_event_id_is_a_noop(
    client: AsyncClient, db_session: AsyncSession, admin: Agent
) -> None:
    aid = admin.agency_id
    envelope = _envelope("subscription.activated", agency_id=aid)
    first = await _post(client, envelope)
    assert first.status_code == 200 and first.json()["status"] == "processed"
    replay = await _post(client, envelope)  # same event_id, valid signature
    assert replay.status_code == 200 and replay.json()["status"] == "duplicate"
    assert await _event_count(db_session) == 1  # stored exactly once


# --- agence inconnue -> 200 + alerte, aucune creation ----------------------------------


async def test_unknown_agency_is_stored_alerted_never_created(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, caplog: pytest.LogCaptureFixture
) -> None:
    before = (await db_session.execute(select(func.count()).select_from(Agency))).scalar_one()
    envelope = _envelope("subscription.activated", agency_id=uuid.uuid4())  # unknown
    with caplog.at_level("ERROR"):
        resp = await _post(client, envelope)
    assert resp.status_code == 200 and resp.json()["status"] == "ignored"
    assert any("UNKNOWN agency" in r.message for r in caplog.records)  # the alert
    after = (await db_session.execute(select(func.count()).select_from(Agency))).scalar_one()
    assert after == before  # no implicit creation
    row = (await db_session.execute(select(PaddleWebhookEvent))).scalar_one()
    assert row.agency_id is None  # audited all the same


# --- billing_mode=manual + webhook -> no-op + alerte -----------------------------------


async def test_manual_agency_is_never_written_by_webhooks(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aid = admin.agency_id
    # A manually-CONVERTED agency (Nicolas): even `activated` must not write.
    await db_session.execute(
        update(Agency)
        .where(Agency.id == aid)
        .values(plan="cabinet", billing_cycle="mensuel", converted_at=datetime.now(UTC))
    )
    await db_session.commit()
    manual_converted_at = (await _agency(db_session, aid)).converted_at

    with caplog.at_level("ERROR"):
        for event_type in (
            "subscription.updated",
            "subscription.canceled",
            "subscription.activated",
        ):
            resp = await _post(client, _envelope(event_type, agency_id=aid))
            assert resp.status_code == 200 and resp.json()["status"] == "ignored"
    assert any("MANUAL agency" in r.message for r in caplog.records)
    agency = await _agency(db_session, aid)
    assert agency.billing_mode == "manual"  # the superadmin keeps the hand
    assert agency.billing_status is None
    assert agency.converted_at == manual_converted_at


# --- activated : conversion par LE geste unique ----------------------------------------


async def test_activated_converts_via_the_single_gesture(
    client: AsyncClient, db_session: AsyncSession, admin: Agent
) -> None:
    aid = admin.agency_id
    occurred = datetime.now(UTC) - timedelta(minutes=5)
    resp = await _post(
        client,
        _envelope("subscription.activated", agency_id=aid, occurred_at=occurred),
    )
    assert resp.status_code == 200 and resp.json()["status"] == "processed"

    agency = await _agency(db_session, aid)
    assert agency.converted_at == occurred  # Paddle's clock, not ours
    assert agency.plan == "cabinet" and agency.billing_cycle == "mensuel"
    assert agency.seat_price_eur == 35
    assert agency.billing_mode == "paddle" and agency.billing_status == "active"
    assert agency.paddle_subscription_id == "sub_123"
    assert agency.paddle_customer_id == "ctm_123"

    # The SAME gesture as the manual PATCH emitted the usage signal…
    emitted = (
        (
            await db_session.execute(
                select(UsageEvent).where(
                    UsageEvent.agency_id == aid,
                    UsageEvent.event_type == "agency.converted",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(emitted) == 1 and emitted[0].actor_type == "system"
    # …and it is the ONLY emission point in the whole codebase (structural).
    hits = [
        p
        for p in Path("src").rglob("*.py")
        if '"agency.converted"' in p.read_text(encoding="utf-8")
    ]
    assert hits == [Path("src/agencies/agencies_manager.py")], hits


# --- activated rejoue / converted_at deja pose -> jamais ecrase ------------------------


async def test_activated_never_overwrites_converted_at(
    client: AsyncClient, db_session: AsyncSession, admin: Agent
) -> None:
    aid = admin.agency_id
    first_at = datetime.now(UTC) - timedelta(days=2)
    resp = await _post(
        client,
        _envelope("subscription.activated", agency_id=aid, occurred_at=first_at),
    )
    assert resp.json()["status"] == "processed"
    # A re-delivery variant (new event_id, later occurred_at): no overwrite.
    resp = await _post(client, _envelope("subscription.activated", agency_id=aid))
    assert resp.status_code == 200
    agency = await _agency(db_session, aid)
    assert agency.converted_at == first_at
    # And the usage signal was emitted ONCE, not per delivery.
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                UsageEvent.agency_id == aid,
                UsageEvent.event_type == "agency.converted",
            )
        )
    ).scalar_one()
    assert count == 1


# --- updated avec quantity divergente -> alerte, rien d'ecrit --------------------------


async def test_updated_with_diverging_quantity_alerts_and_writes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aid = admin.agency_id
    await _post(client, _envelope("subscription.activated", agency_id=aid))
    # billed is 0 (1 member, 3 included) — an echo claiming 4 paid seats lies.
    envelope = _envelope(
        "subscription.updated",
        agency_id=aid,
        items=[
            {"price": {"id": PRICE_IDS["agence_mensuel"]}, "quantity": 1},
            {"price": {"id": PRICE_IDS["seat_agence_mensuel"]}, "quantity": 4},
        ],
        status="active",
    )
    with caplog.at_level("ERROR"):
        resp = await _post(client, envelope)
    assert resp.json()["status"] == "ignored"
    assert any("diverges from billed" in r.message for r in caplog.records)
    agency = await _agency(db_session, aid)
    assert agency.plan == "cabinet"  # the lying update changed NOTHING


# --- PATCH /subscription : paddle -> 409, manual -> inchange ---------------------------


async def test_manual_patch_is_409_on_paddle_agency_unchanged_on_manual(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    aid = admin.agency_id
    superadmin = await make_agent(role=system_roles["superadmin"], email="root@platform.io")
    sh = agent_headers(superadmin)

    # paddle agency → plan/cycle/converted_at are refused by hand…
    await _post(client, _envelope("subscription.activated", agency_id=aid))
    denied = await client.patch(
        f"/agencies/{aid}/subscription", headers=sh, json={"plan": "agence"}
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "subscription.managed_by_paddle"
    # …but the founding fields stay OUR concepts, editable.
    ok = await client.patch(f"/agencies/{aid}/subscription", headers=sh, json={"is_founding": True})
    assert ok.status_code == 200, ok.text

    # manual agency → the superadmin path works exactly as before.
    other = await make_agent(role=system_roles["admin"])
    converted = await client.patch(
        f"/agencies/{other.agency_id}/subscription",
        headers=sh,
        json={"plan": "agence", "billing_cycle": "annuel"},
    )
    assert converted.status_code == 200, converted.text
    manual = await _agency(db_session, other.agency_id)
    assert manual.plan == "agence" and manual.billing_mode == "manual"


# --- billing_mode jamais editable vers "paddle" a la main ------------------------------


async def test_billing_mode_is_never_hand_editable(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    from src.agencies.agencies_schema import AgencyUpdateRequest, SubscriptionUpdateRequest

    # Structural: NO request schema carries billing_mode — there is no door.
    assert "billing_mode" not in SubscriptionUpdateRequest.model_fields
    assert "billing_mode" not in AgencyUpdateRequest.model_fields

    superadmin = await make_agent(role=system_roles["superadmin"], email="root2@platform.io")
    target = await make_agent(role=system_roles["admin"])
    # Sending it anyway is inert (unknown fields are ignored, mode unmoved).
    resp = await client.patch(
        f"/agencies/{target.agency_id}/subscription",
        headers=agent_headers(superadmin),
        json={"billing_mode": "paddle"},
    )
    assert resp.status_code == 200
    assert (await _agency(db_session, target.agency_id)).billing_mode == "manual"


# --- 3 -> 5 sieges : quantity 2 poussee sur l'item sieges (client mocke) ---------------


async def test_member_growth_pushes_seat_quantity(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = admin.agency_id
    from src.billing import paddle_client

    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)

    # A paddle agency at 3 internal members (billed 0)…
    await _post(client, _envelope("subscription.activated", agency_id=aid))
    for _ in range(2):
        await make_agent(agency_id=aid, role=system_roles["member"])
    h = agent_headers(admin)

    # …accepting two more invitations crosses to 5 members → billed 2.
    for i in range(2):
        invited = await client.post(
            "/agencies/me/invitations",
            headers=h,
            json={"email": f"seat{i}@x.io", "role_id": str(system_roles["member"].id)},
        )
        assert invited.status_code == 201, invited.text
        token = (
            await db_session.execute(
                select(AgentInvitation.token).where(AgentInvitation.email == f"seat{i}@x.io")
            )
        ).scalar_one()
        accepted = await client.post(
            "/agencies/invitations/accept",
            json={
                "token": token,
                "password": "pw12345678",
                "first_name": "New",
                "last_name": f"Member{i}",
            },
        )
        assert accepted.status_code == 200, accepted.text

    assert push.await_count == 2  # one push per acceptance
    sub_id, kwargs = push.await_args.args[0], push.await_args.kwargs
    assert sub_id == "sub_123"
    assert kwargs["proration_billing_mode"] == "prorated_immediately"
    seat_item = next(
        i for i in kwargs["items"] if i["price_id"] == PRICE_IDS["seat_cabinet_mensuel"]
    )
    assert seat_item["quantity"] == 2  # 5 members − 3 included


# --- canceled conserve plan et converted_at --------------------------------------------


async def test_canceled_keeps_plan_and_converted_at(
    client: AsyncClient, db_session: AsyncSession, admin: Agent
) -> None:
    aid = admin.agency_id
    await _post(client, _envelope("subscription.activated", agency_id=aid))
    converted_at = (await _agency(db_session, aid)).converted_at

    resp = await _post(client, _envelope("subscription.canceled", agency_id=aid))
    assert resp.json()["status"] == "processed"
    agency = await _agency(db_session, aid)
    assert agency.billing_status == "canceled"
    assert agency.plan == "cabinet"  # historical facts survive
    assert agency.converted_at == converted_at


# --- desordre : canceled AVANT activated converge --------------------------------------


async def test_out_of_order_canceled_before_activated_converges(
    client: AsyncClient, db_session: AsyncSession, admin: Agent
) -> None:
    aid = admin.agency_id
    t1 = datetime.now(UTC) - timedelta(hours=2)  # activation instant (older)
    t2 = datetime.now(UTC) - timedelta(hours=1)  # cancellation instant (newer)

    # Paddle delivers the CANCELLATION first…
    first = await _post(
        client,
        _envelope("subscription.canceled", agency_id=aid, occurred_at=t2),
    )
    # (manual guard: canceled alone cannot establish the link → ignored)
    assert first.json()["status"] == "ignored"
    # …then the older activation arrives.
    second = await _post(
        client,
        _envelope("subscription.activated", agency_id=aid, occurred_at=t1),
    )
    assert second.json()["status"] == "processed"
    agency = await _agency(db_session, aid)
    # Converged: the conversion facts are posed…
    assert agency.billing_mode == "paddle"
    assert agency.converted_at == t1
    assert agency.plan == "cabinet"
    # …and the STALE activated did not resurrect an "active" status over the
    # newer cancellation event already on file.
    assert agency.billing_status != "active"


# --- checkout : custom_data porte agency_id (client mocke) -----------------------------


async def test_checkout_builds_transaction_with_agency_custom_data(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = admin.agency_id
    from src.billing import paddle_client

    create = AsyncMock(return_value={"id": "txn_42"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create)

    resp = await client.post(
        "/billing/checkout",
        headers=agent_headers(admin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"transaction_id": "txn_42", "paddle_env": "sandbox"}
    kwargs = create.await_args.kwargs
    assert kwargs["custom_data"] == {"agency_id": str(aid)}
    assert kwargs["items"][0] == {"price_id": PRICE_IDS["cabinet_mensuel"], "quantity": 1}


# --- plafonds par plan : jamais une quantity au-dela du cap ----------------------------


async def test_seat_sync_never_pushes_beyond_the_plan_cap(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: the invitation seat gate blocks growth beyond the
    cap, but even a DB-grown roster (support gesture, bug) must never push a
    beyond-cap quantity to Paddle — alert, no call."""
    from src.billing import paddle_client
    from src.billing.billing_manager import BillingManager

    aid = admin.agency_id
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    await _post(client, _envelope("subscription.activated", agency_id=aid))
    # 6 internal members on a cabinet plan (cap 5) — grown OUTSIDE the gate.
    for _ in range(5):
        await make_agent(agency_id=aid, role=system_roles["member"])

    with caplog.at_level("ERROR"):
        await BillingManager(db_session).sync_seat_quantity(aid, increase=True)
    push.assert_not_awaited()
    assert any("exceed" in r.message for r in caplog.records)


async def test_checkout_refuses_a_plan_below_current_members(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.billing import paddle_client

    aid = admin.agency_id
    create = AsyncMock(return_value={"id": "txn_1"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create)
    for _ in range(5):  # 6 members > cabinet cap (5)
        await make_agent(agency_id=aid, role=system_roles["member"])

    resp = await client.post(
        "/billing/checkout",
        headers=agent_headers(admin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "billing.plan_capacity_exceeded"
    create.assert_not_awaited()
    # The larger plan (cap 10) takes them fine.
    ok = await client.post(
        "/billing/checkout",
        headers=agent_headers(admin),
        json={"plan": "agence", "billing_cycle": "mensuel"},
    )
    assert ok.status_code == 200, ok.text


# --- gestion d'abonnement in-app (GET/cancel/resume/payment-method) --------------------


def _paddle_subscription_payload(*, scheduled_cancel: str | None = None) -> dict[str, Any]:
    """A realistic Paddle GET /subscriptions payload for the mocks."""
    return {
        "id": "sub_123",
        "status": "active",
        "currency_code": "EUR",
        "next_billed_at": "2026-08-12T18:59:19Z",
        "current_billing_period": {"ends_at": "2026-08-12T18:59:19Z"},
        "scheduled_change": (
            {"action": "cancel", "effective_at": scheduled_cancel} if scheduled_cancel else None
        ),
        "items": [
            {
                "quantity": 1,
                "price": {"id": PRICE_IDS["cabinet_mensuel"], "unit_price": {"amount": "9900"}},
            },
            {
                "quantity": 2,
                "price": {
                    "id": PRICE_IDS["seat_cabinet_mensuel"],
                    "unit_price": {"amount": "3500"},
                },
            },
        ],
        "next_transaction": {"details": {"totals": {"grand_total": "16900"}}},
    }


async def _activate(client: AsyncClient, agency_id: uuid.UUID) -> None:
    await _post(client, _envelope("subscription.activated", agency_id=agency_id))


@pytest.fixture(autouse=True)
def _clear_subscription_cache(monkeypatch: pytest.MonkeyPatch):
    from src.billing import billing_manager, paddle_client

    billing_manager._SUBSCRIPTION_CACHE.clear()
    billing_manager._CATALOG_PRICES_CACHE = None
    # The 409/TRIAL state now fetches the catalog: NO test may reach the
    # network — empty catalog by default, tests that need prices remock.
    monkeypatch.setattr(paddle_client.PaddleClient, "list_prices", AsyncMock(return_value=[]))
    yield
    billing_manager._SUBSCRIPTION_CACHE.clear()
    billing_manager._CATALOG_PRICES_CACHE = None


async def test_management_endpoints_are_409_on_a_manual_agency(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    h = agent_headers(admin)  # manual agency (never activated)
    for method, url in (
        ("GET", "/billing/subscription"),
        ("POST", "/billing/subscription/cancel"),
        ("POST", "/billing/subscription/resume"),
        ("POST", "/billing/payment-method/update"),
    ):
        resp = await client.request(method, url, headers=h)
        assert resp.status_code == 409, (url, resp.text)
        assert resp.json()["code"] == "billing.not_paddle_managed", url


async def test_subscription_state_is_assembled_from_one_cached_call(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.billing import paddle_client

    aid = admin.agency_id
    await _activate(client, aid)
    get_sub = AsyncMock(return_value=_paddle_subscription_payload())
    monkeypatch.setattr(paddle_client.PaddleClient, "get_subscription", get_sub)

    h = agent_headers(admin)
    state = (await client.get("/billing/subscription", headers=h)).json()
    assert state["plan"] == "cabinet" and state["billing_cycle"] == "mensuel"
    assert state["billing_status"] == "active" and state["currency"] == "EUR"
    assert state["seats_billed"] == 0  # 1 member, 3 included — derived live
    assert state["base_unit_price"] == "99"  # money as strings, from Paddle items
    assert state["seat_unit_price"] == "35"
    assert state["next_billed_at"].startswith("2026-08-12")
    assert state["next_payment_amount"] == "169"
    assert state["scheduled_cancel_at"] is None

    # Second read within the TTL: served from the cache, ONE Paddle call.
    await client.get("/billing/subscription", headers=h)
    assert get_sub.await_count == 1


async def test_cancel_schedules_period_end_and_resume_erases_it(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.billing import paddle_client

    aid = admin.agency_id
    await _activate(client, aid)
    h = agent_headers(admin)
    ends = "2026-08-12T18:59:19Z"

    cancel = AsyncMock(return_value=_paddle_subscription_payload(scheduled_cancel=ends))
    monkeypatch.setattr(paddle_client.PaddleClient, "cancel_subscription_at_period_end", cancel)
    get_sub = AsyncMock(return_value=_paddle_subscription_payload(scheduled_cancel=ends))
    monkeypatch.setattr(paddle_client.PaddleClient, "get_subscription", get_sub)

    cancelled = await client.post("/billing/subscription/cancel", headers=h)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["ends_at"].startswith("2026-08-12")  # "se termine le X"
    cancel.assert_awaited_once_with("sub_123")

    # The page now SHOWS the scheduled end (cache was invalidated by cancel).
    state = (await client.get("/billing/subscription", headers=h)).json()
    assert state["scheduled_cancel_at"].startswith("2026-08-12")

    # Resume erases it — the gesture that saves the regrets.
    resume = AsyncMock(return_value=_paddle_subscription_payload(scheduled_cancel=None))
    monkeypatch.setattr(paddle_client.PaddleClient, "remove_scheduled_change", resume)
    resumed = await client.post("/billing/subscription/resume", headers=h)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["scheduled_cancel_at"] is None
    resume.assert_awaited_once_with("sub_123")


async def test_payment_method_update_returns_the_special_transaction(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.billing import paddle_client

    aid = admin.agency_id
    await _activate(client, aid)
    special = AsyncMock(return_value={"id": "txn_pmu_1"})
    monkeypatch.setattr(
        paddle_client.PaddleClient, "get_payment_method_update_transaction", special
    )
    resp = await client.post("/billing/payment-method/update", headers=agent_headers(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"transaction_id": "txn_pmu_1", "paddle_env": "sandbox"}
    special.assert_awaited_once_with("sub_123")


# --- re-souscription d'une agence canceled ---------------------------------------------


async def test_checkout_reopens_for_a_canceled_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """canceled = dead subscription: the re-subscription path MUST open
    (new transaction, full new lifecycle) — the kept plan/converted_at are
    history, not a manual conversion; an ALIVE subscription still refuses."""
    from src.billing import paddle_client

    aid = admin.agency_id
    create = AsyncMock(return_value={"id": "txn_resub"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create)
    h = agent_headers(admin)

    await _post(client, _envelope("subscription.activated", agency_id=aid))
    # Alive: still refused.
    alive = await client.post(
        "/billing/checkout", headers=h, json={"plan": "cabinet", "billing_cycle": "mensuel"}
    )
    assert alive.status_code == 409 and alive.json()["code"] == "billing.already_subscribed"

    await _post(client, _envelope("subscription.canceled", agency_id=aid))
    # Dead: the door opens — a NEW transaction carrying the agency link.
    resub = await client.post(
        "/billing/checkout", headers=h, json={"plan": "cabinet", "billing_cycle": "mensuel"}
    )
    assert resub.status_code == 200, resub.text
    assert resub.json()["transaction_id"] == "txn_resub"
    assert create.await_args.kwargs["custom_data"] == {"agency_id": str(aid)}


async def test_resubscription_activated_relinks_without_reemitting(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
) -> None:
    """The activated of a RE-subscription on an already-converted agency:
    status back to active, NEW subscription adopted, past_due erased,
    converted_at NEVER overwritten, agency.converted NOT re-emitted (Eric's
    stats would count double) — and the new plan's facts are refreshed."""
    aid = admin.agency_id
    t0 = datetime.now(UTC) - timedelta(days=90)

    await _post(client, _envelope("subscription.activated", agency_id=aid, occurred_at=t0))
    first_converted_at = (await _agency(db_session, aid)).converted_at
    await _post(
        client,
        _envelope(
            "subscription.past_due",
            agency_id=aid,
            occurred_at=t0 + timedelta(days=30),
        ),
    )
    await _post(
        client,
        _envelope(
            "subscription.canceled",
            agency_id=aid,
            occurred_at=t0 + timedelta(days=45),
        ),
    )

    # Re-subscription: NEW subscription, DIFFERENT plan (agence, annual).
    resub = await _post(
        client,
        _envelope(
            "subscription.activated",
            agency_id=aid,
            subscription_id="sub_789",
            items=[{"price": {"id": PRICE_IDS["agence_annuel"]}, "quantity": 1}],
        ),
    )
    assert resub.json()["status"] == "processed"

    agency = await _agency(db_session, aid)
    assert agency.billing_status == "active"
    assert agency.paddle_subscription_id == "sub_789"  # the DEAD sub let go
    assert agency.past_due_since is None
    assert agency.converted_at == first_converted_at  # history, untouched
    assert agency.plan == "agence" and agency.billing_cycle == "annuel"  # facts refreshed
    assert agency.seat_price_eur == 25
    emitted = (
        await db_session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(UsageEvent.agency_id == aid, UsageEvent.event_type == "agency.converted")
        )
    ).scalar_one()
    assert emitted == 1  # once per life, not per subscription


async def test_resume_on_a_dead_subscription_is_a_distinct_409(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.billing import paddle_client

    aid = admin.agency_id
    remove = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "remove_scheduled_change", remove)

    await _post(client, _envelope("subscription.activated", agency_id=aid))
    await _post(client, _envelope("subscription.canceled", agency_id=aid))

    resp = await client.post("/billing/subscription/resume", headers=agent_headers(admin))
    assert resp.status_code == 409
    assert resp.json()["code"] == "billing.subscription_ended"  # not nothing_scheduled
    remove.assert_not_awaited()  # refused BEFORE any Paddle call


# --- collision de customer (piege n°8) : 200 + alerte + zero ecriture ------------------


async def test_shared_customer_collision_is_200_alert_zero_write(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Paddle dedups customers BY EMAIL account-wide: a second agency paying
    with the same billing email re-uses the ctm_ of the first. The unique
    link (one customer = one agency — the right rule) fires; the handler
    turns it into 200 + strong alert + ZERO write (never a 500, Paddle's
    retries stop, the event is stored — a human decides)."""
    first = admin.agency_id
    other_admin = await make_agent(role=system_roles["admin"])
    second = other_admin.agency_id

    # Agency 1 converts and takes the customer.
    await _post(client, _envelope("subscription.activated", agency_id=first))

    # Agency 2 pays with the SAME email → same ctm_123, new subscription.
    with caplog.at_level("ERROR"):
        resp = await _post(
            client,
            _envelope("subscription.activated", agency_id=second, subscription_id="sub_456"),
        )
    assert resp.status_code == 200 and resp.json()["status"] == "ignored"
    assert any("VIOLATES a link constraint" in r.message for r in caplog.records)

    # ZERO write on agency 2: still a virgin manual trial.
    agency = await _agency(db_session, second)
    assert agency.billing_mode == "manual" and agency.converted_at is None
    assert agency.paddle_customer_id is None and agency.paddle_subscription_id is None
    assert agency.billing_status is None
    # Its conversion signal was rolled back with everything else…
    emitted = (
        await db_session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(UsageEvent.agency_id == second, UsageEvent.event_type == "agency.converted")
        )
    ).scalar_one()
    assert emitted == 0
    # …but the EVENT is in table (nothing lost, replayable once settled).
    stored = (
        await db_session.execute(
            select(func.count())
            .select_from(PaddleWebhookEvent)
            .where(PaddleWebhookEvent.agency_id == second)
        )
    ).scalar_one()
    assert stored == 1
    # And agency 1 kept its link untouched.
    keeper = await _agency(db_session, first)
    assert keeper.paddle_customer_id == "ctm_123"


# --- catalog_prices : la grille publique servie a froid --------------------------------


def _paddle_prices_payload() -> list[dict[str, Any]]:
    """The 8 catalog prices as GET /prices returns them (2026-07 grid)."""
    amounts = {
        "cabinet_mensuel": "9900",
        "cabinet_annuel": "99000",
        "agence_mensuel": "12900",
        "agence_annuel": "129000",
        "seat_cabinet_mensuel": "3500",
        "seat_cabinet_annuel": "35000",
        "seat_agence_mensuel": "2500",
        "seat_agence_annuel": "25000",
    }
    return [
        {"id": PRICE_IDS[key], "unit_price": {"amount": amount, "currency_code": "EUR"}}
        for key, amount in amounts.items()
    ]


async def test_catalog_prices_block_served_from_one_long_cached_call(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.billing import paddle_client

    aid = admin.agency_id
    await _activate(client, aid)
    get_sub = AsyncMock(return_value=_paddle_subscription_payload())
    monkeypatch.setattr(paddle_client.PaddleClient, "get_subscription", get_sub)
    list_prices = AsyncMock(return_value=_paddle_prices_payload())
    monkeypatch.setattr(paddle_client.PaddleClient, "list_prices", list_prices)

    h = agent_headers(admin)
    state = (await client.get("/billing/subscription", headers=h)).json()
    catalog = state["catalog_prices"]
    assert catalog["currency"] == "EUR"
    # Unit prices as STRINGS (the costs rule everywhere), the whole grid.
    assert catalog["cabinet"] == {
        "monthly": {"base": "99", "seat": "35"},
        "annual": {"base": "990", "seat": "350"},
    }
    assert catalog["agence"] == {
        "monthly": {"base": "129", "seat": "25"},
        "annual": {"base": "1290", "seat": "250"},
    }
    # LONG cache: a second read costs zero extra Paddle call (immutable
    # prices — a rotation means new ids, new env, fresh cache).
    await client.get("/billing/subscription", headers=h)
    assert list_prices.await_count == 1


async def test_catalog_prices_is_null_when_paddle_is_unreachable(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Display prices never cost a 500: the front keeps its SWR/skeleton."""
    from src.billing import paddle_client
    from src.billing.paddle_client import PaddleApiError

    aid = admin.agency_id
    await _activate(client, aid)
    monkeypatch.setattr(
        paddle_client.PaddleClient,
        "get_subscription",
        AsyncMock(return_value=_paddle_subscription_payload()),
    )
    monkeypatch.setattr(
        paddle_client.PaddleClient,
        "list_prices",
        AsyncMock(side_effect=PaddleApiError(503, "down")),
    )
    resp = await client.get("/billing/subscription", headers=agent_headers(admin))
    assert resp.status_code == 200, resp.text  # never a 500 for display prices
    assert resp.json()["catalog_prices"] is None


async def test_trial_409_carries_the_plan_card_material(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 409 IS the front's trial state: trial_ends_at, checkout_enabled
    and the priced grid, so the pricing page renders cold."""
    from src.billing import paddle_client

    monkeypatch.setattr(
        paddle_client.PaddleClient,
        "list_prices",
        AsyncMock(return_value=_paddle_prices_payload()),
    )
    resp = await client.get("/billing/subscription", headers=agent_headers(admin))
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "billing.not_paddle_managed"
    params = body["params"]
    assert set(params) == {"trial_ends_at", "checkout_enabled", "catalog_prices"}
    assert params["checkout_enabled"] is True  # open in this harness
    assert params["catalog_prices"]["cabinet"]["monthly"] == {"base": "99", "seat": "35"}


# --- kill switch d'offre : BILLING_CHECKOUT_ENABLED ------------------------------------


def _close_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_CHECKOUT_ENABLED", "false")
    get_settings.cache_clear()


async def test_disabled_checkout_is_409_without_any_paddle_call(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.billing import paddle_client

    create = AsyncMock(return_value={"id": "txn_never"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create)
    _close_checkout(monkeypatch)

    resp = await client.post(
        "/billing/checkout",
        headers=agent_headers(admin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "billing.checkout_disabled"
    create.assert_not_awaited()  # the mock attests: Paddle was never reached


async def test_disabled_checkout_keeps_management_open_for_a_converted_agency(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch closes the ENTRANCE, never the management of the existing:
    a converted agency keeps state, cancel, resume and payment method — and
    the webhook that converted it flowed while the offer was CLOSED (a living
    subscription keeps living)."""
    from src.billing import paddle_client

    aid = admin.agency_id
    _close_checkout(monkeypatch)
    await _activate(client, aid)  # webhooks stay live despite the switch
    h = agent_headers(admin)
    ends = "2026-08-12T18:59:19Z"

    get_sub = AsyncMock(return_value=_paddle_subscription_payload())
    monkeypatch.setattr(paddle_client.PaddleClient, "get_subscription", get_sub)
    cancel = AsyncMock(return_value=_paddle_subscription_payload(scheduled_cancel=ends))
    monkeypatch.setattr(paddle_client.PaddleClient, "cancel_subscription_at_period_end", cancel)
    resume = AsyncMock(return_value=_paddle_subscription_payload(scheduled_cancel=None))
    monkeypatch.setattr(paddle_client.PaddleClient, "remove_scheduled_change", resume)
    special = AsyncMock(return_value={"id": "txn_pmu_2"})
    monkeypatch.setattr(
        paddle_client.PaddleClient, "get_payment_method_update_transaction", special
    )

    state = await client.get("/billing/subscription", headers=h)
    assert state.status_code == 200, state.text
    assert state.json()["checkout_enabled"] is False  # the front's "Arrive bientot"
    assert (await client.post("/billing/subscription/cancel", headers=h)).status_code == 200
    assert (await client.post("/billing/subscription/resume", headers=h)).status_code == 200
    assert (await client.post("/billing/payment-method/update", headers=h)).status_code == 200


async def test_subscription_state_exposes_the_checkout_flag(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.billing import paddle_client

    await _activate(client, admin.agency_id)
    get_sub = AsyncMock(return_value=_paddle_subscription_payload())
    monkeypatch.setattr(paddle_client.PaddleClient, "get_subscription", get_sub)
    state = (await client.get("/billing/subscription", headers=agent_headers(admin))).json()
    assert state["checkout_enabled"] is True  # open in this harness


async def test_updated_webhook_with_scheduled_change_flows_untouched(
    client: AsyncClient, db_session: AsyncSession, admin: Agent
) -> None:
    """subscription.updated already carries scheduled_change on a scheduled
    cancellation — the handler processes it without breakage: status follows
    the payload, conversion facts untouched (the page reads the schedule live
    from Paddle, nothing to store)."""
    aid = admin.agency_id
    await _activate(client, aid)
    envelope = _envelope(
        "subscription.updated",
        agency_id=aid,
        items=[{"price": {"id": PRICE_IDS["cabinet_mensuel"]}, "quantity": 1}],
        status="active",
    )
    envelope["data"]["scheduled_change"] = {
        "action": "cancel",
        "effective_at": "2026-08-12T18:59:19Z",
    }
    resp = await _post(client, envelope)
    assert resp.status_code == 200 and resp.json()["status"] == "processed"
    agency = await _agency(db_session, aid)
    assert agency.billing_status == "active"  # still active until it takes effect
    assert agency.plan == "cabinet" and agency.converted_at is not None


# --- resume : le rattrapage de quantity (bug du test manuel 17/07) ---------------------


def _sub_payload_with_seats(seat_qty: int) -> dict[str, Any]:
    """A live subscription payload whose seat item carries `seat_qty`
    (0 = no seat item) — what remove_scheduled_change echoes back."""
    items: list[dict[str, Any]] = [
        {
            "quantity": 1,
            "price": {"id": PRICE_IDS["cabinet_mensuel"], "unit_price": {"amount": "9900"}},
        }
    ]
    if seat_qty:
        items.append(
            {
                "quantity": seat_qty,
                "price": {
                    "id": PRICE_IDS["seat_cabinet_mensuel"],
                    "unit_price": {"amount": "3500"},
                },
            }
        )
    return {
        "id": "sub_123",
        "status": "active",
        "currency_code": "EUR",
        "next_billed_at": "2026-08-15T12:00:00Z",
        "current_billing_period": {"ends_at": "2026-08-15T12:00:00Z"},
        "scheduled_change": None,
        "items": items,
        "next_transaction": {"details": {"totals": {"grand_total": "9900"}}},
    }


async def test_resume_catches_up_a_missed_seat_down(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LE scenario du bug : un retrait pendant l'annulation programmee a
    laisse Paddle a qty=2 alors que billed=0. Au resume, le scheduled
    change vient de tomber -> le rattrapage pousse la quantity derivee,
    en full_next_billing_period (la meme regle qu'un retrait)."""
    from src.billing import paddle_client

    aid = admin.agency_id
    await _activate(client, aid)  # 1 membre -> billed 0
    resume = AsyncMock(return_value=_sub_payload_with_seats(2))  # Paddle croit 2 sieges
    monkeypatch.setattr(paddle_client.PaddleClient, "remove_scheduled_change", resume)
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    refetch = AsyncMock(return_value=_sub_payload_with_seats(0))
    monkeypatch.setattr(paddle_client.PaddleClient, "get_subscription", refetch)

    resp = await client.post("/billing/subscription/resume", headers=agent_headers(admin))
    assert resp.status_code == 200, resp.text
    push.assert_awaited_once()
    sub_id, kwargs = push.await_args.args[0], push.await_args.kwargs
    assert sub_id == "sub_123"
    assert kwargs["proration_billing_mode"] == "full_next_billing_period"  # a la baisse
    # billed 0 -> plus d'item siege du tout
    assert all(i["price_id"] != PRICE_IDS["seat_cabinet_mensuel"] for i in kwargs["items"])
    refetch.assert_awaited()  # la reponse reflete l'etat APRES rattrapage


async def test_resume_catches_up_a_missed_seat_up(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'autre sens (rare) : des sieges ajoutes pendant la fenetre annulee
    -> le rattrapage MONTE, prorata immediat (la meme regle qu'un ajout)."""
    from src.billing import paddle_client

    aid = admin.agency_id
    await _activate(client, aid)
    for i in range(4):  # 5 membres -> billed 2
        await make_agent(agency_id=aid, role=system_roles["member"], email=f"cu{i}@x.io")
    resume = AsyncMock(return_value=_sub_payload_with_seats(0))  # Paddle croit 0
    monkeypatch.setattr(paddle_client.PaddleClient, "remove_scheduled_change", resume)
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    monkeypatch.setattr(
        paddle_client.PaddleClient,
        "get_subscription",
        AsyncMock(return_value=_sub_payload_with_seats(2)),
    )

    resp = await client.post("/billing/subscription/resume", headers=agent_headers(admin))
    assert resp.status_code == 200, resp.text
    push.assert_awaited_once()
    kwargs = push.await_args.kwargs
    assert kwargs["proration_billing_mode"] == "prorated_immediately"  # a la hausse
    seat_item = next(
        i for i in kwargs["items"] if i["price_id"] == PRICE_IDS["seat_cabinet_mensuel"]
    )
    assert seat_item["quantity"] == 2


async def test_resume_pushes_nothing_when_quantities_match(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.billing import paddle_client

    aid = admin.agency_id
    await _activate(client, aid)  # billed 0
    resume = AsyncMock(return_value=_sub_payload_with_seats(0))  # Paddle dit 0 aussi
    monkeypatch.setattr(paddle_client.PaddleClient, "remove_scheduled_change", resume)
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)

    resp = await client.post("/billing/subscription/resume", headers=agent_headers(admin))
    assert resp.status_code == 200, resp.text
    push.assert_not_awaited()  # rien ne diverge -> aucun push


# --- le self-serve pour les clients reels : la CONVERSION est le discriminant ----------
# (decision 2026-07-17 : le mur "gere avec l'equipe" ne vise QUE la convertie
#  manuelle ; une agence en essai, quel que soit billing_mode, voit les cartes
#  et peut payer. Domiciliation Bulgarie / Reside Paraguay = manual non
#  converties : le checkout leur est ouvert.)


async def test_manual_trial_agency_gets_trial_409_and_open_checkout(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le cas Domiciliation Bulgarie : manual, converted_at NULL, essai en
    cours -> le 409 ESSAI complet (jamais le mur), et le checkout passe."""
    from src.billing import paddle_client

    await db_session.execute(
        update(Agency)
        .where(Agency.id == admin.agency_id)
        .values(trial_ends_at=datetime.now(UTC) + timedelta(days=30))
    )
    await db_session.commit()
    monkeypatch.setattr(
        paddle_client.PaddleClient,
        "list_prices",
        AsyncMock(return_value=_paddle_prices_payload()),
    )
    resp = await client.get("/billing/subscription", headers=agent_headers(admin))
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "billing.not_paddle_managed"  # l'essai, PAS le mur
    assert set(body["params"]) == {"trial_ends_at", "checkout_enabled", "catalog_prices"}
    assert body["params"]["trial_ends_at"] is not None

    create = AsyncMock(return_value={"id": "txn_db"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create)
    checkout = await client.post(
        "/billing/checkout",
        headers=agent_headers(admin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert checkout.status_code == 200, checkout.text  # la porte est OUVERTE


async def test_payment_converts_the_manual_trial_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
) -> None:
    """Sa conversion par paiement : l'activated bascule billing_mode=paddle,
    pose converted_at, et emet agency.converted (le chemin nominal passe le
    garde manual parce que converted_at est NULL)."""
    from shared.models.usage import UsageEvent

    aid = admin.agency_id
    occurred = datetime.now(UTC)
    resp = await _post(
        client,
        _envelope(
            "subscription.activated",
            agency_id=aid,
            occurred_at=occurred,
            subscription_id="sub_conv",
        ),
    )
    assert resp.json()["status"] == "processed"
    db_session.expire_all()
    agency = await db_session.get(Agency, aid)
    assert agency is not None
    assert agency.billing_mode == "paddle"
    assert agency.billing_status == "active"
    assert agency.converted_at == occurred
    events = (
        (
            await db_session.execute(
                select(UsageEvent).where(
                    UsageEvent.agency_id == aid, UsageEvent.event_type == "agency.converted"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


async def test_manually_converted_agency_gets_the_wall(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Le SEUL cas du mur : la convertie manuelle (Nidria Demo, sur-mesure).
    GET -> manually_billed, checkout -> manually_billed. Et le discriminant
    est bien converted_at : une conversion posee SANS plan (geste superadmin)
    declenche le mur pareil."""
    await db_session.execute(
        update(Agency)
        .where(Agency.id == admin.agency_id)
        .values(converted_at=datetime.now(UTC))  # convertie, plan meme pas pose
    )
    await db_session.commit()
    resp = await client.get("/billing/subscription", headers=agent_headers(admin))
    assert resp.status_code == 409
    assert resp.json()["code"] == "billing.manually_billed"  # le mur, pas l'essai
    checkout = await client.post(
        "/billing/checkout",
        headers=agent_headers(admin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert checkout.status_code == 409
    assert checkout.json()["code"] == "billing.manually_billed"
    # les webhooks residuels : no-op + alerte — test_manual_agency_is_never_
    # written_by_webhooks tient (la convertie manuelle reste intouchable).
