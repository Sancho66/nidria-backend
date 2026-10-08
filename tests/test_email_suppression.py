"""The agency's opposition list (`email_suppression`), Alexandre's decision of
08/10/2026: no refusal to delete a campaign, a DURABLE list instead.

- One fingerprint per address and agency (`suppression.email_hash`: sha256
  of the address lower-cased without blanks), its SQL mirror equal to it.
- An unsubscribe outranks a bounce, never the reverse; the source
  membership follows the reason kept, NULL once that membership is gone.
- Written each time an unsubscribe or a bounce is applied to a membership:
  an event (webhook, job, Sync), the job's reading of an unsubscribed lead,
  a Launch refused because Lemlist's unsubscribe list holds the address.
- Read wherever the durable block is: the add preview, the wizard's company
  counts, the Launch page, the Launch, a move.
- It outlives the deletion of a campaign, of a client record, a merge of
  records: a record created again with the same address stays blocked. It
  goes with its agency (CASCADE).

No test reaches Lemlist: the agency's provider is `FakeProvider`, the
network guard counts nothing.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.email_campaign import EmailCampaignMember, EmailSuppression
from shared.models.rbac import Role
from src.email_campaigns.email_repository import EmailRepository
from src.email_campaigns.providers.base import ProviderError
from src.email_campaigns.suppression import email_hash, email_hash_sql
from tests.email_fakes import FakeProvider
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    EmailWorld,
    MakeCompanyLink,
    MakeEmailCampaign,
    MakeEmailMember,
    MakeEmailSuppression,
)
from tests.test_email_campaigns import act, campaign_via_api, members_of
from tests.test_email_launch import launch_via_api, readiness_of, ready_campaign
from tests.test_email_sync_engine import (  # noqa: F401 (fixtures)
    Live,
    _lead,
    _live_campaign,
    _run,
    job_db,
    live,
)
from tests.test_email_webhook import _launched, _payload, configured

pytestmark = pytest.mark.usefixtures("rbac_baseline")

URL = "/webhooks/lemlist"


async def _listed(
    db: AsyncSession, agency_id: uuid.UUID
) -> dict[str, tuple[str, uuid.UUID | None]]:
    """The agency's opposition list: fingerprint -> (reason, source)."""
    rows = await db.execute(
        select(
            EmailSuppression.email_hash,
            EmailSuppression.reason,
            EmailSuppression.source_member_id,
        )
        .where(EmailSuppression.agency_id == agency_id)
        .execution_options(populate_existing=True)
    )
    return {digest: (reason, source) for digest, reason, source in rows.tuples().all()}


async def _preview(
    client: AsyncClient, headers: dict[str, str], cid: str, profile_ids: list[uuid.UUID]
) -> dict[str, dict[str, Any]]:
    response = await client.post(
        f"/email/campaigns/{cid}/members/preview-add",
        json={"client_profile_ids": [str(i) for i in profile_ids]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return {row["client_profile_id"]: row for row in response.json()["rows"]}


# ── the fingerprint ─────────────────────────────────────────────────────────


def test_the_fingerprint_ignores_case_and_blanks_and_keeps_nothing_else() -> None:
    plain = hashlib.sha256(b"ana.lopez@example.com").hexdigest()
    assert email_hash("ana.lopez@example.com") == plain
    assert email_hash(" Ana.Lopez@Example.COM\t") == plain
    assert email_hash("ana. lopez@exam\nple.com") == plain
    assert email_hash("ana.lopez@example.org") != plain
    assert email_hash(None) is None and email_hash(" \r\n") is None


@pytest.mark.parametrize(
    "address",
    [
        "ana@example.com",
        " Ana.Lopez@Example.COM ",
        "tab\t@example.com",
        "line\nbreak\r@example.com",
        "Élodie@Société.FR",
        "o'neil@acme.io",
        "x@acme.xn--p1ai",
    ],
)
async def test_the_sql_fingerprint_is_the_python_one(
    db_session: AsyncSession, address: str
) -> None:
    """What the wizard's counts compare in the database is what was written."""
    assert await db_session.scalar(select(email_hash_sql(literal(address)))) == email_hash(address)


# ── one row per address, an unsubscribe first ───────────────────────────────


async def test_an_unsubscribe_outranks_a_bounce_and_the_source_follows_the_reason(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
) -> None:
    agency = await make_agency()
    campaign = await make_email_campaign(agency_id=agency.id)
    first = (await make_email_member(campaign_id=campaign.id, status="EMAIL_BOUNCED")).id
    second = (await make_email_member(campaign_id=campaign.id, status="EMAIL_UNSUBSCRIBED")).id
    repo = EmailRepository(db_session)
    digest = email_hash("ana@example.com")
    assert digest is not None

    async def suppress(reason: str, source: uuid.UUID | None) -> dict[str, Any]:
        await repo.suppress(agency.id, digest, reason, source)
        await db_session.commit()
        return await _listed(db_session, agency.id)

    assert await suppress("BOUNCED", first) == {digest: ("BOUNCED", first)}
    # The same reason again: the first source stays.
    assert await suppress("BOUNCED", second) == {digest: ("BOUNCED", first)}
    # An unsubscribe outranks the bounce, and brings its source.
    assert await suppress("UNSUBSCRIBED", second) == {digest: ("UNSUBSCRIBED", second)}
    # Never the reverse.
    assert await suppress("BOUNCED", first) == {digest: ("UNSUBSCRIBED", second)}
    # Its membership gone, the next one of the same reason fills the source.
    await db_session.execute(delete(EmailCampaignMember).where(EmailCampaignMember.id == second))
    await db_session.commit()
    assert await _listed(db_session, agency.id) == {digest: ("UNSUBSCRIBED", None)}
    assert await suppress("UNSUBSCRIBED", first) == {digest: ("UNSUBSCRIBED", first)}
    # Another agency keeps its own list.
    other = await make_agency()
    await repo.suppress(other.id, digest, "BOUNCED", None)
    await db_session.commit()
    assert await _listed(db_session, other.id) == {digest: ("BOUNCED", None)}
    assert await _listed(db_session, agency.id) == {digest: ("UNSUBSCRIBED", first)}


async def test_the_reason_is_one_of_two_and_an_address_is_listed_once(
    db_session: AsyncSession, make_agency: MakeAgency
) -> None:
    agency_id = (await make_agency()).id
    digest = email_hash("ana@example.com")
    db_session.add(EmailSuppression(agency_id=agency_id, email_hash=digest, reason="OPTED_OUT"))
    with pytest.raises(IntegrityError, match="ck_email_suppression_reason"):
        await db_session.commit()
    await db_session.rollback()
    db_session.add(EmailSuppression(agency_id=agency_id, email_hash=digest, reason="BOUNCED"))
    await db_session.commit()
    db_session.add(EmailSuppression(agency_id=agency_id, email_hash=digest, reason="BOUNCED"))
    with pytest.raises(IntegrityError, match="uq_email_suppression_address"):
        await db_session.commit()
    await db_session.rollback()


# ── written each time an unsubscribe or a bounce is applied ─────────────────


async def test_an_event_lists_the_effective_address_and_the_one_lemlist_named(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """The webhook (the one ingestion of the job and the Sync too): Ana's
    unsubscribe lists her EFFECTIVE address (rule N2) and the address
    Lemlist named for the lead; a bounce lists its address as BOUNCED, an
    unsubscribe after it upgrades it, a bounce after an unsubscribe does
    not downgrade it."""
    cid, lemlist, ana = await _launched(client, db_session, email_world, make_client_profile)
    ben = await make_client_profile(
        agency_id=email_world.agency_id, first_name="Ben", email="Ben@Acme.io"
    )
    # Ben in the same campaign, launched too.
    added = await client.post(
        f"/email/campaigns/{cid}/members/add",
        json={"client_profile_ids": [str(ben.id)]},
        headers=email_world.headers,
    )
    assert added.json()["added"] == 1
    assert (await launch_via_api(client, email_world.headers, cid)).json()["members_pushed"] == 1
    rows = {m["client_profile_id"]: m for m in await members_of(db_session, cid)}
    ben_member = rows[ben.id]
    with configured():
        for body in (
            _payload(
                lemlist,
                ana["provider_lead_id"],
                _id="u1",
                type="emailsUnsubscribed",
                leadEmail="Ana.Old@Acme.io",
            ),
            _payload(lemlist, ana["provider_lead_id"], _id="b1", type="emailsBounced"),
            _payload(lemlist, ben_member["provider_lead_id"], _id="b2", type="emailsBounced"),
        ):
            answer = await client.post(URL, json=body)
            assert answer.json()["matched"] is True, body
    listed = await _listed(db_session, email_world.agency_id)
    assert listed == {
        email_hash("ana@acme.io"): ("UNSUBSCRIBED", ana["id"]),
        email_hash("ana.old@acme.io"): ("UNSUBSCRIBED", ana["id"]),
        email_hash("ben@acme.io"): ("BOUNCED", ben_member["id"]),
    }
    with configured():
        upgraded = _payload(
            lemlist, ben_member["provider_lead_id"], _id="u2", type="emailsUnsubscribed"
        )
        assert (await client.post(URL, json=upgraded)).json()["stored"] is True
    listed = await _listed(db_session, email_world.agency_id)
    ben_key = email_hash("ben@acme.io")
    assert ben_key is not None and listed[ben_key] == ("UNSUBSCRIBED", ben_member["id"])
    # No address is ever stored, only fingerprints.
    stored = await db_session.scalars(select(EmailSuppression.email_hash))
    assert all(len(d) == 64 and "@" not in d for d in stored.all())


async def test_the_job_lists_a_lead_lemlist_says_unsubscribed(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    make_client_profile: MakeClientProfile,
) -> None:
    cid, members = await _live_campaign(
        client, make_client_profile, live.world, people=("Ana Lopez", "Ben Ready")
    )
    ben = await _lead(db_session, members["Ben"])
    live.provider.unsubscribed.add(ben.lead_id)
    run = await _run(live.sessions)
    assert run.status == "success" and run.stats["unsubscribed"] == 1
    listed = await _listed(db_session, live.world.agency_id)
    assert listed == {email_hash("ben@founders.io"): ("UNSUBSCRIBED", ben.member_id)}


async def test_a_launch_refused_by_lemlists_unsubscribe_list_lists_the_address(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, _, member = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Lopez", "Ben Ready")
    )
    fake_email_provider.failures[("add_contact", "ana@acme.io")] = ProviderError(
        "Lemlist: Email is in the graveyard", code="EMAIL_UNSUBSCRIBED", status=400
    )
    result = (await launch_via_api(client, email_world.headers, cid)).json()
    assert result["members_pushed"] == 1
    assert [f["code"] for f in result["failures"]] == ["email.provider_unsubscribed"]
    rows = {m["id"]: m for m in await members_of(db_session, cid)}
    assert rows[member["Ana Lopez"]]["status"] == "EMAIL_UNSUBSCRIBED"
    listed = await _listed(db_session, email_world.agency_id)
    assert listed == {email_hash("ana@acme.io"): ("UNSUBSCRIBED", member["Ana Lopez"])}


# ── it outlives the campaign, the record, a merge; it goes with the agency ───


async def test_the_list_outlives_the_record_a_merge_and_the_campaign(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """Ana unsubscribed, Ben and Cleo bounced, through Lemlist's webhook.
    Ben's record deleted, Cleo's merged into Dora's, then their campaign
    deleted (allowed now): each entry only loses its source membership, and
    a record created again with one of the addresses is blocked, whatever
    its case and blanks."""
    headers = email_world.headers
    cid, lemlist, ana = await _launched(client, db_session, email_world, make_client_profile)
    people = {}
    for first in ("Ben", "Cleo"):
        profile = await make_client_profile(
            agency_id=email_world.agency_id, first_name=first, email=f"{first.lower()}@acme.io"
        )
        people[first] = profile.id
    added = await client.post(
        f"/email/campaigns/{cid}/members/add",
        json={"client_profile_ids": [str(i) for i in people.values()]},
        headers=headers,
    )
    assert added.json()["added"] == 2
    assert (await launch_via_api(client, headers, cid)).json()["members_pushed"] == 2
    rows = {m["client_profile_id"]: m for m in await members_of(db_session, cid)}
    ben, cleo = rows[people["Ben"]], rows[people["Cleo"]]
    with configured():
        for event_id, kind, lead in (
            ("u1", "emailsUnsubscribed", ana["provider_lead_id"]),
            ("b1", "emailsBounced", ben["provider_lead_id"]),
            ("b2", "emailsBounced", cleo["provider_lead_id"]),
        ):
            answer = await client.post(URL, json=_payload(lemlist, lead, _id=event_id, type=kind))
            assert answer.json()["matched"] is True
    ana_key, ben_key, cleo_key = (
        email_hash(f"{first}@acme.io") for first in ("ana", "ben", "cleo")
    )
    assert await _listed(db_session, email_world.agency_id) == {
        ana_key: ("UNSUBSCRIBED", ana["id"]),
        ben_key: ("BOUNCED", ben["id"]),
        cleo_key: ("BOUNCED", cleo["id"]),
    }

    # Ben's record deleted (his lead removed at Lemlist first).
    deleted = await client.delete(f"/client-profiles/{people['Ben']}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    # Cleo's record merged away into Dora's (another address).
    dora = await make_client_profile(
        agency_id=email_world.agency_id, first_name="Dora", email="dora@acme.io"
    )
    merged = await client.post(
        f"/client-profiles/{dora.id}/merge",
        json={"source_profile_id": str(people["Cleo"])},
        headers=headers,
    )
    assert merged.status_code == 200, merged.text
    assert await _listed(db_session, email_world.agency_id) == {
        ana_key: ("UNSUBSCRIBED", ana["id"]),
        ben_key: ("BOUNCED", None),
        cleo_key: ("BOUNCED", None),
    }
    # The campaign deleted, though it holds an unsubscribed recipient.
    removed = await client.delete(f"/email/campaigns/{cid}", headers=headers)
    assert removed.status_code == 200, removed.text
    assert await _listed(db_session, email_world.agency_id) == {
        ana_key: ("UNSUBSCRIBED", None),
        ben_key: ("BOUNCED", None),
        cleo_key: ("BOUNCED", None),
    }

    # Created again, with the same address however typed: blocked.
    again = {
        address: (await make_client_profile(agency_id=email_world.agency_id, email=address)).id
        for address in (" ANA@acme.io", "Ben@Acme.io", "cleo@acme.io")
    }
    following = (await campaign_via_api(client, headers, "Next"))["id"]
    plan = await _preview(client, headers, following, [*again.values(), dora.id])
    assert {address: plan[str(pid)]["eligibility"] for address, pid in again.items()} == {
        " ANA@acme.io": "UNSUBSCRIBED",
        "Ben@Acme.io": "BOUNCED",
        "cleo@acme.io": "BOUNCED",
    }
    # Listed through records gone since: no other record to name.
    assert {plan[str(pid)]["via_client_profile_id"] for pid in again.values()} == {None}
    assert plan[str(dora.id)]["eligibility"] == "READY"


async def test_the_list_goes_with_its_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    make_email_suppression: MakeEmailSuppression,
) -> None:
    gone = await make_agency(name="Closing Down")
    kept = await make_agency(name="Still Here")
    await make_email_suppression(agency_id=gone.id, email="ana@example.com")
    await make_email_suppression(agency_id=kept.id, email="ana@example.com", reason="BOUNCED")
    superadmin = await make_agent(role=system_roles["superadmin"], email="root@platform.io")
    deleted = await client.request(
        "DELETE",
        f"/agencies/{gone.id}",
        json={"confirm_name": "Closing Down"},
        headers=agent_headers(superadmin),
    )
    assert deleted.status_code == 200, deleted.text
    db_session.expunge_all()
    assert await db_session.get(Agency, gone.id) is None
    assert await _listed(db_session, gone.id) == {}
    assert await _listed(db_session, kept.id) == {email_hash("ana@example.com"): ("BOUNCED", None)}


# ── read wherever the durable block is ───────────────────────────────────────


async def test_a_listed_address_holds_back_the_launch_page_the_launch_and_a_move(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_suppression: MakeEmailSuppression,
) -> None:
    """Ana was added while her address was free; then it was listed (through
    a record deleted since). The Launch page blocks her with the reason as
    its code, the Launch refuses her the same way, and she is never moved
    to another campaign."""
    headers = email_world.headers
    cid, _, member = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Lopez", "Ben Ready")
    )
    await make_email_suppression(agency_id=email_world.agency_id, email="ANA@acme.io ")
    page = await readiness_of(client, headers, cid)
    rows = {row["lead"]["full_name"]: row for row in page["items"]}
    assert rows["Ana Lopez"]["ready"] is False
    assert [i["code"] for i in rows["Ana Lopez"]["issues"] if i["blocking"]] == ["UNSUBSCRIBED"]
    assert rows["Ben Ready"]["ready"] is True

    launched = (await launch_via_api(client, headers, cid)).json()
    assert launched["members_pushed"] == 1
    assert [(f["full_name"], f["code"]) for f in launched["failures"]] == [
        ("Ana Lopez", "UNSUBSCRIBED")
    ]
    assert [op for op in fake_email_provider.ops if op[0] == "add_contact"] == [
        ("add_contact", {"email": "ben@acme.io", "first": "Ben", "last": "Ready"})
    ]

    target = (await campaign_via_api(client, headers, "Elsewhere"))["id"]
    moved = await act(client, headers, "move", [member["Ana Lopez"]], target_campaign_id=target)
    assert moved["rows"][0]["code"] == "email.member_blocked"
    assert moved["rows"][0]["params"] == {"status": "EMAIL_UNSUBSCRIBED"}
    assert await members_of(db_session, target) == []


async def test_a_listed_address_is_neither_eligible_nor_ready_in_the_company_counts(
    client: AsyncClient,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_company_link: MakeCompanyLink,
    make_email_suppression: MakeEmailSuppression,
) -> None:
    """The counts compare fingerprints in SQL (`email_hash_sql`): an address
    typed with capitals and spaces on the record is found all the same."""
    agency = email_world.agency_id
    hal = await make_client_profile(agency_id=agency, first_name="Hal", email=" Hal@X.io ")
    ivy = await make_client_profile(agency_id=agency, first_name="Ivy", email="ivy@x.io")
    company = await make_company_link(client_profile_id=hal.id, agency_id=agency, name="Gamma")
    await make_company_link(client_profile_id=ivy.id, agency_id=agency, name="Delta")
    await make_email_suppression(agency_id=agency, email="hal@x.io", reason="BOUNCED")
    listed = (await client.get("/email/candidates/companies", headers=email_world.headers)).json()[
        "items"
    ]
    counts = {
        c["name"]: (c["clients_count"], c["eligible_clients_count"], c["ready_clients_count"])
        for c in listed
    }
    assert counts == {"Gamma": (1, 0, 0), "Delta": (1, 1, 1)}
    following = (await campaign_via_api(client, email_world.headers, "Next"))["id"]
    plan = await client.post(
        f"/email/campaigns/{following}/members/preview-add",
        json={"company_ids": [str(company.id)]},
        headers=email_world.headers,
    )
    assert [r["eligibility"] for r in plan.json()["rows"]] == ["BOUNCED"]


async def test_deleting_a_campaign_with_unsubscribed_recipients_is_allowed(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
) -> None:
    """`email.campaign_has_blocked_members` is gone (decision of 08/10/2026)."""
    cid = (await campaign_via_api(client, email_world.headers, "Dry"))["id"]
    for status in ("EMAIL_UNSUBSCRIBED", "EMAIL_BOUNCED"):
        await make_email_member(
            campaign_id=uuid.UUID(cid),
            status=status,
            removed_at=datetime.now(UTC) - timedelta(days=1),
        )
    deleted = await client.delete(f"/email/campaigns/{cid}", headers=email_world.headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["provider_paused"] is False
    remaining = await db_session.scalar(
        select(func.count())
        .select_from(EmailCampaignMember)
        .where(EmailCampaignMember.campaign_id == uuid.UUID(cid))
    )
    assert remaining == 0
