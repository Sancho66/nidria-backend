"""The text Lemlist really sent of an imported campaign's emails (Alexandre,
09/10/2026: « récupère-moi réellement les messages de toutes les
campagnes »). The job reads it from each recipient's inbox thread (`GET
/inbox/{contactId}`, which holds every email sent to them, personalized),
once the campaign's own transaction is over, a bounded number of
recipients per run, the latest sent first; the drawer serves it. A Nidria
campaign's emails keep their render. Lemlist is `FakeProvider`; no test
reaches it.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.email_campaign import EmailMessage
from src.email_campaigns import email_sync
from src.email_campaigns.providers.base import (
    LeadRef,
    ProviderError,
    ProviderLead,
    ProviderMessage,
)
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.test_email_import import (
    LEMLIST_A,
    T0,
    Lead,
    activity,
    campaign_of,
    import_,
    lemlist_campaign,
    members_by_lead,
)
from tests.test_email_sync_engine import (  # noqa: F401 (fixtures)
    Live,
    _event,
    _lead,
    _live_campaign,
    _messages,
    _provider_campaign,
    _run,
    job_db,
    live,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")

ANA_FIRST = "<p>Bonjour Ana,</p><p>Une question sur votre cabinet.</p>"
#: A follow-up sent in the same thread quotes the first email below it.
ANA_FOLLOW_UP = (
    "<p>Je me permets de revenir vers vous.</p>"
    "<blockquote><p>Bonjour Ana,</p><p>Une question sur votre cabinet.</p></blockquote>"
)


def sent(
    message_id: str,
    lead: str,
    contact: str,
    step: int,
    at: timedelta,
    subject: str,
    html: str,
    campaign: str = LEMLIST_A,
) -> ProviderMessage:
    """An email of a contact's inbox thread, as Lemlist keeps it."""
    return ProviderMessage(
        message_id=message_id,
        direction="OUTBOUND",
        occurred_at=T0 + at,
        campaign_id=campaign,
        lead_id=lead,
        contact_id=contact,
        step_position=step,
        subject=subject,
        body_html=html,
        payload={"_id": message_id, "type": "emailsSent", "campaignId": campaign},
    )


@dataclass(frozen=True)
class Imported:
    campaign_id: uuid.UUID
    ana: uuid.UUID
    ben: uuid.UUID
    cy: uuid.UUID


async def imported_campaign(client: AsyncClient, db: AsyncSession, env: Live) -> Imported:
    """Campaign A imported with its history: Ana got two emails, Ben one, Cy
    one but Lemlist names no contact for him yet (no thread to read)."""
    provider = env.provider
    lemlist_campaign(
        provider,
        LEMLIST_A,
        "2026-08 · Avocats · s2",
        [
            Lead("lea_1", "ana@acme.io", "emailsSent", contact="ctc_ana"),
            Lead(
                "lea_2", "ben@acme.io", "emailsSent", first="Ben", last="Ready", contact="ctc_ben"
            ),
            Lead("lea_3", "cy@acme.io", "emailsSent", first="Cy", last="Alone"),
        ],
    )
    provider.events = [
        activity("act_1_s1", "SENT", LEMLIST_A, "lea_1", 1, contactId="ctc_ana"),
        activity(
            "act_1_s2",
            "SENT",
            LEMLIST_A,
            "lea_1",
            2,
            at=T0 + timedelta(days=3),
            contactId="ctc_ana",
        ),
        activity(
            "act_2_s1",
            "SENT",
            LEMLIST_A,
            "lea_2",
            1,
            at=T0 - timedelta(days=1),
            contactId="ctc_ben",
        ),
        activity("act_3_s1", "SENT", LEMLIST_A, "lea_3", 1, at=T0 - timedelta(days=2)),
    ]
    provider.inbox["ctc_ana"] = [
        sent("act_1_s2", "lea_1", "ctc_ana", 2, timedelta(days=3), "", ANA_FOLLOW_UP),
        sent("act_1_s1", "lea_1", "ctc_ana", 1, timedelta(0), "Ana, une question", ANA_FIRST),
        # The thread is the CONTACT's: another campaign's email is not this one.
        sent("act_x", "lea_x", "ctc_ana", 1, timedelta(0), "Autre", "<p>Autre</p>", "cam_other"),
    ]
    provider.inbox["ctc_ben"] = [
        # Another id than the SENT event's: the one email of that step sent
        # within minutes of it, not the one Lemlist sent him two days later.
        sent(
            "act_ben_inbox",
            "lea_2",
            "ctc_ben",
            1,
            timedelta(days=-1, minutes=1),
            "Ben, une question",
            "<p>Bonjour Ben,</p>",
        ),
        sent(
            "act_ben_later",
            "lea_2",
            "ctc_ben",
            1,
            timedelta(days=1),
            "Ben, encore",
            "<p>Un autre email</p>",
        ),
    ]
    await import_(client, env.world.headers, LEMLIST_A)
    campaign = await campaign_of(db, LEMLIST_A)
    members = await members_by_lead(db, campaign.id)
    provider.ops.clear()
    return Imported(campaign.id, members["lea_1"].id, members["lea_2"].id, members["lea_3"].id)


async def outbound(db: AsyncSession, member_id: uuid.UUID) -> dict[int, EmailMessage]:
    found = await _messages(db, member_id)
    return {int(key[3:]): message for key, message in found.items() if key.startswith("out")}


def reads(env: Live) -> list[str]:
    """The inbox threads read, in order (`GET /inbox/{contactId}`)."""
    return [str(kwargs["contact"]) for name, kwargs in env.provider.ops if name == "get_messages"]


async def test_the_job_reads_the_text_lemlist_sent_and_the_drawer_serves_it(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
) -> None:
    campaign = await imported_campaign(client, db_session, live)
    # The import replays the history: Lemlist's emails, their text unread.
    for member in (campaign.ana, campaign.ben, campaign.cy):
        states = {m.content_state for m in (await outbound(db_session, member)).values()}
        assert states == {"PROVIDER_ONLY"}

    run = await _run(live.sessions)
    assert run.status == "success", run.log_output
    assert run.stats["sent_contents"] == 3
    assert (run.stats["errors"], run.stats["campaigns_failed"]) == (0, 0)
    assert "3 sent texts" in (run.log_output or "")
    # One thread per recipient, the latest sent first; none for Cy, who
    # waits for his contact id (nothing counted against him).
    assert reads(live) == ["ctc_ana", "ctc_ben"]

    ana = await outbound(db_session, campaign.ana)
    assert ana[1].content_state == "AVAILABLE"
    assert ana[1].subject == "Ana, une question"
    assert ana[1].body_text == "Bonjour Ana,\n\nUne question sur votre cabinet."
    assert ana[1].body_html == ANA_FIRST
    assert ana[1].payload["inbox"]["_id"] == "act_1_s1"
    # The follow-up: its own text, the history it quotes left out.
    assert ana[2].content_state == "AVAILABLE"
    assert (ana[2].subject, ana[2].body_text) == (None, "Je me permets de revenir vers vous.")
    ben = await outbound(db_session, campaign.ben)
    assert (ben[1].content_state, ben[1].body_text) == ("AVAILABLE", "Bonjour Ben,")
    cy = await outbound(db_session, campaign.cy)
    assert (cy[1].content_state, cy[1].content_attempts) == ("PROVIDER_ONLY", 0)

    detail = await client.get(f"/email/members/{campaign.ana}", headers=live.world.headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    first, second = sorted(body["steps"], key=lambda s: s["position"])[:2]
    assert (first["content_state"], first["subject"]) == ("AVAILABLE", "Ana, une question")
    assert first["body"] == "Bonjour Ana,\n\nUne question sur votre cabinet."
    assert (second["content_state"], second["body"]) == (
        "AVAILABLE",
        "Je me permets de revenir vers vous.",
    )
    sent_items = [c for c in body["conversation"] if c["direction"] == "OUTBOUND"]
    assert [c["body"] for c in sent_items] == [
        "Bonjour Ana,\n\nUne question sur votre cabinet.",
        "Je me permets de revenir vers vous.",
    ]
    cy_detail = (
        await client.get(f"/email/members/{campaign.cy}", headers=live.world.headers)
    ).json()
    [cy_step] = [s for s in cy_detail["steps"] if s["position"] == 1]
    assert (cy_step["content_state"], cy_step["subject"], cy_step["body"]) == (
        "PROVIDER_ONLY",
        None,
        None,
    )

    # Nothing left to read: the next run reads no thread.
    again = await _run(live.sessions)
    assert (again.stats["sent_contents"], reads(live)) == (0, ["ctc_ana", "ctc_ben"])


async def test_a_sync_asked_from_the_interface_does_not_wait_for_the_texts(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
) -> None:
    """The Sync button reads the campaign as the job does, but leaves the
    sent texts to the job's next run: no thread is read while the agent waits."""
    campaign = await imported_campaign(client, db_session, live)
    synced = await client.post(
        f"/email/campaigns/{campaign.campaign_id}/sync", headers=live.world.headers
    )
    assert synced.status_code == 200, synced.text
    assert reads(live) == []
    states = {m.content_state for m in (await outbound(db_session, campaign.ana)).values()}
    assert states == {"PROVIDER_ONLY"}
    run = await _run(live.sessions)
    assert run.stats["sent_contents"] == 3


async def test_a_bounded_number_of_recipients_per_run_the_latest_sent_first(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(email_sync, "SENT_CONTENTS_PER_RUN", 1)
    campaign = await imported_campaign(client, db_session, live)
    first = await _run(live.sessions)
    assert (first.stats["sent_contents"], reads(live)) == (2, ["ctc_ana"])
    assert {m.content_attempts for m in (await outbound(db_session, campaign.ben)).values()} == {0}
    second = await _run(live.sessions)
    assert (second.stats["sent_contents"], reads(live)) == (1, ["ctc_ana", "ctc_ben"])
    # Cy has no contact: never his turn, nothing counted against him.
    third = await _run(live.sessions)
    assert (third.stats["sent_contents"], reads(live)) == (0, ["ctc_ana", "ctc_ben"])
    cy = await outbound(db_session, campaign.cy)
    assert (cy[1].content_state, cy[1].content_attempts) == ("PROVIDER_ONLY", 0)


async def test_a_recipient_without_a_contact_waits_for_one(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
) -> None:
    """The inbox is keyed by the contact: a recipient Lemlist named no
    contact for is not read, and nothing is counted against him, however
    many runs pass; once a listing gives his contact, his text is read."""
    campaign = await imported_campaign(client, db_session, live)
    for _ in range(4):
        await _run(live.sessions)
    assert "None" not in reads(live) and "" not in reads(live)
    cy = await outbound(db_session, campaign.cy)
    assert (cy[1].content_state, cy[1].content_attempts) == ("PROVIDER_ONLY", 0)
    # Lemlist's listing now names his contact (c6dfcc6: a lead imported
    # without one receives it later), and his thread holds the email.
    provider = live.provider
    provider.listed[LEMLIST_A] = [
        lead
        if lead.lead_id != "lea_3"
        else ProviderLead(
            "lea_3",
            None,
            False,
            state="emailsSent",
            contact_id="ctc_cy",
            raw={"state": "emailsSent", "contactId": "ctc_cy"},
        )
        for lead in provider.listed[LEMLIST_A]
    ]
    provider.inbox["ctc_cy"] = [
        sent(
            "act_3_s1",
            "lea_3",
            "ctc_cy",
            1,
            timedelta(days=-2),
            "Cy, une question",
            "<p>Bonjour Cy,</p>",
        )
    ]
    run = await _run(live.sessions)
    assert (run.stats["sent_contents"], reads(live)[-1]) == (1, "ctc_cy")
    cy = await outbound(db_session, campaign.cy)
    assert (cy[1].content_state, cy[1].body_text) == ("AVAILABLE", "Bonjour Cy,")


async def test_an_email_the_thread_lacks_is_read_again_then_left_to_lemlist(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
) -> None:
    campaign = await imported_campaign(client, db_session, live)
    live.provider.inbox["ctc_ana"] = []
    for attempt in (1, 2, 3):
        run = await _run(live.sessions)
        assert run.status == "success", run.log_output
        ana = await outbound(db_session, campaign.ana)
        assert {(m.content_state, m.content_attempts) for m in ana.values()} == {
            ("PROVIDER_ONLY", attempt)
        }
    assert reads(live).count("ctc_ana") == 3
    # Three reads without it: never asked again.
    await _run(live.sessions)
    assert reads(live).count("ctc_ana") == 3
    ana = await outbound(db_session, campaign.ana)
    assert {(m.content_state, m.content_attempts) for m in ana.values()} == {("PROVIDER_ONLY", 3)}


async def test_a_failing_read_keeps_the_campaign_reconciled_and_resumes_next_run(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
) -> None:
    campaign = await imported_campaign(client, db_session, live)
    live.provider.failures[("get_messages", "ctc_ana")] = ProviderError(
        "Lemlist rate limit persisted.", code="RATE_LIMITED", status=429
    )
    run = await _run(live.sessions)
    assert run.status == "success", run.log_output
    assert (run.stats["campaigns_failed"], run.stats["sent_contents"]) == (0, 0)
    assert run.stats["errors"] == 1
    # The read stopped at the failure: Ben waits, nothing counted against them.
    assert reads(live) == ["ctc_ana"]
    for member in (campaign.ana, campaign.ben):
        assert {m.content_attempts for m in (await outbound(db_session, member)).values()} == {0}
    # The campaign's own reading was kept.
    reconciled = await campaign_of(db_session, LEMLIST_A)
    assert reconciled.last_provider_sync_at is not None
    del live.provider.failures[("get_messages", "ctc_ana")]
    after = await _run(live.sessions)
    assert after.stats["sent_contents"] == 3


async def test_nothing_is_locked_while_lemlist_answers(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The threads are read once the campaign's transaction is over: the
    campaign row is free all along (a pause asked meanwhile never waits)."""
    campaign = await imported_campaign(client, db_session, live)
    locked: list[str] = []
    original = live.provider.get_messages

    def probing(lead: LeadRef) -> list[ProviderMessage]:
        with live.sessions() as db:
            try:
                db.execute(
                    text("SELECT id FROM email_campaign WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": campaign.campaign_id},
                )
            except OperationalError:
                locked.append(lead.contact_id or "")
            finally:
                db.rollback()
        return original(lead)

    monkeypatch.setattr(live.provider, "get_messages", probing)
    run = await _run(live.sessions)
    assert run.stats["sent_contents"] == 3, run.log_output
    assert locked == []


async def test_a_nidria_campaign_keeps_its_render(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    make_client_profile: MakeClientProfile,
) -> None:
    """Nidria rendered its own campaign's emails: the inbox never replaces
    them, and its threads are not read for them."""
    cid, members = await _live_campaign(
        client, make_client_profile, live.world, people=("Ana Lopez",), steps=1
    )
    lemlist = await _provider_campaign(db_session, cid)
    ana = await _lead(db_session, members["Ana"])
    assert ana.contact_id is not None
    live.provider.events = [_event("s1", "SENT", lemlist, ana.lead_id, 1, contactId=ana.contact_id)]
    live.provider.inbox[ana.contact_id] = [
        sent(
            "s1",
            ana.lead_id,
            ana.contact_id,
            1,
            timedelta(0),
            "Autre",
            "<p>Autre texte</p>",
            lemlist,
        )
    ]
    live.provider.ops.clear()
    run = await _run(live.sessions)
    assert run.status == "success", run.log_output
    assert (run.stats["sent_contents"], reads(live)) == (0, [])
    [message] = (await outbound(db_session, ana.member_id)).values()
    assert (message.content_state, message.body_html) == ("RENDERED", None)
    detail = (
        await client.get(f"/email/members/{ana.member_id}", headers=live.world.headers)
    ).json()
    [step] = detail["steps"]
    assert step["content_state"] == "RENDERED" and step["subject"] != "Autre"
