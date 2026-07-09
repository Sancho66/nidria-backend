"""Subscription model (structure F, pricing Eric 2026-07-07).

Covers: (a) internal invitation BLOCKED at the plan cap
(subscription.seat_limit) but ALLOWED between included+offered and the
cap (manual billing: the app never blocks paid usage), externals never
gated; (b) an unconverted agency (trial) is capped at 3 members with
the same code; (c) the superadmin PATCH poses the conversion (plan,
derived seat price, converted_at, agency.converted event); (d) the
agency settings expose the read-only subscription block; (e) a
converted agency leaves the trial-nurture scope even with
trial_ends_at still set."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from shared.models.usage import AgencyUsageMilestone, UsageEvent
from src.core.security import hash_password
from src.nurture.nurture_job import send_trial_nurture
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"])


async def _add_members(
    db_session: AsyncSession, agency_id: uuid.UUID, role: Role, count: int
) -> None:
    for i in range(count):
        db_session.add(
            Agent(
                agency_id=agency_id,
                role_id=role.id,
                email=f"member-{uuid.uuid4().hex[:10]}@example.com",
                first_name="Membre",
                last_name=f"N{i}",
                password_hash=hash_password("MemberPassword1!"),
                is_external=False,
            )
        )
    await db_session.commit()


def _invite(client: AsyncClient, headers: dict[str, str], role: Role, email: str):
    return client.post(
        "/agencies/me/invitations",
        headers=headers,
        json={"email": email, "role_id": str(role.id)},
    )


# --- (b) trial: capped at 3 members, dedicated code -----------------------------------


async def test_trial_agency_is_capped_at_three_members(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    headers = agent_headers(admin)
    member_role = system_roles["member"]

    # 2 members total: the 3rd is invitable.
    await _add_members(db_session, admin.agency_id, member_role, 1)
    allowed = await _invite(client, headers, member_role, "third@example.com")
    assert allowed.status_code == 201, allowed.text

    # 3 members total: the cap. The next INVITATION is blocked.
    await _add_members(db_session, admin.agency_id, member_role, 1)
    blocked = await _invite(client, headers, member_role, "fourth@example.com")
    assert blocked.status_code == 409, blocked.text
    body = blocked.json()
    assert body["code"] == "subscription.seat_limit"
    assert body["params"] == {"members": 3, "max": 3, "plan": None}
    assert "trial" in body["detail"].lower()


# --- (a) plan cap: blocked past max, allowed between included and max -----------------


async def test_plan_cap_blocks_past_max_and_allows_billed_seats(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    headers = agent_headers(admin)
    member_role = system_roles["member"]

    converted = await client.patch(
        f"/agencies/{admin.agency_id}/subscription",
        headers=agent_headers(superadmin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert converted.status_code == 200, converted.text

    # 4 members (past the 3 included): invitation of the 5th ALLOWED -
    # billing is manual, the app never blocks paid usage under the cap.
    await _add_members(db_session, admin.agency_id, member_role, 3)
    fifth = await _invite(client, headers, member_role, "fifth@example.com")
    assert fifth.status_code == 201, fifth.text

    # 5 members = the cabinet cap: the next invitation is blocked.
    await _add_members(db_session, admin.agency_id, member_role, 1)
    blocked = await _invite(client, headers, member_role, "sixth@example.com")
    assert blocked.status_code == 409, blocked.text
    body = blocked.json()
    assert body["code"] == "subscription.seat_limit"
    assert body["params"] == {"members": 5, "max": 5, "plan": "cabinet"}

    # Externals never consume a seat: the external invitation still goes
    # through at the cap.
    external_role = next(
        iter(
            [
                r
                for r in (
                    await db_session.execute(select(Role).where(Role.is_external.is_(True)))
                ).scalars()
            ]
        ),
        None,
    )
    if external_role is not None:
        provider = await client.post(
            "/agencies/me/external-invitations",
            headers=headers,
            json={
                "name": "Avocat",
                "email": "avocat@example.com",
                "role_id": str(external_role.id),
            },
        )
        assert provider.status_code == 201, provider.text


# --- (c) superadmin PATCH poses the conversion -----------------------------------------


async def test_superadmin_patch_poses_the_conversion(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    response = await client.patch(
        f"/agencies/{admin.agency_id}/subscription",
        headers=agent_headers(superadmin),
        json={
            "plan": "agence",
            "billing_cycle": "annuel",
            "is_founding": True,
            "founding_free_seats": 2,
            "price_locked_until": "2028-07-07",
        },
    )
    assert response.status_code == 200, response.text
    block = response.json()
    assert block["plan"] == "agence"
    assert block["billing_cycle"] == "annuel"
    assert block["is_founding"] is True
    assert block["seats"] == {"members": 1, "included": 3, "offered": 2, "billed": 0, "max": 10}

    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    assert agency.seat_price_eur == 25  # derived from the plan
    assert agency.converted_at is not None  # stamped when absent
    assert str(agency.price_locked_until) == "2028-07-07"

    event = (
        await db_session.execute(
            select(UsageEvent).where(
                UsageEvent.agency_id == admin.agency_id,
                UsageEvent.event_type == "agency.converted",
            )
        )
    ).scalar_one()
    assert event.details["plan"] == "agence"

    # A plain admin cannot touch it (superadmin gate).
    forbidden = await client.patch(
        f"/agencies/{admin.agency_id}/subscription",
        headers=agent_headers(admin),
        json={"plan": "cabinet"},
    )
    assert forbidden.status_code == 403


# --- (d) settings expose the read-only block -------------------------------------------


async def test_settings_expose_the_subscription_block(
    client: AsyncClient, admin: Agent, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    before = (await client.get("/agencies/me", headers=agent_headers(admin))).json()
    assert before["subscription"] == {
        "plan": None,
        "billing_cycle": None,
        "is_founding": False,
        "seats": {"members": 1, "included": 3, "offered": 0, "billed": 0, "max": 3},
    }

    await client.patch(
        f"/agencies/{admin.agency_id}/subscription",
        headers=agent_headers(superadmin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    after = (await client.get("/agencies/me", headers=agent_headers(admin))).json()
    assert after["subscription"]["plan"] == "cabinet"
    assert after["subscription"]["seats"]["max"] == 5


# --- (e) converted agencies leave the nurture scope --------------------------------------


async def test_nurture_ignores_converted_agencies(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    admin: Agent,
    system_roles: dict[str, Role],
) -> None:
    """trial_ends_at stays set at conversion (unchanged by design): the
    converted_at guard alone must take the agency out of the calendar."""
    activated_at = datetime.now(UTC) - timedelta(days=8)
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.trial_ends_at = activated_at + timedelta(days=30)
    db_session.add(
        AgencyUsageMilestone(
            agency_id=agency.id, key="agence_activee", first_at=activated_at, count=1
        )
    )
    await db_session.commit()

    def run_dry() -> dict:
        with sync_session_local() as db:
            return send_trial_nurture(db, log=lambda _m: None, dry_run=True)

    assert run_dry()["in_scope"] == 1  # J+8, in the calendar

    agency.converted_at = datetime.now(UTC)
    await db_session.commit()
    stats = run_dry()
    assert stats["in_scope"] == 0 and stats["sent"] == 0  # converted: out, no mail
