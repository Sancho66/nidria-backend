"""Lot lecteur (08/08) — the READER SEAT model.

Covers, against the real app:
(a) the seat TYPE at the contract: the invitation carries it, the member
    inherits it at acceptance, the listing serves it;
(b) trial: 3 seats TOTAL across types (arbitrage), and the pool gestures
    are refused without an active subscription;
(c) active subscription: managers keep the roster MIRROR (no ceiling),
    readers land on PURCHASED pool seats only (409 named when empty) —
    and never eat the included tier (`billed` counts managers);
(d) the GROUPED gesture (cas Nicolas): +7 readers = ONE Paddle
    update_subscription_items (one proration), same at removal — with
    the in-use and exceed-pool guards;
(e) the manager quantity is refused BY CONTRACT (mirror decision S1);
(f) the seat-type flip is a traced admin gesture with its gates;
(g) a reader is NEVER a designated actor (owner, bulk owner, step
    responsible, step validator, template default);
(h) the read-only-role coherence (by capability, clone-proof);
(i) the webhook echo guard compares BOTH quantities;
(j) the checkout adopts trial readers (billed from day one) and the
    manual conversion poses the pool.
"""

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any
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
from shared.models.usage import UsageEvent
from src.billing import paddle_client
from src.core.config import get_settings
from src.core.security import hash_password
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
    "seat_reader_mensuel": "pri_seat_reader_m",
    "seat_reader_annuel": "pri_seat_reader_a",
}


@pytest.fixture(autouse=True)
def paddle_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", SECRET)
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
    client: AsyncClient, superadmin: Agent, headers: AuthHeaders, agency_id: uuid.UUID
) -> None:
    """Manual conversion (Eric's PATCH): active subscription, no Paddle."""
    response = await client.patch(
        f"/agencies/{agency_id}/subscription",
        headers=headers(superadmin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert response.status_code == 200, response.text


async def _paddle_activate(db: AsyncSession, agency_id: uuid.UUID) -> None:
    """Wire a LIVE paddle subscription directly (the webhook's outcome)."""
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    agency.plan = "cabinet"
    agency.billing_cycle = "mensuel"
    agency.converted_at = datetime.now(UTC)
    agency.billing_mode = "paddle"
    agency.billing_status = "active"
    agency.paddle_subscription_id = f"sub_{uuid.uuid4().hex[:10]}"
    await db.commit()


async def _add_readers(
    db: AsyncSession, agency_id: uuid.UUID, role: Role, count: int
) -> list[Agent]:
    rows = []
    for i in range(count):
        row = Agent(
            agency_id=agency_id,
            role_id=role.id,
            email=f"reader-{uuid.uuid4().hex[:10]}@example.com",
            first_name="Lecteur",
            last_name=f"N{i}",
            password_hash=hash_password("ReaderPassword1!"),
            is_external=False,
            seat_type="reader",
        )
        db.add(row)
        rows.append(row)
    await db.commit()
    return rows


def _invite(
    client: AsyncClient,
    headers: dict[str, str],
    role: Role,
    email: str,
    seat_type: str = "reader",
):
    return client.post(
        "/agencies/me/invitations",
        headers=headers,
        json={"email": email, "role_id": str(role.id), "seat_type": seat_type},
    )


def _sign(raw: bytes) -> str:
    ts = int(time.time())
    digest = hmac.new(SECRET.encode(), f"{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={digest}"


async def _post_webhook(client: AsyncClient, envelope: dict[str, Any]):
    raw = json.dumps(envelope).encode()
    return await client.post(
        "/billing/webhooks/paddle",
        content=raw,
        headers={"content-type": "application/json", "Paddle-Signature": _sign(raw)},
    )


# --- (a) the type at the contract ------------------------------------------------------


async def test_invitation_carries_the_seat_type_to_the_member(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    """Invite → the invitation carries the type; accept → the member wears
    it; the annuaire serves it. Trial: a reader fits inside the 3-total,
    no pool needed."""
    headers = agent_headers(admin)
    created = await _invite(client, headers, system_roles["viewer"], "lectrice@example.com")
    assert created.status_code == 201, created.text
    assert created.json()["seat_type"] == "reader"

    invitation = (
        await db_session.execute(
            select(AgentInvitation).where(AgentInvitation.email == "lectrice@example.com")
        )
    ).scalar_one()
    accepted = await client.post(
        "/agencies/invitations/accept",
        json={
            "token": invitation.token,
            "password": "ReaderPassword1!",
            "first_name": "Nadia",
            "last_name": "Lectrice",
        },
    )
    assert accepted.status_code == 200, accepted.text

    members = (await client.get("/agencies/me/members", headers=headers)).json()
    by_email = {m["email"]: m for m in members}
    assert by_email["lectrice@example.com"]["seat_type"] == "reader"
    assert by_email[admin.email]["seat_type"] == "manager"

    seats = (await client.get("/agencies/me", headers=headers)).json()["subscription"]["seats"]
    assert seats["members"] == 2 and seats["managers"] == 1
    assert seats["reader"] == {"purchased": 0, "used": 1, "free": 0}  # trial: no pool


# --- (b) trial: 3 TOTAL, pool gestures closed ------------------------------------------


async def test_trial_caps_all_seat_types_at_three_total(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    headers = agent_headers(admin)
    await _add_readers(db_session, admin.agency_id, system_roles["viewer"], 2)  # 3 members total

    blocked = await _invite(client, headers, system_roles["viewer"], "fourth@example.com")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "subscription.seat_limit"  # the TOTAL gate, unchanged

    purchase = await client.post("/billing/seats/add", headers=headers, json={"reader": 5})
    assert purchase.status_code == 409, purchase.text
    assert purchase.json()["code"] == "billing.seats_require_subscription"


# --- (c) active subscription: pool for readers, mirror for managers --------------------


async def test_reader_lands_on_purchased_pool_seats_only(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    headers = agent_headers(admin)
    await _convert(client, superadmin, agent_headers, admin.agency_id)

    # Pool empty: the reader invitation is refused with the NAMED code.
    refused = await _invite(client, headers, system_roles["viewer"], "r1@example.com")
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["code"] == "subscription.reader_seat_limit"
    assert body["params"] == {"purchased": 0, "used": 0}

    # Buy 2 seats (manual agency: the invoice is Eric's gesture, no Paddle).
    bought = await client.post("/billing/seats/add", headers=headers, json={"reader": 2})
    assert bought.status_code == 200, bought.text
    assert bought.json()["reader"] == {"purchased": 2, "used": 0, "free": 2}

    # Invite + accept onto a free seat.
    invited = await _invite(client, headers, system_roles["viewer"], "r1@example.com")
    assert invited.status_code == 201, invited.text
    invitation = (
        await db_session.execute(
            select(AgentInvitation).where(AgentInvitation.email == "r1@example.com")
        )
    ).scalar_one()
    accepted = await client.post(
        "/agencies/invitations/accept",
        json={
            "token": invitation.token,
            "password": "ReaderPassword1!",
            "first_name": "R",
            "last_name": "Un",
        },
    )
    assert accepted.status_code == 200, accepted.text

    seats = (await client.get("/agencies/me", headers=headers)).json()["subscription"]["seats"]
    # The reader NEVER eats the included tier: billed counts managers only.
    assert seats["reader"] == {"purchased": 2, "used": 1, "free": 1}
    assert seats["managers"] == 1 and seats["billed"] == 0 and seats["max"] is None


# --- (d)+(e) the grouped gesture — cas Nicolas -----------------------------------------


async def test_grouped_addition_is_one_paddle_call(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """+7 readers = ONE update_subscription_items: one proration, one
    invoice line — never 7 unit calls."""
    await _paddle_activate(db_session, admin.agency_id)
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)

    response = await client.post(
        "/billing/seats/add", headers=agent_headers(admin), json={"reader": 7}
    )
    assert response.status_code == 200, response.text
    assert response.json()["reader"] == {"purchased": 7, "used": 0, "free": 7}

    assert push.await_count == 1  # LE point du lot: un geste, un appel
    kwargs = push.await_args.kwargs
    assert kwargs["proration_billing_mode"] == "prorated_immediately"
    items = {item["price_id"]: item["quantity"] for item in kwargs["items"]}
    assert items["pri_seat_reader_m"] == 7
    assert items["pri_base_cab_m"] == 1

    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None and agency.reader_seats_purchased == 7


async def test_grouped_removal_guards_then_one_call(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = agent_headers(admin)
    await _paddle_activate(db_session, admin.agency_id)
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.reader_seats_purchased = 7
    await db_session.commit()
    await _add_readers(db_session, admin.agency_id, system_roles["viewer"], 2)
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)

    in_use = await client.post("/billing/seats/remove", headers=headers, json={"reader": 6})
    assert in_use.status_code == 409, in_use.text
    assert in_use.json()["code"] == "billing.reader_seats_in_use"

    beyond = await client.post("/billing/seats/remove", headers=headers, json={"reader": 8})
    assert beyond.status_code == 422, beyond.text
    assert beyond.json()["code"] == "billing.reader_seats_exceed_pool"
    assert push.await_count == 0  # both guards fire BEFORE any Paddle call

    removed = await client.post("/billing/seats/remove", headers=headers, json={"reader": 5})
    assert removed.status_code == 200, removed.text
    assert removed.json()["reader"] == {"purchased": 2, "used": 2, "free": 0}
    assert push.await_count == 1
    kwargs = push.await_args.kwargs
    assert kwargs["proration_billing_mode"] == "full_next_billing_period"
    items = {item["price_id"]: item["quantity"] for item in kwargs["items"]}
    assert items["pri_seat_reader_m"] == 2


async def test_manager_quantity_is_refused_by_contract(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """The contract accepts {manager, reader} but manager answers the
    NAMED 422 (spec S1): manager seats follow the roster mirror."""
    await _convert(client, superadmin, agent_headers, admin.agency_id)
    headers = agent_headers(admin)

    refused = await client.post("/billing/seats/add", headers=headers, json={"manager": 2})
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "billing.manager_seats_follow_roster"

    empty = await client.post("/billing/seats/add", headers=headers, json={})
    assert empty.status_code == 422, empty.text
    assert empty.json()["code"] == "billing.reader_quantity_required"


# --- (f) the traced flip ---------------------------------------------------------------


async def test_seat_type_change_is_a_traced_admin_gesture(
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
    member = await make_agent(
        role=system_roles["viewer"], agency_id=admin.agency_id, email="flip@example.com"
    )

    # Pool empty → the flip to reader is refused (the seat must exist).
    refused = await client.put(
        f"/agencies/me/members/{member.id}/seat-type",
        headers=headers,
        json={"seat_type": "reader"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "subscription.reader_seat_limit"

    bought = await client.post("/billing/seats/add", headers=headers, json={"reader": 1})
    assert bought.status_code == 200, bought.text
    flipped = await client.put(
        f"/agencies/me/members/{member.id}/seat-type",
        headers=headers,
        json={"seat_type": "reader"},
    )
    assert flipped.status_code == 200, flipped.text
    assert flipped.json()["seat_type"] == "reader"

    event = (
        await db_session.execute(
            select(UsageEvent).where(
                UsageEvent.agency_id == admin.agency_id,
                UsageEvent.event_type == "member.seat_type_changed",
            )
        )
    ).scalar_one()
    assert event.details == {"member_id": str(member.id), "from": "manager", "to": "reader"}

    # reader → manager is always open (the mirror bills it).
    back = await client.put(
        f"/agencies/me/members/{member.id}/seat-type",
        headers=headers,
        json={"seat_type": "manager"},
    )
    assert back.status_code == 200, back.text

    # Self-change refused, same rule as the role.
    own = await client.put(
        f"/agencies/me/members/{admin.id}/seat-type",
        headers=headers,
        json={"seat_type": "reader"},
    )
    assert own.status_code == 403, own.text


# --- (h) role coherence, by capability -------------------------------------------------


async def test_reader_role_must_be_read_only(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    headers = agent_headers(admin)

    # Invitation: a reader on a WRITING role (member holds case.edit…) → 422.
    invited = await _invite(client, headers, system_roles["member"], "w@example.com")
    assert invited.status_code == 422, invited.text
    assert invited.json()["code"] == "seat.reader_role_not_read_only"

    # Flip: a member wearing a writing role cannot become a reader as-is.
    member = await make_agent(
        role=system_roles["member"], agency_id=admin.agency_id, email="m@example.com"
    )
    flipped = await client.put(
        f"/agencies/me/members/{member.id}/seat-type",
        headers=headers,
        json={"seat_type": "reader"},
    )
    assert flipped.status_code == 422, flipped.text
    assert flipped.json()["code"] == "seat.reader_role_not_read_only"

    # Role change on an EXISTING reader: widening the role is refused —
    # flip the seat type first.
    reader = await make_agent(
        role=system_roles["viewer"],
        agency_id=admin.agency_id,
        email="r@example.com",
        seat_type="reader",
    )
    widened = await client.put(
        f"/agencies/me/members/{reader.id}/role",
        headers=headers,
        json={"role_id": str(system_roles["member"].id)},
    )
    assert widened.status_code == 422, widened.text
    assert widened.json()["code"] == "seat.reader_role_not_read_only"


# --- (g) never a designated actor ------------------------------------------------------


async def test_reader_is_never_a_designated_actor(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    make_client_case,
    make_journey_template,
    make_template_step,
    system_roles: dict[str, Role],
) -> None:
    headers = agent_headers(admin)
    reader = await make_agent(
        role=system_roles["viewer"],
        agency_id=admin.agency_id,
        email="lecteur@example.com",
        seat_type="reader",
    )
    case = await make_client_case(agency_id=admin.agency_id)

    def _is_reader_actor_error(response) -> None:
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "seat.reader_cannot_be_actor"

    # Owner — the ONE gate covers PATCH, create and bulk alike.
    _is_reader_actor_error(
        await client.patch(
            f"/cases/{case.id}", headers=headers, json={"owner_agent_id": str(reader.id)}
        )
    )
    _is_reader_actor_error(
        await client.post(
            "/cases/bulk-action",
            headers=headers,
            json={
                "action": "set_owner",
                "case_ids": [str(case.id)],
                "owner_agent_id": str(reader.id),
            },
        )
    )

    # Template default (multiplied at every instantiation).
    template = await make_journey_template(agency_id=admin.agency_id)
    _is_reader_actor_error(
        await client.post(
            f"/journeys/{template.id}/steps",
            headers=headers,
            json={"name": "Dossier bancaire", "default_responsible_agent_id": str(reader.id)},
        )
    )

    # Step responsible + validator on a LIVE case journey.
    await make_template_step(template=template)
    assigned = await client.post(
        f"/cases/{case.id}/journey",
        headers=headers,
        json={"journey_template_id": str(template.id)},
    )
    assert assigned.status_code == 201, assigned.text
    progress_id = assigned.json()[0]["id"]
    _is_reader_actor_error(
        await client.put(
            f"/cases/{case.id}/steps/{progress_id}/responsible",
            headers=headers,
            json={"responsible_type": "agent", "responsible_agent_id": str(reader.id)},
        )
    )
    _is_reader_actor_error(
        await client.put(
            f"/cases/{case.id}/steps/{progress_id}/validator",
            headers=headers,
            json={"validated_by_type": "agent", "validated_by_agent_id": str(reader.id)},
        )
    )


# --- (i) webhook echo, ventilated ------------------------------------------------------


async def test_updated_webhook_echo_compares_both_quantities(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
) -> None:
    await _paddle_activate(db_session, admin.agency_id)
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    subscription_id = agency.paddle_subscription_id
    agency.reader_seats_purchased = 2
    await db_session.commit()

    def envelope(reader_quantity: int) -> dict[str, Any]:
        return {
            "event_id": f"evt_{uuid.uuid4().hex[:12]}",
            "event_type": "subscription.updated",
            "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "data": {
                "id": subscription_id,
                "customer_id": "ctm_123",
                "status": "active",
                "custom_data": {"agency_id": str(admin.agency_id)},
                "items": [
                    {"price": {"id": PRICE_IDS["cabinet_mensuel"]}, "quantity": 1},
                    {
                        "price": {"id": PRICE_IDS["seat_reader_mensuel"]},
                        "quantity": reader_quantity,
                    },
                ],
            },
        }

    # The faithful echo (manager 0 = no seat item, reader 2 = the pool).
    faithful = await _post_webhook(client, envelope(2))
    assert faithful.status_code == 200, faithful.text
    assert faithful.json()["status"] == "processed"

    # A diverging reader quantity: alert, NOTHING written.
    diverging = await _post_webhook(client, envelope(5))
    assert diverging.status_code == 200, diverging.text
    assert diverging.json()["status"] == "ignored"


# --- (j) checkout + conversion adopt trial readers -------------------------------------


async def test_checkout_and_conversion_adopt_trial_readers(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trial reader is billed from day one: the checkout carries the
    reader item, the manual conversion poses the pool — never offered by
    accident."""
    await _add_readers(db_session, admin.agency_id, system_roles["viewer"], 1)
    create_txn = AsyncMock(return_value={"id": "txn_reader_1"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create_txn)

    checkout = await client.post(
        "/billing/checkout",
        headers=agent_headers(admin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert checkout.status_code == 200, checkout.text
    items = {i["price_id"]: i["quantity"] for i in create_txn.await_args.kwargs["items"]}
    assert items["pri_seat_reader_m"] == 1  # the trial reader, billed at entry

    await _convert(client, superadmin, agent_headers, admin.agency_id)
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None and agency.reader_seats_purchased == 1
