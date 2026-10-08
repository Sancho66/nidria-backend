"""The Email module's providers: the Lemlist adapter against a simulated
Lemlist (`FakeLemlist`, httpx.MockTransport: no network, the guard counts
nothing), and the dry-run provider.

Ported from Prism's `tests/test_email_rendering.py` L271-806 (reference
629a2b0), with Nidria's shells, the error codes of spec 9.1, the 429
Retry-After cap, pagination, and the calls Nidria adds outside the
EmailProvider protocol (`GET /team`, `GET /user/channels` for the plan,
`GET` / `POST /hooks`). Not ported: Prism's `lemlist_lead_url` (decision A9).
The registry and its external-effects rule are proven in
`test_email_registry.py`."""

import base64
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from src.email_campaigns.providers import lemlist as lemlist_module
from src.email_campaigns.providers.base import (
    CampaignRef,
    CampaignSpec,
    EmailProvider,
    LeadRef,
    LeadSpec,
    ProviderError,
    ScheduleSpec,
    StepContent,
    StepSpec,
)
from src.email_campaigns.providers.dry_run import DryRunEmailProvider, is_dry_run_id
from src.email_campaigns.providers.lemlist import (
    LemlistEmailProvider,
    LemlistHook,
    TeamUnknownError,
    normalize_activity,
)
from tests.email_fakes import FAKE_LEMLIST_KEY, FakeLemlist, lemlist_double

TRACKING = {"trackOpens": True, "trackClicks": True, "trackReplies": True}
EM_DASH = chr(0x2014)  # spelled out: no em dash in the source

Double = tuple[FakeLemlist, LemlistEmailProvider]


@pytest.fixture
def fake_lemlist(monkeypatch: pytest.MonkeyPatch) -> Double:
    return lemlist_double(monkeypatch)


def test_both_providers_honour_the_contract() -> None:
    # Checked by mypy (structural typing) and at run time on the names.
    providers: list[EmailProvider] = [DryRunEmailProvider(), LemlistEmailProvider("k")]
    assert [p.external for p in providers] == [False, True]
    assert {p.name for p in providers} == {"LEMLIST"}


# ── Lemlist activities ───────────────────────────────────────────────────


def test_a_lemlist_activity_is_normalized() -> None:
    event = normalize_activity(
        {
            "_id": "act_1",
            "type": "emailsReplied",
            "createdAt": "2026-10-04T10:00:00.000Z",
            "campaignId": "cam_1",
            "leadId": "lea_1",
            "leadEmail": "jane@example.com",
            "sequenceStep": 1,
            "secret": "never-kept",
        }
    )
    assert event is not None
    assert (event.type, event.step_position, event.lead_id) == ("REPLIED", 2, "lea_1")
    assert event.occurred_at.isoformat() == "2026-10-04T10:00:00+00:00"
    assert event.email == "jane@example.com"
    assert "secret" not in event.payload
    assert normalize_activity({"_id": "x", "type": "linkedinVisitDone"}) is None
    assert normalize_activity({"type": "emailsSent"}) is None
    bounced = normalize_activity(
        {"_id": "b", "type": "emailsBounced", "to": [{"address": "x@example.com"}], "reason": "r"}
    )
    assert bounced is not None and (bounced.email, bounced.reason) == ("x@example.com", "r")


# ── campaigns ────────────────────────────────────────────────────────────


def test_creating_a_campaign_sets_stop_on_reply_tracking_and_its_own_schedule(
    fake_lemlist: Double,
) -> None:
    fake, provider = fake_lemlist
    fake.queue(
        "POST",
        "/campaigns",
        200,
        {"_id": "cam_1", "sequenceId": "seq_1", "scheduleIds": ["skd_1"]},
    )
    ref = provider.create_campaign(
        CampaignSpec(
            name="Founders",
            schedule=ScheduleSpec(
                timezone="Europe/Paris", weekdays=(1, 2, 3), start="09:00", end="17:30"
            ),
        )
    )
    assert ref == CampaignRef("cam_1", "seq_1", "skd_1")
    assert fake.routes() == [
        ("POST", "/campaigns"),
        ("PATCH", "/campaigns/cam_1"),
        ("PATCH", "/schedules/skd_1"),
    ]
    assert fake.calls[0][3] == {"name": "Founders", "autoReview": False, "timezone": "Europe/Paris"}
    assert fake.calls[1][3] == {
        "name": "Founders",
        "stopOnEmailReplied": True,
        "disableTrackClick": False,
        "tracking": TRACKING,
    }
    assert fake.calls[2][3] == {
        "timezone": "Europe/Paris",
        "start": "09:00",
        "end": "17:30",
        "weekdays": [1, 2, 3],
    }
    fake.queue("POST", "/campaigns", 200, {"name": "no id"})
    with pytest.raises(ProviderError) as bad:
        provider.create_campaign(CampaignSpec(name="x"))
    assert bad.value.code == "BAD_RESPONSE"


def test_calls_use_basic_auth_with_an_empty_login_and_the_key(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    provider.pause_campaign(CampaignRef("cam_1"))
    expected = "Basic " + base64.b64encode(f":{FAKE_LEMLIST_KEY}".encode()).decode()
    assert fake.authorizations == [expected]


def test_a_campaign_lemlist_lost_reads_as_missing(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    fake.queue("GET", "/campaigns/cam_1", 404, {"error": "Campaign not found"})
    assert provider.read_campaign(CampaignRef("cam_1")).exists is False
    fake.queue("GET", "/campaigns/cam_1", 200, {})
    assert provider.read_campaign(CampaignRef("cam_1")).exists is False
    fake.queue(
        "GET", "/campaigns/cam_1", 200, {"_id": "cam_1", "status": "running", "secretKey": "x"}
    )
    found = provider.read_campaign(CampaignRef("cam_1"))
    assert (found.exists, found.status, found.metadata) == (True, "running", {"status": "running"})


# ── steps ────────────────────────────────────────────────────────────────


def test_steps_are_shells_only_the_first_with_a_subject(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    fake.queue("POST", "/sequences/seq_1/steps", 200, {"_id": "stp_1"})
    step_id = provider.create_step(
        CampaignRef("cam_1", "seq_1"), StepSpec(position=2, name="Relance", delay_days=3)
    )
    assert step_id == "stp_1"
    body = fake.calls[0][3]
    assert body["type"] == "email" and body["delay"] == 3
    # A follow-up has no subject: it answers in the first email's thread.
    assert "subject" not in body
    # The first email needs one, or Lemlist refuses to launch the leads.
    fake.queue("POST", "/sequences/seq_1/steps", 200, {"_id": "stp_0"})
    provider.create_step(CampaignRef("cam_1", "seq_1"), StepSpec(1, "Premier", 0))
    assert fake.calls[1][3]["subject"] == "Managed by Nidria"
    # The shell says what it is: the content each lead gets is Nidria's.
    assert body["message"] == (
        "<p>Content managed by Nidria.</p>"
        "<p>Personalized subject and message are provided individually for each lead.</p>"
    )
    provider.update_step(CampaignRef("cam_1", "seq_1"), "stp_0", StepSpec(1, "Premier", 0))
    provider.update_step(CampaignRef("cam_1", "seq_1"), "stp_1", StepSpec(2, "Relance", 3))
    assert [call[3]["subject"] for call in fake.calls[2:4]] == ["Managed by Nidria", ""]
    # An update refreshes an older shell's placeholder too.
    assert all("Content managed by Nidria." in call[3]["message"] for call in fake.calls[2:4])
    # Nothing Prism's, no em dash: Lemlist's app shows these texts (spec 9.3).
    for call in fake.calls:
        text = f"{call[3].get('subject', '')} {call[3]['message']}"
        assert "Prism" not in text and EM_DASH not in text
    with pytest.raises(ProviderError) as missing:
        provider.create_step(CampaignRef("cam_1", None), StepSpec(1, "x", 0))
    assert missing.value.code == "NO_SEQUENCE"


def test_the_sequence_is_read_in_order_and_a_step_is_parked_out_of_reach(
    fake_lemlist: Double,
) -> None:
    fake, provider = fake_lemlist
    ref = CampaignRef("cam_1", "seq_1")
    # Lemlist's answer, as read by Prism: keyed by sequence, 0-based sequenceStep.
    fake.queue(
        "GET",
        "/campaigns/cam_1/sequences",
        200,
        {
            "seq_1": {
                "_id": "seq_1",
                "level": 0,
                "steps": [
                    {"_id": "stp_b", "index": 2, "sequenceStep": 1, "delay": 1},
                    {"_id": "stp_a", "index": 1, "sequenceStep": 0, "delay": 0},
                    {"_id": "stp_c", "index": 3, "sequenceStep": 2, "delay": 1500},
                ],
            },
            "seq_branch": {"_id": "seq_branch", "level": 1, "steps": [{"_id": "stp_x"}]},
        },
    )
    steps = provider.read_steps(ref)
    assert [(s.step_id, s.position, s.delay_days) for s in steps] == [
        ("stp_a", 1, 0),
        ("stp_b", 2, 1),
        ("stp_c", 3, 1500),
    ]
    provider.park_step(ref, "stp_c")
    method, path, _, body = fake.calls[-1]
    assert (method, path) == ("PATCH", "/sequences/seq_1/steps/stp_c")
    assert body["delay"] == lemlist_module.PARKED_DELAY_DAYS == 1500
    assert body["subject"] == "" and "Content managed by Nidria." in body["message"]
    # A reorder only moves the delays, step by step.
    fake.calls.clear()
    provider.reorder_steps(ref, [("stp_b", StepSpec(1, "B", 0)), ("stp_a", StepSpec(2, "A", 2))])
    assert [(p, b["delay"], b["subject"]) for _, p, _, b in fake.calls] == [
        ("/sequences/seq_1/steps/stp_b", 0, "Managed by Nidria"),
        ("/sequences/seq_1/steps/stp_a", 2, ""),
    ]
    # Deleting a step Lemlist no longer has is done already.
    fake.queue("DELETE", "/sequences/seq_1/steps/stp_z", 404, {"error": "not found"})
    provider.delete_step(ref, "stp_z")


def test_lemlist_refusals_once_leads_entered_have_their_own_codes(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    ref = CampaignRef("cam_1", "seq_1")
    # The two refusals Prism measured on 2026-10-05.
    fake.queue(
        "DELETE",
        "/sequences/seq_1/steps/stp_1",
        409,
        {
            "error": "You can't remove steps once leads have entered the campaign",
            "code": "remove-step-on-sequence-in-use",
        },
    )
    with pytest.raises(ProviderError) as removal:
        provider.delete_step(ref, "stp_1")
    assert removal.value.code == "STEP_IN_USE"
    fake.queue(
        "POST",
        "/sequences/seq_1/steps",
        409,
        {
            "error": "You cannot insert a step at this position once leads have entered the "
            "campaign: new steps can only be appended after the last one",
            "code": "SEQUENCE_STEP_INSERT_ON_SEQUENCE_IN_USE",
        },
    )
    with pytest.raises(ProviderError) as insert:
        provider.create_step(ref, StepSpec(2, "Between", 1))
    assert insert.value.code == "INSERT_IN_USE"


# ── leads ────────────────────────────────────────────────────────────────


def test_adding_a_lead_sends_only_what_it_carries_and_adopts_its_own_lead(
    fake_lemlist: Double,
) -> None:
    fake, provider = fake_lemlist
    ref = CampaignRef("cam_1", "seq_1")
    # Nidria's lead (decision A8): email, first and last name, nothing else.
    lead = LeadSpec(email="marie@example.com", first_name="Marie", last_name="Martin")
    fake.queue("POST", "/campaigns/cam_1/leads/", 200, {"_id": "lea_1", "contactId": "ctc_1"})
    assert provider.add_contact(ref, lead) == LeadRef("lea_1", "ctc_1")
    _, _, params, body = fake.calls[0]
    assert params == {"deduplicate": "true", "updateStrategy": "fillEmptyOnly"}
    assert body == {"email": "marie@example.com", "firstName": "Marie", "lastName": "Martin"}
    # Empty fields are left out, whatever the spec carries.
    fake.queue("POST", "/campaigns/cam_1/leads/", 200, {"_id": "lea_2"})
    provider.add_contact(ref, LeadSpec(email="jo@example.com", first_name=None, job_title="CEO"))
    assert fake.calls[-1][3] == {"email": "jo@example.com", "jobTitle": "CEO"}

    # An earlier push whose answer was lost: the lead is found, not duplicated.
    fake.queue("POST", "/campaigns/cam_1/leads/", 400, "Lead already in the campaign")
    fake.queue("GET", "/leads", 200, {"_id": "lea_9", "campaignId": "cam_1", "contactId": "c"})
    assert provider.add_contact(ref, lead) == LeadRef("lea_9", "c")
    assert fake.calls[-1][2] == {"email": "marie@example.com", "version": "v2"}
    # Found, but in another campaign: still refused.
    fake.queue("POST", "/campaigns/cam_1/leads/", 400, {"code": "LEAD_ALREADY_IN_CAMPAIGN"})
    fake.queue("GET", "/leads", 200, {"_id": "lea_8", "campaignId": "cam_other"})
    with pytest.raises(ProviderError) as elsewhere:
        provider.add_contact(ref, lead)
    assert elsewhere.value.code == "ALREADY_IN_CAMPAIGN"

    # Already in ANOTHER campaign: refused, never moved behind Nidria's back.
    fake.queue("POST", "/campaigns/cam_1/leads/", 409, "Lead already in other campaign")
    with pytest.raises(ProviderError) as other:
        provider.add_contact(ref, lead)
    assert other.value.code == "ALREADY_IN_OTHER_CAMPAIGN"
    # Lemlist's unsubscribed list (its "graveyard").
    fake.queue("POST", "/campaigns/cam_1/leads/", 400, {"error": "Email is in the graveyard"})
    with pytest.raises(ProviderError) as graveyard:
        provider.add_contact(ref, lead)
    assert graveyard.value.code == "EMAIL_UNSUBSCRIBED"


def test_personalization_and_launch_answers(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    ref, lead = CampaignRef("cam_1", "seq_1"), LeadRef("lea_1")
    path = "/campaigns/cam_1/leads/lea_1/steps/stp_2/personalization"
    content = StepContent(subject="", html="<p>Hi</p>")
    assert provider.set_step_content(ref, lead, "stp_2", content) is True
    assert fake.calls[-1][:2] == ("PUT", path)
    assert fake.calls[-1][3] == {"subject": "", "message": "<p>Hi</p>"}
    fake.queue("PUT", path, 409, {"error": "reached", "code": "REVIEW_DRAFT_STEP_ALREADY_REACHED"})
    assert provider.set_step_content(ref, lead, "stp_2", content) is False
    fake.queue("PUT", path, 409, {"error": "AI off", "code": "CAMPAIGN_AI_DISCONTINUED"})
    with pytest.raises(ProviderError) as refused:
        provider.set_step_content(ref, lead, "stp_2", content)
    assert refused.value.code == "CONFLICT"

    fake.queue(
        "POST",
        "/leads/review/lea_1",
        400,
        {
            "error": "CAMPAIGN_LEAD_REVIEW_LEAD_ALREADY_LAUNCHED",
            "message": "Lead is already launched",
        },
    )
    provider.launch_contact(lead)  # idempotent
    fake.queue("POST", "/leads/review/lea_1", 400, {"error": "CAMPAIGN_LEAD_REVIEW_LEAD_PAUSED"})
    with pytest.raises(ProviderError):
        provider.launch_contact(lead)
    # Launching needs the emailPro plan: Nidria never upgrades it.
    fake.queue("POST", "/leads/review/lea_1", 402, {"message": "Payment required"})
    with pytest.raises(ProviderError) as plan:
        provider.launch_contact(lead)
    assert (plan.value.code, plan.value.status) == ("PLAN_REQUIRED", 402)


def test_pause_resume_remove_and_events_use_the_documented_routes(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    ref, lead = CampaignRef("cam_1", "seq_1"), LeadRef("lea_1")
    provider.pause_contact(ref, lead)
    provider.resume_contact(ref, lead)
    provider.remove_contact(ref, lead)
    fake.queue(
        "GET",
        "/activities",
        200,
        [
            {"_id": "a1", "type": "emailsSent", "campaignId": "cam_1", "sequenceStep": 0},
            {"_id": "a2", "type": "linkedinVisitDone", "campaignId": "cam_1"},
        ],
    )
    events = provider.sync_campaign(ref, None)
    assert [(m, p, q) for m, p, q, _ in fake.calls] == [
        ("POST", "/leads/pause/lea_1", {"campaignId": "cam_1"}),
        ("POST", "/leads/start/lea_1", {"campaignId": "cam_1"}),
        ("DELETE", "/campaigns/cam_1/leads/lea_1", {"action": "remove"}),
        (
            "GET",
            "/activities",
            {"campaignId": "cam_1", "version": "v2", "limit": "100", "offset": "0"},
        ),
    ]
    assert [(e.event_id, e.type, e.step_position) for e in events] == [("a1", "SENT", 1)]
    # A lead Lemlist no longer has is removed already.
    fake.queue("DELETE", "/campaigns/cam_1/leads/lea_2", 404, {"error": "not found"})
    provider.remove_contact(ref, LeadRef("lea_2"))


def test_one_leads_events_and_its_state(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    ref, lead = CampaignRef("cam_1"), LeadRef("lea_1")
    fake.queue("GET", "/activities", 200, [{"_id": "a1", "type": "emailsOpened"}])
    [event] = provider.sync_contact(ref, lead)
    assert event.type == "OPENED"
    assert fake.calls[-1][2] == {
        "campaignId": "cam_1",
        "leadId": "lea_1",
        "version": "v2",
        "limit": "100",
        "offset": "0",
    }
    fake.queue(
        "GET",
        "/leads",
        200,
        {"_id": "lea_1", "state": "paused", "contactId": "ctc_1", "isUnsubscribed": True},
    )
    state = provider.get_contact(lead)
    assert state is not None
    assert (state.is_paused, state.unsubscribed, state.contact_id) == (True, True, "ctc_1")
    assert state.raw == {"state": "paused", "contactId": "ctc_1", "isUnsubscribed": True}
    fake.queue("GET", "/leads", 404, {"error": "not found"})
    assert provider.get_contact(lead) is None


# ── pagination ───────────────────────────────────────────────────────────


def test_leads_are_listed_page_by_page_until_a_short_page(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    ref = CampaignRef("cam_1")
    fake.queue("GET", "/campaigns/cam_1/leads", 200, [{"_id": f"l{i}"} for i in range(100)])
    fake.queue(
        "GET",
        "/campaigns/cam_1/leads",
        200,
        {"leads": [{"_id": "l100", "isPaused": True}, {"email": "no-id@example.com"}]},
    )
    listing = provider.list_contacts(ref)
    assert listing.complete is True
    assert len(listing.leads) == 101 and listing.leads[-1].is_paused is True
    assert [q for _, _, q, _ in fake.calls] == [
        {"limit": "100", "offset": "0"},
        {"limit": "100", "offset": "100"},
    ]
    # An empty answer ends the listing; no JSON is a bad answer.
    fake.queue("GET", "/campaigns/cam_1/leads", 200, "")
    assert provider.list_contacts(ref).leads == []
    fake.queue("GET", "/campaigns/cam_1/leads", 200, "<html>")
    with pytest.raises(ProviderError) as bad:
        provider.list_contacts(ref)
    assert bad.value.code == "BAD_RESPONSE"


def test_past_the_page_cap_a_leads_listing_is_incomplete(
    fake_lemlist: Double, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, provider = fake_lemlist
    monkeypatch.setattr(lemlist_module, "_MAX_LEAD_PAGES", 2)
    for page in range(2):
        fake.queue(
            "GET",
            "/campaigns/cam_1/leads",
            200,
            [{"_id": f"p{page}-{i}"} for i in range(100)],
        )
    listing = provider.list_contacts(CampaignRef("cam_1"))
    # A lead's absence from it proves nothing.
    assert (listing.complete, len(listing.leads), len(fake.calls)) == (False, 200, 2)


def test_activities_are_read_page_by_page_since_a_date(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    fake.queue(
        "GET",
        "/activities",
        200,
        [{"_id": f"a{i}", "type": "emailsSent", "sequenceStep": 0} for i in range(100)],
    )
    fake.queue("GET", "/activities", 200, [{"_id": "a100", "type": "emailsClicked"}])
    since = datetime(2026, 10, 7, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    events = provider.sync_campaign(CampaignRef("cam_1"), since)
    assert len(events) == 101 and events[-1].type == "CLICKED"
    assert [q["offset"] for _, _, q, _ in fake.calls] == ["0", "100"]
    assert fake.calls[0][2]["minDate"] == since.astimezone(UTC).isoformat()


def test_the_inbox_thread_is_read_by_contact_page_by_page(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    assert provider.get_messages(LeadRef("lea_1")) == []  # no contact id: no call
    assert fake.calls == []
    first_page = [
        {
            "_id": f"m{i}",
            "type": "emailsSent",
            "sequenceStep": 0,
            "subject": "S",
            "message": "<p>Hi</p>",
            "createdAt": "2026-10-05T09:00:00Z",
        }
        for i in range(100)
    ]
    fake.queue("GET", "/inbox/ctc_1", 200, {"data": first_page, "pagination": {"nextPage": 2}})
    fake.queue(
        "GET",
        "/inbox/ctc_1",
        200,
        {
            "data": [
                {"_id": "r1", "type": "emailsReplied", "message": "<p>Oui</p>", "leadId": "lea_1"},
                {"_id": "x1", "type": "linkedinSent"},
            ],
            "pagination": {"nextPage": None},
        },
    )
    messages = provider.get_messages(LeadRef("lea_1", "ctc_1"))
    assert len(messages) == 101
    assert (messages[0].direction, messages[0].step_position) == ("OUTBOUND", 1)
    reply = messages[-1]
    assert (reply.direction, reply.body_html, reply.lead_id) == ("INBOUND", "<p>Oui</p>", "lea_1")
    # The body is kept apart from the record.
    assert "message" not in reply.payload
    assert [q for _, _, q, _ in fake.calls] == [
        {"limit": "100", "skip": "0"},
        {"limit": "100", "skip": "100"},
    ]


# ── senders ──────────────────────────────────────────────────────────────


def test_starting_gives_the_campaign_its_sender_and_never_fails_on_a_running_one(
    fake_lemlist: Double,
) -> None:
    fake, provider = fake_lemlist
    ref = CampaignRef("cam_1")
    # Created by the API, a campaign has no sender: the team's only one is given.
    fake.queue("GET", "/campaigns/cam_1", 200, {"senders": [], "sendUsers": []})
    fake.queue("GET", "/team/senders", 200, [{"userId": "usr_1", "campaigns": []}])
    fake.queue(
        "POST", "/campaigns/cam_1/start", 400, "You can't start campaigns that are already running."
    )
    provider.start_campaign(ref)
    assert [(m, p, b) for m, p, _, b in fake.calls] == [
        ("GET", "/campaigns/cam_1", None),
        ("GET", "/team/senders", None),
        ("PATCH", "/campaigns/cam_1", {"sendUserIds": ["usr_1"]}),
        ("POST", "/campaigns/cam_1/start", None),
    ]
    # A sender already there: nothing else is asked.
    fake.calls.clear()
    fake.queue("GET", "/campaigns/cam_1", 200, {"senders": [{"userId": "usr_1"}]})
    provider.start_campaign(ref)
    assert [p for _, p, _, _ in fake.calls] == ["/campaigns/cam_1", "/campaigns/cam_1/start"]
    # Several senders: the choice is a person's, and nothing starts.
    fake.calls.clear()
    fake.queue("GET", "/campaigns/cam_1", 200, {"senders": []})
    fake.queue("GET", "/team/senders", 200, [{"userId": "usr_1"}, {"userId": "usr_2"}])
    with pytest.raises(ProviderError) as choose:
        provider.start_campaign(ref)
    assert choose.value.code == "SENDER_REQUIRED" and choose.value.params == {"senders": 2}
    assert "Sent from" in str(choose.value)
    assert ("POST", "/campaigns/cam_1/start") not in fake.routes()
    # Nobody at all: a mailbox must be connected in Lemlist.
    fake.queue("GET", "/campaigns/cam_1", 200, {"senders": []})
    fake.queue("GET", "/team/senders", 200, [])
    fake.queue("GET", "/team", 200, {"_id": "tea_1", "userIds": []})
    with pytest.raises(ProviderError) as nobody:
        provider.start_campaign(ref)
    assert nobody.value.code == "SENDER_REQUIRED" and nobody.value.params == {"senders": 0}
    # Any other refusal still stops; pausing a paused campaign does not.
    fake.queue("GET", "/campaigns/cam_1", 200, {"senders": [{"userId": "usr_1"}]})
    fake.queue("POST", "/campaigns/cam_1/start", 400, "Campaign has step errors")
    with pytest.raises(ProviderError):
        provider.start_campaign(ref)
    fake.queue("POST", "/campaigns/cam_1/pause", 400, "Campaign is already paused")
    provider.pause_campaign(ref)
    # Lemlist's own words for a synced draft, paused since its creation:
    # pausing or archiving it is already done.
    for action in (provider.pause_campaign, provider.archive_campaign):
        fake.queue(
            "POST", "/campaigns/cam_1/pause", 400, "You can't pause campaigns that are not running."
        )
        action(ref)
    fake.queue("POST", "/campaigns/cam_1/pause", 400, "Campaign not found")
    with pytest.raises(ProviderError):
        provider.pause_campaign(ref)


def test_the_team_senders_come_with_their_mailbox_and_the_chosen_one_is_sent(
    fake_lemlist: Double,
) -> None:
    fake, provider = fake_lemlist
    # The senders route lists user ids; a campaign each sends shows its address.
    fake.queue(
        "GET",
        "/team/senders",
        200,
        [
            {"userId": "usr_2", "campaigns": []},
            {"userId": "usr_1", "campaigns": [{"_id": "cam_9", "name": "A"}]},
        ],
    )
    fake.queue(
        "GET", "/campaigns/cam_9", 200, {"senders": [{"id": "usr_1", "email": "ro@pp.example"}]}
    )
    senders = provider.list_senders()
    assert [(s.user_id, s.email, s.campaigns) for s in senders] == [
        ("usr_1", "ro@pp.example", 1),
        ("usr_2", None, 0),
    ]
    # A team of one user (a new team: nobody sends a campaign yet): each of the
    # API key's mailboxes is a choice, all of that user.
    fake.calls.clear()
    fake.queue("GET", "/team/senders", 200, [])
    fake.queue("GET", "/team", 200, {"_id": "tea_1", "userIds": ["usr_3"]})
    channels = {
        "email": {
            "accounts": [
                {"id": "usm_b", "email": "chloe@x.example", "provider": "google"},
                {"id": "usm_a", "email": "me@x.example", "provider": "microsoft"},
            ]
        }
    }
    fake.queue("GET", "/user/channels", 200, channels)
    assert [(s.user_id, s.email) for s in provider.list_senders()] == [
        ("usr_3", "chloe@x.example"),
        ("usr_3", "me@x.example"),
    ]
    # The sender chosen in Nidria goes with the campaign when it changes...
    fake.calls.clear()
    provider.update_campaign(CampaignRef("cam_1"), CampaignSpec(name="A", sender_ids=("usr_1",)))
    [patch] = [body for method, _, _, body in fake.calls if method == "PATCH"]
    assert patch == {
        "name": "A",
        "stopOnEmailReplied": True,
        "disableTrackClick": False,
        "tracking": TRACKING,
        "sendUserIds": ["usr_1"],
    }
    # ...and not again once it sends: a mailbox picked in Lemlist for it stays.
    fake.calls.clear()
    fake.queue("GET", "/campaigns/cam_1", 200, {"senders": [{"id": "usr_1", "email": "c@x"}]})
    provider.update_campaign(CampaignRef("cam_1"), CampaignSpec(name="A", sender_ids=("usr_1",)))
    [patch] = [body for method, _, _, body in fake.calls if method == "PATCH"]
    assert patch == {
        "name": "A",
        "stopOnEmailReplied": True,
        "disableTrackClick": False,
        "tracking": TRACKING,
    }


def test_a_campaign_starts_only_from_the_mailbox_chosen_in_nidria(fake_lemlist: Double) -> None:
    """Lemlist's API assigns a user, which sends from its default mailbox
    (Prism, measured 2026-10-05): another of its mailboxes is picked in Lemlist."""
    fake, provider = fake_lemlist
    ref = CampaignRef("cam_1")
    # A new team: no campaign sender yet, its only user is assigned.
    fake.queue("GET", "/campaigns/cam_1", 200, {"senders": [], "sendUsers": []})
    fake.queue("GET", "/team/senders", 200, [])
    fake.queue("GET", "/team", 200, {"_id": "tea_1", "userIds": ["usr_1"]})
    default = {
        "senders": [{"id": "usr_1", "email": "ro@x.example", "sendUserMailboxId": "usm_a"}],
        "sendUsers": [{"id": "usr_1", "mailboxes": [{"email": "ro@x.example"}]}],
    }
    fake.queue("GET", "/campaigns/cam_1", 200, default)
    with pytest.raises(ProviderError) as wrong:
        provider.start_campaign(ref, mailbox="chloe@x.example")
    assert wrong.value.code == "SENDER_MAILBOX"
    assert wrong.value.params == {"mailbox": "chloe@x.example", "sending_from": ["ro@x.example"]}
    assert "ro@x.example" in str(wrong.value) and "Settings › Senders" in str(wrong.value)
    assert ("PATCH", "/campaigns/cam_1") in fake.routes()
    assert "/campaigns/cam_1/start" not in [p for _, p in fake.routes()]
    # Picked in Lemlist (here among the mailboxes it rotates): it starts.
    fake.calls.clear()
    rotated = {
        "senders": [{"id": "usr_1", "email": "ro@x.example"}],
        "sendUsers": [
            {"id": "usr_1", "mailboxes": [{"email": "ro@x.example"}, {"email": "Chloe@x.example"}]}
        ],
    }
    fake.queue("GET", "/campaigns/cam_1", 200, rotated)
    provider.start_campaign(ref, mailbox="chloe@x.example")
    assert [p for _, p in fake.routes()] == ["/campaigns/cam_1", "/campaigns/cam_1/start"]
    # No mailbox chosen: Lemlist's choice stands.
    fake.calls.clear()
    fake.queue("GET", "/campaigns/cam_1", 200, default)
    provider.start_campaign(ref)
    assert [p for _, p in fake.routes()] == ["/campaigns/cam_1", "/campaigns/cam_1/start"]


# ── transport: rate limit, retries, error codes ──────────────────────────


@pytest.mark.parametrize(
    ("retry_after", "expected_wait"),
    [("3", 3.0), ("30", 10.0), (None, 2.0), ("Wed, 07 Oct 2026 23:00:00 GMT", 2.0)],
)
def test_a_429_is_retried_after_retry_after_capped_at_ten_seconds(
    fake_lemlist: Double, retry_after: str | None, expected_wait: float
) -> None:
    fake, provider = fake_lemlist
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    fake.queue("POST", "/campaigns/cam_1/pause", 429, {"error": "slow down"}, headers=headers)
    provider.pause_campaign(CampaignRef("cam_1"))
    # A 429 was not processed: even a write is sent again, once.
    assert fake.routes() == [("POST", "/campaigns/cam_1/pause")] * 2
    assert fake.sleeps == [expected_wait]


def test_a_persistent_429_gives_up_after_three_retries(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    for _ in range(4):
        fake.queue("GET", "/campaigns/cam_1", 429, {"error": "slow down"})
    with pytest.raises(ProviderError) as limited:
        provider.read_campaign(CampaignRef("cam_1"))
    assert (limited.value.code, limited.value.status) == ("RATE_LIMITED", 429)
    assert len(fake.calls) == 4


def test_reads_are_retried_after_a_429_or_a_5xx_writes_are_not(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    fake.queue("GET", "/activities", 429, {"error": "slow down"})
    fake.queue("GET", "/activities", 500, "Internal Server Error")
    fake.queue(
        "GET", "/activities", 200, [{"_id": "a1", "type": "emailsOpened", "campaignId": "c"}]
    )
    [event] = provider.sync_campaign(CampaignRef("c"), None)
    assert event.type == "OPENED" and len(fake.calls) == 3

    fake.queue("GET", "/campaigns/c", 200, {"senders": [{"userId": "usr_1"}]})
    fake.queue("POST", "/campaigns/c/start", 500, "Internal Server Error")
    with pytest.raises(ProviderError) as failure:
        provider.start_campaign(CampaignRef("c"))
    assert (failure.value.code, failure.value.status) == ("PROVIDER_ERROR", 500)
    assert [p for _, p in fake.routes()].count("/campaigns/c/start") == 1

    fake.queue("POST", "/leads/review/lea_1", 403, {"message": "Upgrade your plan to emailPro"})
    with pytest.raises(ProviderError) as plan:
        provider.launch_contact(LeadRef("lea_1"))
    assert plan.value.code == "PLAN_REQUIRED"


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, "The authentication you supplied is incorrect", "UNAUTHORIZED"),
        (402, {"message": "Payment required"}, "PLAN_REQUIRED"),
        (403, {"message": "Upgrade your plan"}, "PLAN_REQUIRED"),
        (403, {"message": "Forbidden"}, "FORBIDDEN"),
        (404, {"error": "Not found"}, "NOT_FOUND"),
        (409, {"error": "Conflict"}, "CONFLICT"),
        (400, {"code": "LEAD_ALREADY_IN_OTHER_CAMPAIGN"}, "ALREADY_IN_OTHER_CAMPAIGN"),
        (400, {"code": "LEAD_ALREADY_IN_CAMPAIGN"}, "ALREADY_IN_CAMPAIGN"),
        (400, {"error": "This email is in the graveyard"}, "EMAIL_UNSUBSCRIBED"),
        (409, {"code": "campaign-reroute-in-progress"}, "RETRY_LATER"),
        (409, {"code": "remove-step-on-sequence-in-use"}, "STEP_IN_USE"),
        (409, {"code": "SEQUENCE_STEP_INSERT_ON_SEQUENCE_IN_USE"}, "INSERT_IN_USE"),
        (400, {"error": "Something else"}, "PROVIDER_ERROR"),
    ],
)
def test_lemlist_errors_are_mapped_to_stable_codes(
    fake_lemlist: Double, status: int, body: Any, code: str
) -> None:
    fake, provider = fake_lemlist
    fake.queue("POST", "/leads/pause/lea_1", status, body)
    with pytest.raises(ProviderError) as error:
        provider.pause_contact(CampaignRef("cam_1"), LeadRef("lea_1"))
    assert (error.value.code, error.value.status) == (code, status)
    # The key never appears in what Nidria keeps of an error.
    assert FAKE_LEMLIST_KEY not in str(error.value)


@pytest.mark.parametrize(("method", "expected_calls"), [("GET", 3), ("POST", 1)])
def test_timeout_retries_reads_but_never_replays_an_uncertain_write(
    monkeypatch: pytest.MonkeyPatch, method: str, expected_calls: int
) -> None:
    calls: list[str] = []

    def timeout(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        raise httpx.ReadTimeout("synthetic timeout: private-key-must-not-leak", request=request)

    monkeypatch.setattr("src.email_campaigns.providers.lemlist.time.sleep", lambda _seconds: None)
    adapter = LemlistEmailProvider("synthetic-key", transport=httpx.MockTransport(timeout))
    with pytest.raises(ProviderError) as failure:
        if method == "GET":
            adapter.read_campaign(CampaignRef("synthetic-campaign"))
        else:
            adapter.pause_campaign(CampaignRef("synthetic-campaign"))
    assert failure.value.code == "TIMEOUT"
    assert calls == [method] * expected_calls
    assert "private-key-must-not-leak" not in str(failure.value)


def test_an_unreachable_lemlist_is_said_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic refusal", request=request)

    monkeypatch.setattr("src.email_campaigns.providers.lemlist.time.sleep", lambda _seconds: None)
    adapter = LemlistEmailProvider("synthetic-key", transport=httpx.MockTransport(refused))
    with pytest.raises(ProviderError) as failure:
        adapter.pause_campaign(CampaignRef("c"))
    assert failure.value.code == "UNREACHABLE"


def test_the_throttle_holds_twenty_requests_per_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr("src.email_campaigns.providers.lemlist.time.sleep", waits.append)
    throttle = lemlist_module._Throttle()
    # Twenty calls take microseconds: all within one two-second window.
    for _ in range(20):
        throttle.wait()
    assert waits == []
    throttle.wait()  # the 21st waits for the window to slide
    assert len(waits) == 1 and 1.9 < waits[0] <= 2.05


# ── account: team, plan, webhook (outside the protocol) ──────────────────


def test_the_team_is_read_once_and_unknown_is_never_another_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, provider = lemlist_double(monkeypatch)
    fake.queue("GET", "/team", 200, {"_id": "tea_1", "name": "Nidria", "userIds": ["u2", "u1"]})
    team = provider.read_team()
    assert team is not None and (team.team_id, team.name, team.user_ids) == (
        "tea_1",
        "Nidria",
        ("u1", "u2"),
    )
    fake.queue("GET", "/team", 200, {"_id": "tea_1"})
    assert provider.team_id() == "tea_1"
    assert provider.team_id() == "tea_1"
    assert fake.routes().count(("GET", "/team")) == 2  # read_team, then once for team_id
    # Lemlist failing: unknown, raised (decision A4): never None ("no team"),
    # never another team, and not kept: the next call asks again.
    failing, adapter = lemlist_double(monkeypatch)
    failing.queue("GET", "/team", 401, "Unauthorized")
    with pytest.raises(TeamUnknownError) as unknown:
        adapter.team_id()
    assert (unknown.value.code, unknown.value.reason) == ("TEAM_UNKNOWN", "http_401")
    assert isinstance(unknown.value, ProviderError)
    failing.queue("GET", "/team", 200, {"name": "no id"})
    with pytest.raises(TeamUnknownError) as nameless:
        adapter.team_id()
    assert nameless.value.reason == "payload"
    failing.queue("GET", "/team", 200, {"_id": "tea_2"})
    assert adapter.team_id() == "tea_2"
    assert failing.routes().count(("GET", "/team")) == 3
    # An injected lookup (the 10-minute cache of the team module) is used instead.
    injected, cached = lemlist_double(monkeypatch, team_lookup=lambda: "tea_cached")
    assert cached.team_id() == "tea_cached" and injected.calls == []

    def lookup_fails() -> str:
        raise TeamUnknownError("network")

    _, outage = lemlist_double(monkeypatch, team_lookup=lookup_fails)
    with pytest.raises(TeamUnknownError):
        outage.team_id()


def test_the_plan_is_read_from_the_user_channels(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    fake.queue("GET", "/user/channels", 200, {"plan": "Email", "email": {"accounts": []}})
    assert provider.read_plan() == "Email"
    fake.queue("GET", "/user/channels", 200, {"email": {"accounts": []}})
    assert provider.read_plan() is None
    assert fake.routes() == [("GET", "/user/channels")] * 2


def test_the_webhook_is_created_once_for_every_event(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    url = "https://api.nidria.example/webhooks/lemlist"
    fake.queue(
        "GET",
        "/hooks",
        200,
        [{"_id": "hoo_other", "targetUrl": "https://elsewhere.example/hook", "type": "emailsSent"}],
    )
    fake.queue("POST", "/hooks", 200, {"_id": "hoo_1", "targetUrl": url})
    hook, created = provider.ensure_hook(url, "s3cret-value")
    assert (hook, created) == (LemlistHook("hoo_1", url, None), True)
    # No `type`: a hook has one type or none, and none receives every event.
    assert fake.calls[-1][:2] == ("POST", "/hooks")
    assert fake.calls[-1][3] == {"targetUrl": url, "secret": "s3cret-value"}
    # Idempotent: the hook already at this exact URL is returned, nothing created.
    fake.calls.clear()
    fake.queue("GET", "/hooks", 200, [{"_id": "hoo_1", "targetUrl": url}])
    assert provider.ensure_hook(url, "s3cret-value") == (LemlistHook("hoo_1", url, None), False)
    assert fake.routes() == [("GET", "/hooks")]
    # A refusal never carries the secret, even when Lemlist echoes it.
    fake.queue("POST", "/hooks", 400, {"error": "Invalid secret s3cret-value"})
    with pytest.raises(ProviderError) as refused:
        provider.create_hook(url, "s3cret-value")
    assert "s3cret-value" not in str(refused.value)
    assert refused.value.status == 400
    fake.queue("POST", "/hooks", 200, {"targetUrl": url})
    with pytest.raises(ProviderError) as no_id:
        provider.create_hook(url, "s3cret-value")
    assert no_id.value.code == "BAD_RESPONSE"


def test_a_webhook_is_deleted_by_its_id_and_recreated_for_its_url_only(
    fake_lemlist: Double,
) -> None:
    """`DELETE /hooks/{hookId}` (developer.lemlist.com, webhooks/delete-webhook):
    a 404 "Webhook not found" is a webhook gone already. Recreating deletes
    the webhook of exactly that URL, never another one, then creates it."""
    fake, provider = fake_lemlist
    url = "https://api.nidria.example/webhooks/lemlist"
    fake.queue("DELETE", "/hooks/hoo_gone", 404, "Webhook not found")
    provider.delete_hook("hoo_gone")
    fake.queue("DELETE", "/hooks/hoo_bad", 400, "Bad team")
    with pytest.raises(ProviderError):
        provider.delete_hook("hoo_bad")
    fake.calls.clear()
    fake.queue(
        "GET",
        "/hooks",
        200,
        [
            {"_id": "hoo_other", "targetUrl": "https://elsewhere.example/hook"},
            {"_id": "hoo_1", "targetUrl": url},
        ],
    )
    fake.queue("POST", "/hooks", 200, {"_id": "hoo_2", "targetUrl": url})
    assert provider.recreate_hook(url, "n3w-secret") == (LemlistHook("hoo_2", url, None), True)
    assert fake.routes() == [("GET", "/hooks"), ("DELETE", "/hooks/hoo_1"), ("POST", "/hooks")]
    assert fake.calls[-1][3] == {"targetUrl": url, "secret": "n3w-secret"}
    fake.calls.clear()
    fake.queue("GET", "/hooks", 200, [])
    fake.queue("POST", "/hooks", 200, {"_id": "hoo_3", "targetUrl": url})
    assert provider.recreate_hook(url, "n3w-secret") == (LemlistHook("hoo_3", url, None), False)
    assert fake.routes() == [("GET", "/hooks"), ("POST", "/hooks")]


# ── unsubscribes since 30/03/2026 ────────────────────────────────────────


def _activity(event_id: str, kind: str, **fields: Any) -> dict[str, Any]:
    """An activity as Lemlist documents it (webhooks/add-webhook, common
    fields): `campaignId`, `leadId` and `leadEmail` only on a campaign event,
    `contactId` on every one."""
    return {
        "_id": event_id,
        "type": kind,
        "teamId": "tea_1",
        "createdAt": "2026-10-08T09:00:00.000Z",
        **fields,
    }


def test_a_contacts_unsubscribe_is_an_unsubscribe_and_a_variables_is_its_own(
    fake_lemlist: Double,
) -> None:
    """Since 30/03/2026 a contact unsubscribing emits `entityUnsubscribed`
    (whole contact) or `variableUnsubscribed` (one variable) instead of
    `emailsUnsubscribed`, which "also matches `apiUnsubscribed` and
    `manualUnsubscribed`". A variable's carries no documented field saying
    which: VARIABLE_UNSUBSCRIBED (kept, never applied). A re-subscription is
    not tracked: nothing ever lifts a block."""
    fake, provider = fake_lemlist
    fake.queue(
        "GET",
        "/activities",
        200,
        [
            _activity(
                "act_1",
                "entityUnsubscribed",
                campaignId="cam_1",
                leadId="lea_1",
                leadEmail="ana@example.com",
                contactId="ctc_1",
            ),
            _activity("act_2", "entityUnsubscribed", contactId="ctc_2", email="ben@example.com"),
            _activity("act_3", "variableUnsubscribed", contactId="ctc_3"),
            _activity("act_4", "apiUnsubscribed", campaignId="cam_1", leadId="lea_4"),
            _activity("act_5", "manualUnsubscribed", campaignId="cam_1", leadId="lea_5"),
            _activity("act_6", "entitySubscribed", contactId="ctc_1"),
            _activity("act_7", "variableSubscribed", contactId="ctc_3"),
        ],
    )
    events = provider.sync_campaign(CampaignRef("cam_1"), None)
    assert [(e.event_id, e.type, e.provider_type) for e in events] == [
        ("act_1", "UNSUBSCRIBED", "entityUnsubscribed"),
        ("act_2", "UNSUBSCRIBED", "entityUnsubscribed"),
        ("act_3", "VARIABLE_UNSUBSCRIBED", "variableUnsubscribed"),
        ("act_4", "UNSUBSCRIBED", "apiUnsubscribed"),
        ("act_5", "UNSUBSCRIBED", "manualUnsubscribed"),
    ]
    contact = events[1]
    assert (contact.campaign_id, contact.lead_id, contact.email) == (
        None,
        None,
        "ben@example.com",
    )
    assert contact.payload["contactId"] == "ctc_2" and contact.payload["teamId"] == "tea_1"
    assert events[0].email == "ana@example.com"
    contact_types = {"entityUnsubscribed", "variableUnsubscribed"}
    assert contact_types == lemlist_module.CONTACT_ACTIVITY_TYPES


def test_a_lead_whose_state_says_it_unsubscribed_is_read_unsubscribed(
    fake_lemlist: Double,
) -> None:
    """The listing's `state` is the lead's last activity type: seen live on
    08/10/2026, `entityUnsubscribed`; `status` "unsubscribed" is documented
    too. A variable's unsubscribe is not the lead's."""
    fake, provider = fake_lemlist
    fake.queue(
        "GET",
        "/campaigns/cam_1/leads",
        200,
        [
            {"_id": "l1", "contactId": "c1", "state": "entityUnsubscribed"},
            {"_id": "l2", "contactId": "c2", "state": "emailsUnsubscribed"},
            {"_id": "l3", "contactId": "c3", "state": "variableUnsubscribed"},
            {"_id": "l4", "contactId": "c4", "state": "emailsSent"},
            {"_id": "l5", "contactId": "c5", "state": "reviewed", "status": "unsubscribed"},
            {"_id": "l6", "contactId": "c6", "isUnsubscribed": True},
        ],
    )
    leads = provider.list_contacts(CampaignRef("cam_1")).leads
    assert {lead.lead_id: lead.unsubscribed for lead in leads} == {
        "l1": True,
        "l2": True,
        "l3": False,
        "l4": False,
        "l5": True,
        "l6": True,
    }


# ── dry run ──────────────────────────────────────────────────────────────


def test_the_dry_run_provider_records_and_sends_nothing() -> None:
    provider = DryRunEmailProvider()
    ref = provider.create_campaign(CampaignSpec(name="Test"))
    lead = provider.add_contact(ref, LeadSpec(email="marie@example.com"))
    assert all(is_dry_run_id(i) for i in (ref.campaign_id, ref.sequence_id, lead.lead_id))
    assert not is_dry_run_id("cam_real") and not is_dry_run_id(None)
    assert provider.external is False
    assert provider.team_id() is None and provider.list_senders() == []
    provider.pause_contact(ref, lead)
    paused = provider.get_contact(lead)
    assert paused is not None and paused.is_paused is True
    provider.resume_contact(ref, lead)
    resumed = provider.get_contact(lead)
    assert resumed is not None and resumed.is_paused is False
    assert [op for op, _ in provider.operations] == [
        "create_campaign",
        "add_contact",
        "pause_contact",
        "resume_contact",
    ]
