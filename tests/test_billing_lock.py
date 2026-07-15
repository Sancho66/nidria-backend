"""Billing lock (read-only, never destructive) — the 4th stage of enforce().

Two halves. The PURE RULE (blocking_reason: trial J+0, converted_at as the
discriminant — NOT billing_mode —, past_due grace, canceled immediate) unit
tested on unsaved Agency objects. The ENFORCEMENT over HTTP: a blocked
agency's agents read everything and write nothing (403
billing.subscription_required, stable), the declared allowlist stays open
(the payment path is the exit), superadmin exempt, the expat face fully
functional (their démarches, not the agency's fault), and the lock lifts
by webhook alone (payment recovered → writes come back).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.billing.billing_lock import blocking_reason
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


# --- the pure rule ----------------------------------------------------------------------


def _agency(**overrides: object) -> Agency:
    fields: dict = {
        "id": uuid.uuid4(),
        "name": "A",
        "slug": "a",
        "settings": {},
        "trial_ends_at": None,
        "converted_at": None,
        "billing_mode": "manual",
        "billing_status": None,
        "past_due_since": None,
    }
    fields.update(overrides)
    return Agency(**fields)


def test_trial_rule_is_j_plus_zero() -> None:
    running = _agency(trial_ends_at=NOW + timedelta(days=1))
    assert blocking_reason(running, now=NOW) is None
    expired = _agency(trial_ends_at=NOW - timedelta(seconds=1))
    assert blocking_reason(expired, now=NOW) == "trial_expired"
    # No trial calendar at all (platform/demo agencies): no deadline, ever.
    assert blocking_reason(_agency(trial_ends_at=None), now=NOW) is None


def test_manually_converted_agency_is_never_blocked() -> None:
    """THE test that protects Nicolas and Reside: converted_at posed by
    Eric, no Paddle anywhere — never blocked, even trial long expired,
    even without any billing_status."""
    nicolas = _agency(
        trial_ends_at=NOW - timedelta(days=400),  # trial ancient history
        converted_at=NOW - timedelta(days=300),
        billing_mode="manual",
        billing_status=None,
    )
    assert blocking_reason(nicolas, now=NOW) is None


def test_paddle_past_due_blocks_after_the_grace_only() -> None:
    def paddle(since_days: float) -> Agency:
        return _agency(
            converted_at=NOW - timedelta(days=60),
            billing_mode="paddle",
            billing_status="past_due",
            past_due_since=NOW - timedelta(days=since_days),
        )

    assert blocking_reason(paddle(6.9), now=NOW) is None  # dunning runs, we wait
    assert blocking_reason(paddle(7), now=NOW) == "past_due"  # grace consumed
    # past_due WITHOUT an anchor (defensive): never blocks — the anchor is
    # posed by the same webhook that poses the status.
    orphan = _agency(converted_at=NOW, billing_mode="paddle", billing_status="past_due")
    assert blocking_reason(orphan, now=NOW) is None


def test_paddle_canceled_blocks_immediately_and_active_never() -> None:
    canceled = _agency(
        converted_at=NOW - timedelta(days=60), billing_mode="paddle", billing_status="canceled"
    )
    assert blocking_reason(canceled, now=NOW) == "canceled"  # period end = the grace
    active = _agency(
        converted_at=NOW - timedelta(days=60),
        billing_mode="paddle",
        billing_status="active",
        trial_ends_at=NOW - timedelta(days=90),  # the old trial date is irrelevant
    )
    assert blocking_reason(active, now=NOW) is None


# --- the enforcement over HTTP ----------------------------------------------------------


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="locked-client@example.com")


async def _expire_trial(db: AsyncSession, agency_id: uuid.UUID) -> None:
    await db.execute(
        update(Agency)
        .where(Agency.id == agency_id)
        .values(trial_ends_at=datetime.now(UTC) - timedelta(days=1))
    )
    await db.commit()


async def test_blocked_agency_reads_everything_writes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    aid = admin.agency_id
    # Before the lock: writes pass (control).
    created = await client.post("/journeys", headers=h, json={"name": "Avant"})
    assert created.status_code == 201, created.text

    await _expire_trial(db_session, aid)

    # Writes: 403 with THE stable code, on several surfaces (method rule).
    for method, url, body in (
        ("POST", "/journeys", {"name": "Apres"}),
        ("PATCH", "/agencies/me", {"name": "Renamed"}),
        ("POST", "/agencies/me/invitations", {"email": "x@y.io", "role_id": str(admin.role_id)}),
        ("DELETE", f"/journeys/{created.json()['id']}", None),
    ):
        resp = await client.request(method, url, headers=h, json=body)
        assert resp.status_code == 403, (url, resp.text)
        assert resp.json()["code"] == "billing.subscription_required", url
        assert resp.json()["params"]["reason"] == "trial_expired"

    # Reads: EVERYTHING stays visible (read-only, never destructive).
    for url in ("/journeys", "/cases", "/agencies/me", "/agencies/me/members"):
        resp = await client.get(url, headers=h)
        assert resp.status_code == 200, (url, resp.text)

    # And GET /agencies/me tells the front why (banner + greyed states).
    sub = (await client.get("/agencies/me", headers=h)).json()["subscription"]
    assert sub["is_blocked"] is True and sub["blocked_reason"] == "trial_expired"


async def test_allowlist_keeps_the_exit_open(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a BLOCKED agency: the payment path and the session lifecycle
    still answer — the exit of the blockage never locks."""
    import json as _json
    from unittest.mock import AsyncMock

    from src.billing import paddle_client
    from src.core.config import get_settings

    h = agent_headers(admin)
    await _expire_trial(db_session, admin.agency_id)

    # Checkout: reaches the billing logic (mocked Paddle), NOT the lock.
    monkeypatch.setenv("BILLING_CHECKOUT_ENABLED", "true")
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv(
        "PADDLE_PRICE_IDS",
        _json.dumps(
            {
                "cabinet_mensuel": "pri_b",
                "seat_cabinet_mensuel": "pri_s",
            }
        ),
    )
    get_settings.cache_clear()
    create = AsyncMock(return_value={"id": "txn_exit"})
    monkeypatch.setattr(paddle_client.PaddleClient, "create_transaction", create)
    checkout = await client.post(
        "/billing/checkout", headers=h, json={"plan": "cabinet", "billing_cycle": "mensuel"}
    )
    get_settings.cache_clear()
    assert checkout.status_code == 200, checkout.text  # the exit works

    # Session lifecycle: logout answers (not the lock's 403).
    logout = await client.post("/auth/agent/logout", headers=h)
    assert logout.status_code != 403, logout.text


async def test_superadmin_is_exempt(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"], email="root@platform.io")
    await _expire_trial(db_session, superadmin.agency_id)  # even HIS agency expired
    target = await make_agent(role=system_roles["admin"])
    resp = await client.patch(
        f"/agencies/{target.agency_id}/subscription",
        headers=agent_headers(superadmin),
        json={"is_founding": True},
    )
    assert resp.status_code == 200, resp.text  # the human exit never locks


async def test_expat_face_is_fully_functional_on_a_blocked_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """Their démarches, not the agency's fault: the client keeps reading
    AND writing (step validation here — the deposit gesture family) while
    the agency is read-only."""
    ah = agent_headers(admin)
    # An expat-validated step, started — built BEFORE the lock falls.
    tid = (await client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    sid = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=ah,
            json={"name": "Collecte", "validated_by_type": "expat"},
        )
    ).json()["id"]
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    timeline = (
        await client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    pid = next(s["id"] for s in timeline if s["template_step_id"] == sid)
    started = await client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "in_progress"}
    )
    assert started.status_code == 200, started.text

    await _expire_trial(db_session, admin.agency_id)

    eh = expat_headers(expat)
    # Read: the whole client space.
    assert (await client.get("/expat/cases", headers=eh)).status_code == 200
    detail = await client.get(f"/expat/cases/{case.id}", headers=eh)
    assert detail.status_code == 200, detail.text
    # Write: the client validates their step — 200 while the agency is locked.
    done = await client.post(f"/expat/cases/{case.id}/steps/{pid}/validate", headers=eh)
    assert done.status_code == 200, done.text
    # Control: the agent, meanwhile, cannot touch the same dossier.
    denied = await client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "done"}
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "billing.subscription_required"


async def test_webhook_poses_grace_anchor_and_recovery_lifts_the_lock(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full past_due lifecycle, webhook-driven only: past_due poses
    past_due_since (kept across re-deliveries), the lock falls once the
    grace is consumed, and a recovered payment (activated) clears the
    anchor — writes come back with NO human gesture on our side."""
    from tests.test_billing_paddle import PRICE_IDS, SECRET, _envelope, _post

    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", SECRET)
    import json as _json

    from src.core.config import get_settings

    monkeypatch.setenv("PADDLE_PRICE_IDS", _json.dumps(PRICE_IDS))
    get_settings.cache_clear()
    try:
        h = agent_headers(admin)
        aid = admin.agency_id
        # Real-life chronology (the convergence rule drops stale statuses):
        # activation well BEFORE the payment failure.
        await _post(
            client,
            _envelope(
                "subscription.activated",
                agency_id=aid,
                occurred_at=datetime.now(UTC) - timedelta(days=60),
            ),
        )

        first_fail = datetime.now(UTC) - timedelta(days=8)
        await _post(
            client,
            _envelope("subscription.past_due", agency_id=aid, occurred_at=first_fail),
        )
        # Re-delivery one day later: the FIRST instant is kept.
        await _post(
            client,
            _envelope(
                "subscription.past_due",
                agency_id=aid,
                occurred_at=first_fail + timedelta(days=1),
            ),
        )
        db_session.expire_all()
        agency = await db_session.get(Agency, aid)
        assert agency is not None and agency.past_due_since == first_fail

        # 8 days > 7 of grace: the lock is down.
        denied = await client.post("/journeys", headers=h, json={"name": "Locked"})
        assert denied.status_code == 403
        assert denied.json()["params"]["reason"] == "past_due"

        # Paddle recovers the payment → activated → anchor cleared, lock lifted.
        await _post(client, _envelope("subscription.activated", agency_id=aid))
        db_session.expire_all()
        agency = await db_session.get(Agency, aid)
        assert agency is not None
        assert agency.billing_status == "active" and agency.past_due_since is None
        recovered = await client.post("/journeys", headers=h, json={"name": "Back"})
        assert recovered.status_code == 201, recovered.text
    finally:
        get_settings.cache_clear()
