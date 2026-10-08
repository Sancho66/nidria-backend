"""Lemlist's activity webhook, `POST /webhooks/lemlist` (spec 7.2, 10.4).

PUBLIC (a real binding line, no token), authenticated by the body's
`secret` against EMAIL_WEBHOOK_SECRET (`hmac.compare_digest`): 403
`email.webhook_forbidden` when it is absent, wrong, or not configured.
Rate limited like `/webhooks/docuseal` (600 calls a minute), per real client
IP (the first X-Forwarded-For hop). The answer is
Prism's `{stored, duplicate, matched}`; an event of a type Nidria does not
track, or of no campaign Nidria runs, is acknowledged and not stored; a
known campaign's event whose lead matches nobody is stored unmatched. The
agency is the campaign's; archived campaigns and agencies whose module is
switched off are served (the webhook depends on the env only). It never
hands a lead over.

Ported from Prism's `tests/test_email_campaigns.py` (L1600, the webhook).
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency_email_module import AgencyEmailModule
from shared.models.email_campaign import EmailCampaign, EmailCampaignMember, EmailProviderEvent
from src.core.enums import Audience
from src.core.rbac.baseline import collect_bindings
from src.email_campaigns import email_router
from src.email_campaigns.email_router import WEBHOOK_BINDINGS
from tests.email_fakes import FakeProvider
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import EmailWorld, email_env
from tests.test_email_campaigns import campaign_via_api, members_of, set_status, step_via_api
from tests.test_email_launch import add_via_api, launch_via_api

pytestmark = pytest.mark.usefixtures("rbac_baseline")

SECRET = "s3cret-webhook"
URL = "/webhooks/lemlist"
HAND_OVER = {"add_contact", "launch_contact"}


@contextmanager
def configured(secret: str = SECRET) -> Iterator[None]:
    with email_env(EMAIL_WEBHOOK_SECRET=secret):
        yield


async def _launched(
    client: AsyncClient,
    db: AsyncSession,
    world: EmailWorld,
    make_client_profile: MakeClientProfile,
    *,
    name: str = "Founders",
) -> tuple[str, str, dict[str, Any]]:
    """A one-step campaign with Ana launched at the (fake) provider: (campaign
    id, Lemlist campaign id, Ana's membership row)."""
    profile = await make_client_profile(
        agency_id=world.agency_id, first_name="Ana", last_name="Ready", email="ana@acme.io"
    )
    cid = (await campaign_via_api(client, world.headers, name))["id"]
    await step_via_api(client, world.headers, cid)
    await add_via_api(client, world.headers, cid, client_profile_ids=[profile.id])
    await set_status(client, world.headers, cid, "ACTIVE")
    launched = await launch_via_api(client, world.headers, cid)
    assert launched.json()["members_pushed"] == 1, launched.text
    campaign = await db.get(EmailCampaign, uuid.UUID(cid))
    assert campaign is not None and campaign.provider_campaign_id
    [member] = await members_of(db, cid)
    return cid, campaign.provider_campaign_id, member


def _payload(lemlist: str, lead: str | None, **values: Any) -> dict[str, Any]:
    return {
        "_id": "act_42",
        "type": "emailsSent",
        "createdAt": "2026-10-04T09:00:00.000Z",
        "campaignId": lemlist,
        "leadId": lead,
        "sequenceStep": 0,
        "secret": SECRET,
        **values,
    }


async def _events(db: AsyncSession) -> list[EmailProviderEvent]:
    rows = await db.scalars(select(EmailProviderEvent).execution_options(populate_existing=True))
    return list(rows.all())


# ── the binding ──────────────────────────────────────────────────────────────


def test_the_webhook_is_bound_public() -> None:
    [binding] = WEBHOOK_BINDINGS
    assert (binding.method, binding.path) == ("POST", URL)
    assert binding.audience == Audience.PUBLIC and binding.permission is None
    assert ("POST", URL) in {(b.method, b.path) for b in collect_bindings()}
    assert email_router.WEBHOOK_RATE_LIMIT == 600
    assert email_router.WEBHOOK_RATE_WINDOW_SECONDS == 60


# ── the secret ───────────────────────────────────────────────────────────────


async def test_the_webhook_needs_its_secret_and_stores_each_event_once(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, lemlist, member = await _launched(client, db_session, email_world, make_client_profile)
    payload = _payload(lemlist, member["provider_lead_id"])
    fake_email_provider.ops.clear()

    # Not configured: refused whatever the body says.
    unconfigured = await client.post(URL, json=payload)
    assert unconfigured.status_code == 403
    assert unconfigured.json()["code"] == "email.webhook_forbidden"
    with configured():
        for body in (
            {**payload, "secret": "nope"},
            {k: v for k, v in payload.items() if k != "secret"},
            {**payload, "secret": 42},
        ):
            refused = await client.post(URL, json=body)
            assert refused.status_code == 403, body
            assert refused.json()["code"] == "email.webhook_forbidden"
        not_json = await client.post(
            URL, content=b"not json", headers={"content-type": "application/json"}
        )
        assert not_json.status_code == 403
        listed = await client.post(URL, json=[payload])
        assert listed.status_code == 403
        assert await _events(db_session) == []

        # PUBLIC: no token at all, and it is served.
        first = await client.post(URL, json=payload)
        assert first.status_code == 200, first.text
        assert first.json() == {"stored": True, "duplicate": False, "matched": True}
        second = await client.post(URL, json=payload)
        assert second.json() == {"stored": False, "duplicate": True, "matched": False}
        foreign = await client.post(URL, json={**payload, "_id": "act_43", "campaignId": "cam_x"})
        assert foreign.json() == {"stored": False, "duplicate": False, "matched": False}
    [event] = await _events(db_session)
    assert "secret" not in event.payload and event.step_position == 1
    assert (event.agency_id, event.campaign_member_id) == (
        email_world.agency_id,
        member["id"],
    )
    [now] = await members_of(db_session, cid)
    # Its only step went out: the sequence is over (a reply would still count).
    assert now["status"] == "EMAIL_COMPLETED" and now["current_step"] == 1
    # The webhook never hands a lead over, nor writes at Lemlist.
    assert HAND_OVER.isdisjoint(fake_email_provider.names())
    assert fake_email_provider.ops == []


async def test_an_empty_secret_setting_refuses_even_an_empty_secret(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    _, lemlist, member = await _launched(client, db_session, email_world, make_client_profile)
    with configured(""):
        refused = await client.post(
            URL, json=_payload(lemlist, member["provider_lead_id"], secret="")
        )
    assert refused.status_code == 403
    assert refused.json()["code"] == "email.webhook_forbidden"
    assert await _events(db_session) == []


# ── what is stored, and for whom ─────────────────────────────────────────────


async def test_untracked_unknown_and_third_party_events_are_acknowledged_only(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    _, lemlist, member = await _launched(client, db_session, email_world, make_client_profile)
    lead = member["provider_lead_id"]
    ignored = {"stored": False, "duplicate": False, "matched": False}
    with configured():
        # A type Nidria does not track.
        visit = await client.post(URL, json=_payload(lemlist, lead, type="linkedinVisitDone"))
        assert visit.json() == ignored
        # No campaign at all, and a reply Lemlist attributes to a third party.
        no_campaign = await client.post(URL, json=_payload(lemlist, lead, campaignId=None))
        assert no_campaign.json() == ignored
        third = await client.post(
            URL,
            json=_payload(
                lemlist, lead, _id="act_tp", type="emailsReplied", isThirdPartyReply=True
            ),
        )
        assert third.json() == ignored
        # A dry run's campaign id does not exist at Lemlist.
        dry = await client.post(URL, json=_payload("dry_cam_x", lead, _id="act_dry"))
        assert dry.json() == ignored
        assert await _events(db_session) == []

        # A known campaign, a lead nobody here holds: stored, matched to nobody.
        stranger = await client.post(URL, json=_payload(lemlist, "lea_stranger", _id="act_s"))
        assert stranger.json() == {"stored": True, "duplicate": False, "matched": False}
    [event] = await _events(db_session)
    assert (event.campaign_member_id, event.agency_id) == (None, email_world.agency_id)


async def test_a_lead_is_found_by_its_address_when_lemlist_names_no_lead(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    _, lemlist, member = await _launched(client, db_session, email_world, make_client_profile)
    with configured():
        answer = await client.post(
            URL, json=_payload(lemlist, None, leadEmail="ANA@acme.io", type="emailsOpened")
        )
    assert answer.json() == {"stored": True, "duplicate": False, "matched": True}
    [event] = await _events(db_session)
    assert event.campaign_member_id == member["id"]


async def test_archived_campaigns_and_switched_off_agencies_are_still_served(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """The job reads ACTIVE and PAUSED campaigns only: the webhook serves the
    archived ones. It depends on the env only: an agency whose module the
    platform switched off still converges."""
    cid, lemlist, member = await _launched(client, db_session, email_world, make_client_profile)
    await set_status(client, email_world.headers, cid, "ARCHIVED")
    await db_session.execute(
        update(AgencyEmailModule)
        .where(AgencyEmailModule.agency_id == email_world.agency_id)
        .values(enabled=False)
    )
    await db_session.commit()
    with configured(), email_env(EMAIL_MODULE_ENABLED="false"):
        answer = await client.post(
            URL, json=_payload(lemlist, member["provider_lead_id"], type="emailsUnsubscribed")
        )
    assert answer.json() == {"stored": True, "duplicate": False, "matched": True}
    status = await db_session.scalar(
        select(EmailCampaignMember.status)
        .where(EmailCampaignMember.id == member["id"])
        .execution_options(populate_existing=True)
    )
    assert status == "EMAIL_UNSUBSCRIBED"


async def test_a_pause_seen_by_the_webhook_is_read_again_from_lemlist(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """A PAUSED / RESUMED event says little about the state now: the lead's
    state is re-read from the provider (a read, never a write)."""
    _, lemlist, member = await _launched(client, db_session, email_world, make_client_profile)
    lead = member["provider_lead_id"]
    fake_email_provider.paused.add(lead)
    fake_email_provider.ops.clear()
    with configured():
        answer = await client.post(URL, json=_payload(lemlist, lead, type="paused"))
    assert answer.json()["stored"] is True
    row = await db_session.scalar(
        select(EmailCampaignMember)
        .where(EmailCampaignMember.id == member["id"])
        .execution_options(populate_existing=True)
    )
    assert row is not None
    assert (row.status, row.status_reason, row.paused_by_agent_id) == (
        "EMAIL_PAUSED",
        "paused_at_provider",
        None,
    )
    assert fake_email_provider.ops == []  # get_contact is a read, not recorded as an op


# ── the rate limit ───────────────────────────────────────────────────────────


async def test_the_webhook_is_rate_limited_per_real_client_ip(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism of `/webhooks/docuseal` (`src/core/ratelimit.py`), per
    REAL client IP: the first X-Forwarded-For hop behind Fly's proxy
    (`core.http.client_ip`, the consent trace's helper; decision of
    08/10/2026, risk R6), else the peer; 600 a minute in production, 3
    here. It answers before the secret is even read."""
    monkeypatch.setattr(email_router, "WEBHOOK_RATE_LIMIT", 3)
    first = {"x-forwarded-for": "203.0.113.7, 10.0.0.1"}
    answers = [
        (await client.post(URL, json={"secret": "x"}, headers=first)).status_code for _ in range(4)
    ]
    assert answers == [403, 403, 403, 429]
    # Another client behind the same proxy hop keeps its own window...
    other = {"x-forwarded-for": "198.51.100.9, 10.0.0.1"}
    assert (await client.post(URL, json={"secret": "x"}, headers=other)).status_code == 403
    # ...and so does a call without the header (the direct peer).
    answers = [(await client.post(URL, json={"secret": "x"})).status_code for _ in range(4)]
    assert answers == [403, 403, 403, 429]
    again = await client.post(URL, json={"secret": "x"}, headers=first)
    assert again.status_code == 429


async def test_nothing_is_stored_twice_under_concurrent_deliveries(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """Lemlist retries: the same delivery, again and again, is one event."""
    _, lemlist, member = await _launched(client, db_session, email_world, make_client_profile)
    payload = _payload(lemlist, member["provider_lead_id"], type="emailsOpened", _id="act_o")
    with configured():
        answers = [(await client.post(URL, json=payload)).json() for _ in range(5)]
    assert [a["stored"] for a in answers] == [True, False, False, False, False]
    assert [a["duplicate"] for a in answers] == [False, True, True, True, True]
    count = await db_session.scalar(
        select(func.count())
        .select_from(EmailProviderEvent)
        .where(EmailProviderEvent.provider_event_id == "act_o")
    )
    assert count == 1
