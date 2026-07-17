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
    assert block["seats"] == {"members": 1, "included": 6, "offered": 2, "billed": 0, "max": 10}

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
    sub = before["subscription"]
    # trial_ends_at is a DATE (running trial) or None — dynamic, so it is
    # asserted for shape, not value, and popped from the snapshot below.
    trial_ends = sub.pop("trial_ends_at")
    assert trial_ends is None or isinstance(trial_ends, str)
    assert sub == {
        "plan": None,
        "billing_cycle": None,
        "is_founding": False,
        "seats": {"members": 1, "included": 3, "offered": 0, "billed": 0, "max": 3},
        # Billing lock: a running trial is not blocked (the front's banner).
        "is_blocked": False,
        "blocked_reason": None,
        # Providers (grid 2026-07): trial tier, nothing billed in phase 1.
        "providers": {"count": 0, "included": 10, "max": 10},
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


# --- (f) grille 2026-07 : sieges inclus par plan, prestataires, sur-mesure -------------


async def _external_role(db_session: AsyncSession) -> Role:
    return (
        await db_session.execute(select(Role).where(Role.is_external.is_(True)).limit(1))
    ).scalar_one()


async def _add_externals(
    db_session: AsyncSession, agency_id: uuid.UUID, role: Role, count: int
) -> None:
    for i in range(count):
        db_session.add(
            Agent(
                agency_id=agency_id,
                role_id=role.id,
                email=f"prov-{uuid.uuid4().hex[:10]}@example.com",
                first_name="Prov",
                last_name=f"N{i}",
                password_hash=hash_password("ProviderPassword1!"),
                is_external=True,
            )
        )
    await db_session.commit()


async def test_agence_plan_includes_six_seats(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    """SEATS_INCLUDED_BY_PLAN is the truth (the column is gone): agence
    includes 6 — billed starts at the 7th, never negative below."""
    await client.patch(
        f"/agencies/{admin.agency_id}/subscription",
        headers=agent_headers(superadmin),
        json={"plan": "agence", "billing_cycle": "mensuel"},
    )
    await _add_members(db_session, admin.agency_id, system_roles["member"], 4)  # 5 members

    seats = (await client.get("/agencies/me", headers=agent_headers(admin))).json()["subscription"][
        "seats"
    ]
    assert seats == {"members": 5, "included": 6, "offered": 0, "billed": 0, "max": 10}

    # 7 members: the 7th is the first billed one.
    await _add_members(db_session, admin.agency_id, system_roles["member"], 2)
    seats = (await client.get("/agencies/me", headers=agent_headers(admin))).json()["subscription"][
        "seats"
    ]
    assert seats["billed"] == 1  # 7 − 6 included


async def test_founding_seats_stay_cumulative_with_the_plan_included(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    await client.patch(
        f"/agencies/{admin.agency_id}/subscription",
        headers=agent_headers(superadmin),
        json={
            "plan": "agence",
            "billing_cycle": "mensuel",
            "is_founding": True,
            "founding_free_seats": 2,
        },
    )
    await _add_members(db_session, admin.agency_id, system_roles["member"], 7)  # 8 members
    seats = (await client.get("/agencies/me", headers=agent_headers(admin))).json()["subscription"][
        "seats"
    ]
    # 8 − 6 included − 2 offered = 0 billed (cumulative, never negative).
    assert seats["billed"] == 0 and seats["included"] == 6 and seats["offered"] == 2


async def test_provider_gate_blocks_at_the_cap_with_params(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Trial provider cap (10): actives + PENDING invitations count; the
    gate answers 409 subscription.provider_limit with the cap in params."""
    headers = agent_headers(admin)
    role = await _external_role(db_session)
    await _add_externals(db_session, admin.agency_id, role, 9)  # 9 active

    # 10th provider: the INVITATION goes through (free up to the cap)…
    tenth = await client.post(
        "/agencies/me/external-invitations",
        headers=headers,
        json={"name": "Prestataire 10", "email": "p10@example.com", "role_id": str(role.id)},
    )
    assert tenth.status_code == 201, tenth.text

    # …and now 9 active + 1 invited = 10 = the trial cap: blocked, message
    # pointing past the plan (custom on a plan; trial says convert).
    blocked = await client.post(
        "/agencies/me/external-invitations",
        headers=headers,
        json={"name": "Prestataire 11", "email": "p11@example.com", "role_id": str(role.id)},
    )
    assert blocked.status_code == 409, blocked.text
    body = blocked.json()
    assert body["code"] == "subscription.provider_limit"
    assert body["params"] == {"providers": 10, "max": 10, "plan": None}


async def test_directory_contacts_never_count_as_providers(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """A named directory contact (no login) costs nothing: the gate ignores
    it entirely."""
    headers = agent_headers(admin)
    role = await _external_role(db_session)
    for i in range(12):  # more directory rows than the trial cap
        created = await client.post(
            "/agencies/me/external-contacts",
            headers=headers,
            json={"name": f"Annuaire {i}"},
        )
        assert created.status_code == 201, created.text
    # The provider invitation still goes through: the directory is free.
    invited = await client.post(
        "/agencies/me/external-invitations",
        headers=headers,
        json={"name": "Vrai prestataire", "email": "vrai@example.com", "role_id": str(role.id)},
    )
    assert invited.status_code == 201, invited.text


async def test_sur_mesure_escapes_both_caps_and_checkout(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.config import get_settings

    # Open the kill switch so the checkout refusal exercised below is the
    # sur_mesure one, not billing.checkout_disabled.
    monkeypatch.setenv("BILLING_CHECKOUT_ENABLED", "true")
    get_settings.cache_clear()
    """Plan 3 (quote): absent from the MAX dicts = NO cap — members past 10
    and providers past 25 both pass; max is served as null (the front shows
    "illimité"); the self-serve checkout refuses it explicitly."""
    headers = agent_headers(admin)
    # The checkout refuses the quote-based plan PER SE (before any agency
    # state is considered) — tested while still on trial.
    checkout = await client.post(
        "/billing/checkout",
        headers=headers,
        json={"plan": "sur_mesure", "billing_cycle": "mensuel"},
    )
    assert checkout.status_code == 409
    assert checkout.json()["code"] == "billing.sur_mesure_is_a_quote"
    # Close the switch and drop the cached settings NOW (not at teardown):
    # nothing may leak into the next test.
    monkeypatch.delenv("BILLING_CHECKOUT_ENABLED")
    get_settings.cache_clear()
    await client.patch(
        f"/agencies/{admin.agency_id}/subscription",
        headers=agent_headers(superadmin),
        json={"plan": "sur_mesure", "billing_cycle": "mensuel"},
    )
    # max served as null, not a number.
    seats = (await client.get("/agencies/me", headers=headers)).json()["subscription"]["seats"]
    assert seats["max"] is None

    # 12 members (past the agence cap of 10): the 13th invitation passes.
    await _add_members(db_session, admin.agency_id, system_roles["member"], 11)
    allowed = await _invite(client, headers, system_roles["member"], "m13@example.com")
    assert allowed.status_code == 201, allowed.text

    # 26 providers (past the agence cap of 25): the 27th passes too.
    role = await _external_role(db_session)
    await _add_externals(db_session, admin.agency_id, role, 26)
    provider = await client.post(
        "/agencies/me/external-invitations",
        headers=headers,
        json={"name": "P27", "email": "p27@example.com", "role_id": str(role.id)},
    )
    assert provider.status_code == 201, provider.text


# --- (g) hygiene des invitations : re-check au cap a l'ACCEPTATION, purge du fantome ---


async def _pending_token(db_session: AsyncSession, agency_id: uuid.UUID, email: str) -> str:
    from shared.models.invitation import AgentInvitation

    return (
        await db_session.execute(
            select(AgentInvitation.token).where(
                AgentInvitation.agency_id == agency_id, AgentInvitation.email == email
            )
        )
    ).scalar_one()


async def _accept(client: AsyncClient, token: str):
    return await client.post(
        "/agencies/invitations/accept",
        json={
            "token": token,
            "password": "AcceptPassword1!",
            "first_name": "New",
            "last_name": "Member",
        },
    )


async def test_acceptance_recheck_blocks_at_the_cap_invitation_stays_pending(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    """N invitations on one slot can no longer overshoot: the cap re-checks
    at ACCEPTANCE — 409 to the acceptant, the invitation STAYS PENDING (a
    seat can free up and the same link works again)."""
    from shared.models.invitation import AgentInvitation

    headers = agent_headers(admin)
    member_role = system_roles["member"]
    aid = admin.agency_id  # plain id: instances expire below (expire_all)
    # 1 member, trial cap 3: two invitations pass the invite gate.
    for email in ("i2@example.com", "i3@example.com"):
        r = await _invite(client, headers, member_role, email)
        assert r.status_code == 201, r.text
    # The roster fills up THROUGH ANOTHER PATH before anyone accepts.
    await _add_members(db_session, admin.agency_id, member_role, 2)  # 3 = cap

    token = await _pending_token(db_session, admin.agency_id, "i2@example.com")
    blocked = await _accept(client, token)
    assert blocked.status_code == 409, blocked.text
    body = blocked.json()
    assert body["code"] == "invitation.capacity_reached"
    assert body["params"]["max"] == 3
    # The invitation is UNTOUCHED: still pending.
    db_session.expire_all()
    status = (
        await db_session.execute(
            select(AgentInvitation.status).where(AgentInvitation.email == "i2@example.com")
        )
    ).scalar_one()
    assert status == "pending"

    # A seat frees up -> the SAME link works.
    victim = (
        (
            await db_session.execute(
                select(Agent).where(
                    Agent.agency_id == aid,
                    Agent.email.like("member-%@example.com"),
                )
            )
        )
        .scalars()
        .first()
    )
    await db_session.delete(victim)
    await db_session.commit()
    accepted = await _accept(client, token)
    assert accepted.status_code == 200, accepted.text


async def test_external_acceptance_recheck_blocks_past_the_cap(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Provider variant: the pre-created agent counts itself, so acceptance
    blocks only when the roster grew PAST the cap after the invite."""
    headers = agent_headers(admin)
    role = await _external_role(db_session)
    invited = await client.post(
        "/agencies/me/external-invitations",
        headers=headers,
        json={"name": "Presta cap", "email": "pcap@example.com", "role_id": str(role.id)},
    )
    assert invited.status_code == 201, invited.text
    # The roster grows past the trial cap (10) after the invite.
    await _add_externals(db_session, admin.agency_id, role, 10)  # 11 with the pre-created

    token = await _pending_token(db_session, admin.agency_id, "pcap@example.com")
    blocked = await _accept(client, token)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "invitation.capacity_reached"

    # Two providers leave -> acceptance passes (10 <= 10 after).
    extras = (
        (
            await db_session.execute(
                select(Agent)
                .where(
                    Agent.agency_id == admin.agency_id,
                    Agent.email.like("prov-%@example.com"),
                )
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    for extra in extras:
        await db_session.delete(extra)
    await db_session.commit()
    accepted = await _accept(client, token)
    assert accepted.status_code == 200, accepted.text


async def test_cancellation_purges_the_phantom_and_frees_the_counter(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Cancelling an external invitation deletes the pre-created agent: the
    provider counter drops, the directory contact returns to 'none'."""
    from shared.models.external_contact import ExternalContact
    from shared.models.invitation import AgentInvitation

    headers = agent_headers(admin)
    role = await _external_role(db_session)
    role_id, aid = str(role.id), admin.agency_id  # plain ids (expire_all below)
    await _add_externals(db_session, aid, role, 9)
    tenth = await client.post(
        "/agencies/me/external-invitations",
        headers=headers,
        json={"name": "P dix", "email": "pdix@example.com", "role_id": role_id},
    )
    assert tenth.status_code == 201, tenth.text
    # At the trial cap (10): the next invitation is refused.
    refused = await client.post(
        "/agencies/me/external-invitations",
        headers=headers,
        json={"name": "P onze", "email": "ponze@example.com", "role_id": role_id},
    )
    assert refused.status_code == 409

    invitation_id = (
        await db_session.execute(
            select(AgentInvitation.id).where(AgentInvitation.email == "pdix@example.com")
        )
    ).scalar_one()
    cancelled = await client.delete(f"/agencies/me/invitations/{invitation_id}", headers=headers)
    assert cancelled.status_code in (200, 204), cancelled.text

    db_session.expire_all()
    # The phantom is GONE…
    ghost = (
        await db_session.execute(select(Agent).where(Agent.email == "pdix@example.com"))
    ).scalar_one_or_none()
    assert ghost is None
    # …the directory contact is back to 'none' (re-invitable)…
    contact_agent = (
        await db_session.execute(
            select(ExternalContact.agent_id).where(ExternalContact.name == "P dix")
        )
    ).scalar_one()
    assert contact_agent is None
    # …and the counter dropped: the eleventh invitation now passes.
    retry = await client.post(
        "/agencies/me/external-invitations",
        headers=headers,
        json={"name": "P onze bis", "email": "ponzebis@example.com", "role_id": role_id},
    )
    assert retry.status_code == 201, retry.text
