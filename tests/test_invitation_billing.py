"""Règle unifiée 08/08 — UNE INVITATION = UN PAIEMENT.

The seat is paid at the INVITE gesture (prorated immediately) and returned
at the DELETE gesture — invitation or member — with the décrue landing at
the next cycle. ACCEPTANCE CHANGES NOTHING. One model for both seat types.

Covers, against the real app:
(a) manager invitation → immediate push (mirror counts roster + attente);
    acceptance → ZERO push;
(b) reader invitation → pool auto-buy + push at the gesture; acceptance →
    ZERO push;
(c) cancelling a pending invitation → décrue (manager mirror re-derives,
    reader pool returns its seat), pushed full_next_billing_period, traced
    like a member deletion;
(d) the expiration JOB performs the same delete gesture on dead rows:
    EXPIRED + seats returned + push down (a dead invitation stops costing
    at the next cycle);
(e) trial: the 3-seat cap counts roster + attente, zero Paddle traffic,
    acceptance passes without any capacity error;
(f) the mixed end-to-end case;
(g) fin d'essai, conversion arithmetic (constat B7): a 3-member trial can
    NEVER produce a billed manager at Cabinet conversion (3 included) —
    the only possible conversion cost is the READERS; pending invitations
    of both types count in the first push (A5).
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

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


async def _paddle_activate(db: AsyncSession, agency_id: uuid.UUID) -> None:
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    agency.plan = "cabinet"
    agency.billing_cycle = "mensuel"
    agency.converted_at = datetime.now(UTC)
    agency.billing_mode = "paddle"
    agency.billing_status = "active"
    agency.paddle_subscription_id = f"sub_{uuid.uuid4().hex[:10]}"
    await db.commit()


def _mock_push(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    return push


def _invite(
    client: AsyncClient,
    headers: dict[str, str],
    role: Role,
    email: str,
    seat_type: str = "manager",
):
    return client.post(
        "/agencies/me/invitations",
        headers=headers,
        json={"email": email, "role_id": str(role.id), "seat_type": seat_type},
    )


async def _accept(client: AsyncClient, db: AsyncSession, email: str):
    invitation = (
        await db.execute(select(AgentInvitation).where(AgentInvitation.email == email))
    ).scalar_one()
    return await client.post(
        "/agencies/invitations/accept",
        json={
            "token": invitation.token,
            "password": "InvitePassword1!",
            "first_name": "I",
            "last_name": "N",
        },
    )


def _items(push: AsyncMock) -> dict[str, int]:
    return {i["price_id"]: i["quantity"] for i in push.await_args.kwargs["items"]}


# --- (a) manager: the push leaves at the INVITE, acceptance is silent ------------------


async def test_manager_invitation_pushes_immediately_acceptance_pushes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _paddle_activate(db_session, admin.agency_id)
    for i in range(2):  # 3 managers: the included tier is exactly full
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    push = _mock_push(monkeypatch)
    headers = agent_headers(admin)

    invited = await _invite(client, headers, system_roles["member"], "fourth@example.com")
    assert invited.status_code == 201, invited.text
    push.assert_awaited_once()  # the seat is paid at THIS gesture
    kwargs = push.await_args.kwargs
    assert kwargs["proration_billing_mode"] == "prorated_immediately"
    assert _items(push)["pri_seat_cab_m"] == 1  # 4 committed − 3 included

    push.reset_mock()
    accepted = await _accept(client, db_session, "fourth@example.com")
    assert accepted.status_code == 200, accepted.text
    assert push.await_count == 0  # acceptance is billing-neutral

    seats = (await client.get("/agencies/me", headers=headers)).json()["subscription"]["seats"]
    assert seats["managers"] == 4 and seats["billed"] == 1


# --- (b) reader: pool auto-buy at the invite, acceptance silent ------------------------


async def test_reader_invitation_buys_the_pool_seat_acceptance_pushes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _paddle_activate(db_session, admin.agency_id)
    push = _mock_push(monkeypatch)
    headers = agent_headers(admin)

    invited = await _invite(
        client, headers, system_roles["viewer"], "lect@example.com", seat_type="reader"
    )
    assert invited.status_code == 201, invited.text
    push.assert_awaited_once()
    assert push.await_args.kwargs["proration_billing_mode"] == "prorated_immediately"
    assert _items(push)["pri_seat_reader_m"] == 1  # the auto-bought pool seat
    event = (
        await db_session.execute(
            select(UsageEvent).where(
                UsageEvent.agency_id == admin.agency_id,
                UsageEvent.event_type == "reader_seats.purchased",
            )
        )
    ).scalar_one()
    assert event.details == {"quantity": 1, "pool": 1, "reason": "member_invited"}

    push.reset_mock()
    accepted = await _accept(client, db_session, "lect@example.com")
    assert accepted.status_code == 200, accepted.text
    assert push.await_count == 0
    seats = (await client.get("/agencies/me", headers=headers)).json()["subscription"]["seats"]
    assert seats["reader"] == {"purchased": 1, "used": 1, "free": 0}


# --- (c) cancelling a pending invitation = the delete gesture --------------------------


async def test_cancelling_pending_invitations_returns_both_seat_types(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _paddle_activate(db_session, admin.agency_id)
    for i in range(2):
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    push = _mock_push(monkeypatch)
    headers = agent_headers(admin)
    invited_m = await _invite(client, headers, system_roles["member"], "gest@example.com")
    invited_r = await _invite(
        client, headers, system_roles["viewer"], "lect@example.com", seat_type="reader"
    )
    assert invited_m.status_code == 201 and invited_r.status_code == 201
    push.reset_mock()

    invitations = {
        row.email: row
        for row in ((await db_session.execute(select(AgentInvitation))).scalars().all())
    }
    cancelled_m = await client.delete(
        f"/agencies/me/invitations/{invitations['gest@example.com'].id}", headers=headers
    )
    assert cancelled_m.status_code == 200, cancelled_m.text
    push.assert_awaited_once()  # the mirror re-derives without the pending row
    assert push.await_args.kwargs["proration_billing_mode"] == "full_next_billing_period"
    assert "pri_seat_cab_m" not in _items(push)  # back to 3 committed = included

    push.reset_mock()
    cancelled_r = await client.delete(
        f"/agencies/me/invitations/{invitations['lect@example.com'].id}", headers=headers
    )
    assert cancelled_r.status_code == 200, cancelled_r.text
    push.assert_awaited_once()
    assert push.await_args.kwargs["proration_billing_mode"] == "full_next_billing_period"
    assert "pri_seat_reader_m" not in _items(push)  # the pool followed (0 = absent item)
    released = (
        await db_session.execute(
            select(UsageEvent).where(
                UsageEvent.agency_id == admin.agency_id,
                UsageEvent.event_type == "reader_seats.released",
            )
        )
    ).scalar_one()
    assert released.details == {"quantity": 1, "pool": 0, "reason": "invitation_cancelled"}
    traces = (
        (
            await db_session.execute(
                select(UsageEvent.details).where(
                    UsageEvent.agency_id == admin.agency_id,
                    UsageEvent.event_type == "member.invitation_cancelled",
                )
            )
        )
        .scalars()
        .all()
    )
    assert {t["seat_type"] for t in traces} == {"manager", "reader"}  # same trace, both types

    seats = (await client.get("/agencies/me", headers=headers)).json()["subscription"]["seats"]
    assert seats["members"] == 3 and seats["billed"] == 0
    assert seats["reader"] == {"purchased": 0, "used": 0, "free": 0}


# --- (d) the expiration job performs the delete gesture --------------------------------


async def test_expiration_job_returns_seats_and_pushes_down(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    sync_session_local,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two dead PENDING rows (one per type): the sweep flips them to
    EXPIRED, returns the reader pool seat, traces, and pushes the agency
    DOWN — a dead invitation stops costing at the next cycle."""
    from src.agencies import agencies_jobs

    await _paddle_activate(db_session, admin.agency_id)
    for i in range(2):
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    push = _mock_push(monkeypatch)
    headers = agent_headers(admin)
    assert (
        await _invite(client, headers, system_roles["member"], "gest@example.com")
    ).status_code == 201
    assert (
        await _invite(
            client, headers, system_roles["viewer"], "lect@example.com", seat_type="reader"
        )
    ).status_code == 201
    # Kill their clocks.
    for invitation in (await db_session.execute(select(AgentInvitation))).scalars():
        invitation.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()
    push.reset_mock()
    pushed_down = Mock()
    monkeypatch.setattr(agencies_jobs, "_push_seat_quantities_down", pushed_down)

    def _sweep() -> dict:
        with sync_session_local() as sync_db:
            return agencies_jobs.expire_agent_invitations(sync_db, log=lambda _line: None)

    stats = await asyncio.to_thread(_sweep)
    assert stats == {
        "expired": 2,
        "reader_seats_released": 1,
        "provider_phantoms_purged": 0,
        "agencies_pushed": 1,
    }
    agency_id = admin.agency_id  # capture BEFORE expire_all (async lazy-load trap)
    pushed_down.assert_called_once_with([agency_id])

    db_session.expire_all()  # the sweep wrote through its OWN sync session
    statuses = {
        row.email: row.status
        for row in (await db_session.execute(select(AgentInvitation))).scalars()
    }
    assert set(statuses.values()) == {"expired"}
    released = (
        await db_session.execute(
            select(UsageEvent).where(
                UsageEvent.agency_id == agency_id,
                UsageEvent.event_type == "reader_seats.released",
            )
        )
    ).scalar_one()
    assert released.details["reason"] == "invitation_expired"
    seats = (await client.get("/agencies/me", headers=headers)).json()["subscription"]["seats"]
    # The dead attente is out of EVERY count: 3 managers, pool 0.
    assert seats["members"] == 3 and seats["billed"] == 0
    assert seats["reader"] == {"purchased": 0, "used": 0, "free": 0}


# --- (e) trial: the cap counts attente, zero Paddle ------------------------------------


async def test_trial_cap_counts_pending_invitations_and_never_touches_paddle(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    push = _mock_push(monkeypatch)
    headers = agent_headers(admin)

    assert (
        await _invite(client, headers, system_roles["member"], "g1@example.com")
    ).status_code == 201
    assert (
        await _invite(client, headers, system_roles["viewer"], "l1@example.com", seat_type="reader")
    ).status_code == 201

    # 1 member + 2 pending = 3: the FOURTH seat is refused AT THE INVITE —
    # an invitation IS a seat, N pending can never overshoot the cap.
    blocked = await _invite(client, headers, system_roles["member"], "g2@example.com")
    assert blocked.status_code == 409, blocked.text
    body = blocked.json()
    assert body["code"] == "subscription.seat_limit"
    assert body["params"]["members"] == 3

    # Acceptance passes (attente → roster, totals identical): no capacity 409.
    assert (await _accept(client, db_session, "g1@example.com")).status_code == 200
    assert (await _accept(client, db_session, "l1@example.com")).status_code == 200

    assert push.await_count == 0  # trial: zero Paddle traffic, ever
    seats = (await client.get("/agencies/me", headers=headers)).json()["subscription"]["seats"]
    assert seats["members"] == 3 and seats["max"] == 3
    assert seats["reader"] == {"purchased": 0, "used": 1, "free": 0}


# --- (f) the mixed end-to-end case -----------------------------------------------------


async def test_mixed_lifecycle_end_to_end(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    sync_session_local,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invite manager (push up) → invite reader (push up) → accept the
    manager (silence) → cancel the reader invitation (push down) → the
    manager MEMBER leaves (push down): every gesture pays or returns, the
    acceptance alone is silent."""
    from src.agencies import agencies_jobs  # noqa: F401  (imported for parity)

    await _paddle_activate(db_session, admin.agency_id)
    for i in range(2):
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    push = _mock_push(monkeypatch)
    headers = agent_headers(admin)

    assert (
        await _invite(client, headers, system_roles["member"], "g@example.com")
    ).status_code == 201
    assert _items(push) == {"pri_base_cab_m": 1, "pri_seat_cab_m": 1}
    assert (
        await _invite(client, headers, system_roles["viewer"], "l@example.com", seat_type="reader")
    ).status_code == 201
    assert _items(push) == {"pri_base_cab_m": 1, "pri_seat_cab_m": 1, "pri_seat_reader_m": 1}
    assert push.await_count == 2

    push.reset_mock()
    assert (await _accept(client, db_session, "g@example.com")).status_code == 200
    assert push.await_count == 0  # the ONLY silent gesture

    reader_invitation = (
        await db_session.execute(
            select(AgentInvitation).where(AgentInvitation.email == "l@example.com")
        )
    ).scalar_one()
    assert (
        await client.delete(f"/agencies/me/invitations/{reader_invitation.id}", headers=headers)
    ).status_code == 200
    assert _items(push) == {"pri_base_cab_m": 1, "pri_seat_cab_m": 1}  # reader line gone

    joined = (
        await db_session.execute(select(Agent).where(Agent.email == "g@example.com"))
    ).scalar_one()
    push.reset_mock()
    assert (
        await client.post(f"/agencies/me/members/{joined.id}/deactivate", headers=headers)
    ).status_code == 200
    assert _items(push) == {"pri_base_cab_m": 1}  # back to 3 managers = included
    assert push.await_args.kwargs["proration_billing_mode"] == "full_next_billing_period"


# --- (g) constat B7 — conversion arithmetic --------------------------------------------


async def test_trial_conversion_only_ever_bills_readers(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limit case, proved: a trial holds 3 seats, Cabinet includes 3
    manager seats — a trial MANAGER can never be billed at Cabinet
    conversion (Agence, 6 included, even less). The only possible
    conversion cost is the READERS — including a PENDING reader
    invitation (A5: the first push counts roster + attente)."""
    # 2 managers (admin + 1) + 1 PENDING reader invitation = 3 seats.
    await make_agent(role=system_roles["member"], agency_id=admin.agency_id, email="g@example.com")
    headers = agent_headers(admin)
    assert (
        await _invite(
            client, headers, system_roles["viewer"], "lect@example.com", seat_type="reader"
        )
    ).status_code == 201
    create_txn = AsyncMock(return_value={"id": "txn_conv_1"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create_txn)

    checkout = await client.post(
        "/billing/checkout", headers=headers, json={"plan": "cabinet", "billing_cycle": "mensuel"}
    )
    assert checkout.status_code == 200, checkout.text
    assert create_txn.await_args.kwargs["items"] == [
        {"price_id": "pri_base_cab_m", "quantity": 1},
        {"price_id": "pri_seat_reader_m", "quantity": 1},  # the pending reader, nothing else
    ]

    # Manual conversion (Eric's PATCH): the pool is POSED from the attente.
    await client.patch(
        f"/agencies/{admin.agency_id}/subscription",
        headers=agent_headers(superadmin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None and agency.reader_seats_purchased == 1


async def test_trial_conversion_full_manager_roster_bills_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 trial managers → Cabinet (3 included): the checkout carries the
    base alone — zero seat item, zero conversion cost."""
    for i in range(2):
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"g{i}@example.com"
        )
    create_txn = AsyncMock(return_value={"id": "txn_conv_2"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create_txn)

    checkout = await client.post(
        "/billing/checkout",
        headers=agent_headers(admin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert checkout.status_code == 200, checkout.text
    assert create_txn.await_args.kwargs["items"] == [{"price_id": "pri_base_cab_m", "quantity": 1}]


# --- (h) l'expiration passe à 30 jours (lot 10/08) -------------------------------------


async def test_invitation_expiry_follows_the_setting_and_never_resurrects(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    sync_session_local,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La durée est une VALEUR de config (30 j, décision 10/08), lue à la
    CRÉATION et gravée dans expires_at. Deux conséquences prouvées ici :
    une invitation neuve vit 30 jours ; une invitation DÉJÀ expirée sous
    l'ancienne règle (7 j) reste expirée — l'allongement ne ressuscite
    rien, parce que le terme est une colonne, jamais un calcul."""
    from src.agencies import agencies_jobs

    settings = get_settings()
    assert settings.agent_invitation_expires_days == 30  # la valeur, pas un littéral

    headers = agent_headers(admin)
    before = datetime.now(UTC)
    assert (
        await _invite(client, headers, system_roles["member"], "neuve@example.com")
    ).status_code == 201
    fresh = (
        await db_session.execute(
            select(AgentInvitation).where(AgentInvitation.email == "neuve@example.com")
        )
    ).scalar_one()
    lifetime = fresh.expires_at - before
    assert timedelta(days=29, hours=23) < lifetime <= timedelta(days=30, minutes=1)

    # Une invitation née sous l'ANCIENNE règle et déjà périmée : le stamp
    # stocké fait foi, la nouvelle durée ne la rappelle pas à la vie.
    assert (
        await _invite(client, headers, system_roles["member"], "vieille@example.com")
    ).status_code == 201
    old = (
        await db_session.execute(
            select(AgentInvitation).where(AgentInvitation.email == "vieille@example.com")
        )
    ).scalar_one()
    old.expires_at = datetime.now(UTC) - timedelta(days=1)  # stamp 7 j, périmé hier
    await db_session.commit()
    agency_id = admin.agency_id

    # Le FILET : la périmée sort des comptes AVANT même le job.
    seats = (await client.get("/agencies/me", headers=headers)).json()["subscription"]["seats"]
    assert seats["members"] == 2  # admin + la neuve ; la périmée ne compte plus

    def _sweep() -> dict:
        with sync_session_local() as sync_db:
            return agencies_jobs.expire_agent_invitations(sync_db, log=lambda _line: None)

    monkeypatch.setattr(agencies_jobs, "_push_seat_quantities_down", Mock())
    stats = await asyncio.to_thread(_sweep)
    assert stats["expired"] == 1  # la périmée seule ; la neuve (30 j) est intacte

    db_session.expire_all()
    statuses = {
        row.email: row.status
        for row in (
            await db_session.execute(
                select(AgentInvitation).where(AgentInvitation.agency_id == agency_id)
            )
        ).scalars()
    }
    assert statuses == {"neuve@example.com": "pending", "vieille@example.com": "expired"}
