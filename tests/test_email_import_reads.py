"""What the import of the running Lemlist campaigns reads (lot C), and how it
maps Lemlist's words on a lead to a Nidria status.

The adapter's reads run against `FakeLemlist` (httpx.MockTransport: no
socket, the network guard counts nothing). Each route is the one the notes
source (notes/lemlist-import-research.txt): the team's campaigns (§6), one
campaign (§4), its sequences (§5), its leads (§1.1), its export (§1.2 a,
under the API base; its rows as the real team's answered, §17), the
contacts lookup (§1.2 d), its activities (§3); all GET. `resume_campaign`
is the one write an imported campaign's resume makes.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from src.email_campaigns.email_import import (
    RULES,
    STATE_SYSTEM_STATUS,
    propose,
    read_campaign,
    read_status,
)
from src.email_campaigns.providers.base import (
    CampaignRef,
    ImportReader,
    ProviderCampaignRow,
    ProviderError,
)
from src.email_campaigns.providers.dry_run import DryRunEmailProvider
from src.email_campaigns.providers.lemlist import LemlistEmailProvider
from tests.email_fakes import FakeLemlist, FakeProvider, lemlist_double, observed_export_row

Double = tuple[FakeLemlist, LemlistEmailProvider]


@pytest.fixture
def fake_lemlist(monkeypatch: pytest.MonkeyPatch) -> Double:
    return lemlist_double(monkeypatch)


def _campaign_row(campaign_id: str, status: str = "paused", **values: Any) -> dict[str, Any]:
    return {
        "_id": campaign_id,
        "name": f"Campaign {campaign_id}",
        "status": status,
        "createdAt": "2026-08-01T09:00:00.000Z",
        **values,
    }


def _only_gets(fake: FakeLemlist) -> None:
    assert {method for method, _path, _query, _body in fake.calls} == {"GET"}


# ── the contract ─────────────────────────────────────────────────────────────


def test_lemlist_reads_for_an_import_and_the_dry_run_does_not() -> None:
    assert isinstance(LemlistEmailProvider("k"), ImportReader)
    assert isinstance(FakeProvider(), ImportReader)
    # The dry run has nothing to import: an import needs the real provider.
    assert not isinstance(DryRunEmailProvider(), ImportReader)


# ── the team's campaigns: GET /campaigns?version=v2 ──────────────────────────


def test_the_campaigns_are_listed_page_by_page_with_version_two(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    first = [_campaign_row(f"cam_{i}") for i in range(100)]
    fake.queue("GET", "/campaigns", 200, first)
    fake.queue(
        "GET",
        "/campaigns",
        200,
        [_campaign_row("cam_99"), _campaign_row("cam_100", "draft", createdAt=None)],
    )
    listing = provider.list_campaigns()
    assert listing.complete is True and listing.pages == 2
    # A campaign listed twice (a page shifting meanwhile) is kept once.
    assert len(listing.campaigns) == 101
    last = listing.campaigns[-1]
    assert (last.campaign_id, last.status, last.created_at) == ("cam_100", "draft", None)
    assert listing.campaigns[0].created_at == "2026-08-01T09:00:00.000Z"
    assert [q for _m, _p, q, _b in fake.calls] == [
        {"version": "v2", "limit": "100", "offset": "0"},
        {"version": "v2", "limit": "100", "offset": "100"},
    ]
    _only_gets(fake)


def test_a_campaigns_answer_wrapped_in_an_object_is_read_too(fake_lemlist: Double) -> None:
    """The v2 shape was never observed: an array is documented, an object
    `{campaigns: [...]}` is read defensively."""
    fake, provider = fake_lemlist
    fake.queue("GET", "/campaigns", 200, {"campaigns": [_campaign_row("cam_1", "running")]})
    listing = provider.list_campaigns()
    assert [(c.campaign_id, c.status) for c in listing.campaigns] == [("cam_1", "running")]
    fake.queue("GET", "/campaigns", 200, "")
    assert provider.list_campaigns().campaigns == []
    fake.queue("GET", "/campaigns", 200, "<html>not json</html>")
    with pytest.raises(ProviderError) as refused:
        provider.list_campaigns()
    assert refused.value.code == "BAD_RESPONSE"


# ── one campaign and its sequences ───────────────────────────────────────────


def test_a_campaign_names_its_main_sequence(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    fake.queue(
        "GET",
        "/campaigns/cam_1",
        200,
        {"_id": "cam_1", "status": "paused", "sequenceId": "seq_main", "name": "Avocats"},
    )
    campaign = provider.read_campaign(CampaignRef("cam_1"))
    assert (campaign.exists, campaign.sequence_id, campaign.status) == (True, "seq_main", "paused")
    assert campaign.metadata["name"] == "Avocats"
    fake.queue("GET", "/campaigns/cam_gone", 200, {})
    assert provider.read_campaign(CampaignRef("cam_gone")).exists is False


def test_only_the_main_sequence_is_read_and_branches_and_ab_tests_are_counted(
    fake_lemlist: Double,
) -> None:
    fake, provider = fake_lemlist
    fake.queue(
        "GET",
        "/campaigns/cam_1/sequences",
        200,
        {
            "seq_branch": {
                "_id": "seq_branch",
                "level": 1,
                "parentId": "seq_main",
                "steps": [{"_id": "stp_b1", "type": "email"}, {"_id": "stp_b2", "type": "email"}],
            },
            "seq_main": {
                "_id": "seq_main",
                "level": 0,
                "steps": [
                    {
                        "_id": "stp_2",
                        "type": "linkedinSend",
                        "delay": 3,
                        "index": 2,
                        "sequenceStep": 1,
                        "message": "Hello",
                    },
                    {
                        "_id": "stp_1",
                        "type": "email",
                        "delay": 0,
                        "index": 1,
                        "sequenceStep": 0,
                        "subject": "{{firstName}}, une question",
                        "message": "<p>Bonjour {{firstName}}</p>",
                        "abTest": "A",
                    },
                    {
                        "_id": "stp_3",
                        "type": "email",
                        "delay": "7",
                        "sequenceStep": 2,
                        "abTest": True,
                    },
                    {"type": "email"},
                ],
            },
        },
    )
    sequences = provider.read_sequences(CampaignRef("cam_1", "seq_main"))
    assert sequences.main_sequence_id == "seq_main"
    assert [(s.step_id, s.position, s.type, s.delay_days, s.ab_test) for s in sequences.steps] == [
        ("stp_1", 1, "email", 0, True),
        ("stp_2", 2, "linkedinSend", 3, False),
        ("stp_3", 3, "email", 7, True),
    ]
    # The raw step is kept as Lemlist returned it (variant A's text).
    assert sequences.steps[0].raw["subject"] == "{{firstName}}, une question"
    assert (sequences.branches, sequences.branch_steps) == (1, 2)
    # Without the campaign's sequence id, the main one is the level 0.
    fake.queue(
        "GET",
        "/campaigns/cam_1/sequences",
        200,
        {"seq_x": {"level": 0, "steps": [{"_id": "stp_x", "type": "email"}]}},
    )
    assert provider.read_sequences(CampaignRef("cam_1")).main_sequence_id == "seq_x"
    _only_gets(fake)


# ── the leads' identities: the export, then the contacts lookup ──────────────


def test_the_export_is_read_under_the_api_base_in_json(fake_lemlist: Double) -> None:
    """[D-export-v2]: https://api.lemlist.com/api/v2/campaigns/{id}/export/leads,
    `state` and `format` only, no pagination documented."""
    fake, provider = fake_lemlist
    fake.queue(
        "GET",
        "/v2/campaigns/cam_1/export/leads",
        200,
        [
            {
                "_id": "lea_1",
                "email": " Ana@Example.com ",
                "firstName": "Ana",
                "lastName": "Lopez",
                "companyName": "Acme",
                "status": "running",
                "lastState": "emailsSent",
                "emailStatus": "deliverable",
            },
            {"_id": "lea_1", "email": "twice@example.com"},
            {"_id": "lea_2", "status": "done"},
        ],
    )
    rows = provider.export_leads(CampaignRef("cam_1"))
    assert fake.urls == [
        "https://api.lemlist.com/api/v2/campaigns/cam_1/export/leads?state=all&format=json"
    ]
    assert [(r.lead_id, r.email, r.status, r.last_state, r.company_name) for r in rows] == [
        ("lea_1", "Ana@Example.com", "running", "emailsSent", "Acme"),
        ("lea_2", None, "done", None, None),
    ]
    _only_gets(fake)


def test_the_real_export_says_state_and_state_system_and_no_status(fake_lemlist: Double) -> None:
    """The export as the real team's answered on 08/10/2026 (notes §17), not
    as documented: an array in one answer, `state` and `stateSystem`, the
    campaign's own variables, the names only when the campaign has them,
    never `status` nor `lastState`. Every word is optional; no variable is
    kept."""
    fake, provider = fake_lemlist
    fake.queue(
        "GET",
        "/v2/campaigns/cam_1/export/leads",
        200,
        [
            observed_export_row(
                "lea_1",
                "ana@acme.io",
                "emailsSent",
                "inProgress",
                company="Acme",
                objet="Votre cabinet",
                mail1="Bonjour Ana",
                relance1="Je me permets",
                objet_r2="Suite",
                cle="k1",
                nom="Lopez",
                parcours="avocats",
            ),
            observed_export_row(
                "lea_2", "ben@acme.io", "reviewed", "reviewed", firstName="Ben", lastName="Ready"
            ),
            {"_id": "lea_3", "email": "cleo@acme.io"},
        ],
    )
    rows = provider.export_leads(CampaignRef("cam_1"))
    assert [
        (r.lead_id, r.email, r.first_name, r.last_name, r.company_name)
        + (r.status, r.last_state, r.state_system)
        for r in rows
    ] == [
        ("lea_1", "ana@acme.io", None, None, "Acme", None, "emailsSent", "inProgress"),
        ("lea_2", "ben@acme.io", "Ben", "Ready", None, None, "reviewed", "reviewed"),
        ("lea_3", "cleo@acme.io", None, None, None, None, None, None),
    ]
    assert fake.urls == [
        "https://api.lemlist.com/api/v2/campaigns/cam_1/export/leads?state=all&format=json"
    ]
    _only_gets(fake)


@pytest.mark.parametrize("key", ["leads", "data"])
def test_an_export_wrapped_in_an_object_is_read_too(fake_lemlist: Double, key: str) -> None:
    fake, provider = fake_lemlist
    fake.queue(
        "GET",
        "/v2/campaigns/cam_1/export/leads",
        200,
        {key: [observed_export_row("lea_1", "ana@acme.io", "emailsSent", "inProgress")]},
    )
    [row] = provider.export_leads(CampaignRef("cam_1"))
    assert (row.lead_id, row.status, row.last_state, row.state_system) == (
        "lea_1",
        None,
        "emailsSent",
        "inProgress",
    )


def test_contacts_are_looked_up_by_a_hundred_ids_at_most(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    ids = [f"ctc_{i}" for i in range(150)]
    fake.queue(
        "GET",
        "/contacts",
        200,
        [
            {
                "_id": "ctc_0",
                "email": "kim@example.com",
                "fields": {"firstName": "Kim", "lastName": "Park"},
                "campaigns": [
                    {"campaignId": "cam_1", "leadId": "lea_9", "leadState": "running"},
                    {"campaignId": "cam_2", "leadId": "lea_x"},
                ],
            }
        ],
    )
    fake.queue("GET", "/contacts", 200, [])
    contacts = provider.lookup_contacts([*ids, "ctc_0", ""])
    assert [len(q["idsOrEmails"].split(",")) for _m, _p, q, _b in fake.calls] == [100, 50]
    [kim] = contacts
    assert (kim.email, kim.first_name, kim.last_name) == ("kim@example.com", "Kim", "Park")
    assert kim.lead_statuses == {("cam_1", "lea_9"): "running"}
    _only_gets(fake)


# ── a campaign's leads: the export's, and the ones only the listing gives ────


def test_a_campaigns_leads_are_the_exports_when_the_listing_loses_some(
    fake_lemlist: Double,
) -> None:
    """Measured on the real team: the listing's pages shift (offset, newest
    first), a lead read twice taking another's place, while the export
    gives every lead in one answer. The export is the reference; the
    listing gives what it alone has (contact id, state); the contact id of
    a lead it missed is looked up by address (`GET /contacts?idsOrEmails=`),
    and one not found leaves the lead without it, imported all the same."""
    fake, provider = fake_lemlist
    ids = [f"lea_{i:03}" for i in range(103)]
    for path, body in (
        ("/campaigns/cam_u", _campaign_row("cam_u", sequenceId="seq_u")),
        ("/campaigns/cam_u/sequences", {"seq_u": {"_id": "seq_u", "level": 0, "steps": []}}),
        (
            "/v2/campaigns/cam_u/export/leads",
            [
                observed_export_row(i, f"{i}@unstable.example", "emailsSent", "inProgress")
                for i in ids
            ],
        ),
    ):
        fake.answer("GET", path, 200, body)
    listed = [{"_id": i, "contactId": f"ctc_{i}", "state": "emailsSent"} for i in ids[:100]]
    # The second page repeats the first one's last two: three leads lost.
    fake.queue("GET", "/campaigns/cam_u/leads", 200, listed)
    fake.queue("GET", "/campaigns/cam_u/leads", 200, listed[98:])
    fake.queue(
        "GET",
        "/contacts",
        200,
        [
            # Names the lead in this campaign.
            {
                "_id": "ctc_lea_100",
                "email": "lea_100@unstable.example",
                "campaigns": [{"campaignId": "cam_u", "leadId": "lea_100", "leadState": "running"}],
            },
            # Its address only.
            {"_id": "ctc_lea_101", "email": "LEA_101@unstable.example"},
        ],
    )
    read = read_campaign(provider, ProviderCampaignRow("cam_u", "U", "paused", None))
    assert read.error is None
    assert (read.listed, read.listing_duplicates, read.exported) == (100, 2, 103)
    assert (read.not_listed, read.contact_ids_found) == (3, 2)
    leads = {lead.lead_id: lead for lead in read.leads}
    assert sorted(leads) == ids
    assert [
        (leads[i].listed, leads[i].contact_id, leads[i].state, leads[i].email)
        for i in ("lea_000", "lea_100", "lea_101", "lea_102")
    ] == [
        (True, "ctc_lea_000", "emailsSent", "lea_000@unstable.example"),
        (False, "ctc_lea_100", "emailsSent", "lea_100@unstable.example"),
        (False, "ctc_lea_101", "emailsSent", "lea_101@unstable.example"),
        (False, None, "emailsSent", "lea_102@unstable.example"),
    ]
    # The missed leads' addresses, in one lookup; every call a GET.
    [lookup] = [query for _m, path, query, _b in fake.calls if path == "/contacts"]
    assert lookup["idsOrEmails"].split(",") == [
        "lea_100@unstable.example",
        "lea_101@unstable.example",
        "lea_102@unstable.example",
    ]
    # Campaign, sequences, two listing pages, the export, the lookup.
    assert read.reads == len(fake.calls) == 6
    _only_gets(fake)


# ── the history: GET /activities?version=v2&campaignId= ──────────────────────


def test_a_campaigns_whole_history_is_read_page_by_page(fake_lemlist: Double) -> None:
    fake, provider = fake_lemlist
    page = [
        {"_id": f"act_{i}", "type": "emailsSent", "campaignId": "cam_1", "sequenceStep": 0}
        for i in range(99)
    ] + [{"_id": "act_li", "type": "linkedinVisitDone", "campaignId": "cam_1"}]
    fake.queue("GET", "/activities", 200, page)
    fake.queue(
        "GET",
        "/activities",
        200,
        [
            {"_id": "act_0", "type": "emailsSent", "campaignId": "cam_1"},
            {"_id": "act_r", "type": "emailsReplied", "campaignId": "cam_1"},
        ],
    )
    history = provider.read_activities(CampaignRef("cam_1"))
    # Every activity read is counted; only the tracked email events are kept,
    # each once (a page shifting meanwhile).
    assert (history.rows, history.pages, history.complete) == (102, 2, True)
    assert len(history.events) == 100 and history.events[-1].event_id == "act_r"
    assert [q for _m, _p, q, _b in fake.calls] == [
        {"campaignId": "cam_1", "version": "v2", "limit": "100", "offset": "0"},
        {"campaignId": "cam_1", "version": "v2", "limit": "100", "offset": "100"},
    ]
    _only_gets(fake)


# ── the one write of an imported campaign's resume ───────────────────────────


def test_resuming_an_imported_campaign_only_starts_it(fake_lemlist: Double) -> None:
    """No sender assigned, no mailbox checked: the start alone (unlike
    `start_campaign`, which reads the campaign and may PATCH its senders)."""
    fake, provider = fake_lemlist
    provider.resume_campaign(CampaignRef("cam_1"))
    fake.queue(
        "POST",
        "/campaigns/cam_1/start",
        400,
        {"error": "You can't start campaigns already running"},
    )
    provider.resume_campaign(CampaignRef("cam_1"))
    assert fake.routes() == [("POST", "/campaigns/cam_1/start")] * 2
    fake.queue("POST", "/campaigns/cam_1/start", 400, {"error": "Bad team"})
    with pytest.raises(ProviderError):
        provider.resume_campaign(CampaignRef("cam_1"))


# ── the mapping ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "status", "state_system", "rule", "proposed"),
    [
        # Blocks and replies: the state is a fact, whatever the status.
        ("entityUnsubscribed", "running", None, "unsubscribed", "EMAIL_UNSUBSCRIBED"),
        ("emailsUnsubscribed", None, None, "unsubscribed", "EMAIL_UNSUBSCRIBED"),
        ("emailsSent", "unsubscribed", None, "unsubscribed", "EMAIL_UNSUBSCRIBED"),
        ("emailsBounced", "running", None, "bounced", "EMAIL_BOUNCED"),
        ("emailsBounced", "done", None, "bounced", "EMAIL_BOUNCED"),
        ("emailsReplied", "done", None, "replied", "EMAIL_REPLIED"),
        ("emailsReplied", "interested", None, "replied", "EMAIL_REPLIED"),
        # Not launched at Lemlist, or skipped there: not imported.
        ("skipped", "running", None, "skipped", None),
        ("scanned", "review", None, "not_launched", None),
        ("reviewed", "scanning", None, "not_launched", None),
        # The status, then the state for a running lead.
        ("emailsSent", "done", None, "completed", "EMAIL_COMPLETED"),
        ("emailsSent", "paused", None, "paused", "EMAIL_PAUSED"),
        ("paused", "running", None, "paused", "EMAIL_PAUSED"),
        ("reviewed", "running", None, "launched", "EMAIL_QUEUED"),
        ("scanned", "running", None, "launched", "EMAIL_QUEUED"),
        ("emailsSent", "running", None, "in_sequence", "EMAIL_ACTIVE"),
        ("emailsOpened", "running", None, "in_sequence", "EMAIL_ACTIVE"),
        ("emailsClicked", "running", None, "in_sequence", "EMAIL_ACTIVE"),
        # The real export (notes §17): no status, its `stateSystem` read as
        # the status it stands for, with the states it came with there.
        ("emailsSent", None, "inProgress", "in_sequence", "EMAIL_ACTIVE"),
        ("emailsClicked", None, "inProgress", "in_sequence", "EMAIL_ACTIVE"),
        ("reviewed", None, "reviewed", "launched", "EMAIL_QUEUED"),
        ("emailsReplied", None, "paused", "replied", "EMAIL_REPLIED"),
        ("emailsSent", None, "paused", "paused", "EMAIL_PAUSED"),
        ("emailsBounced", None, "done", "bounced", "EMAIL_BOUNCED"),
        ("emailsSent", None, "done", "completed", "EMAIL_COMPLETED"),
        ("entityUnsubscribed", None, "unsubscribed", "unsubscribed", "EMAIL_UNSUBSCRIBED"),
        # A status, when given, wins over the `stateSystem`.
        ("emailsSent", "done", "inProgress", "completed", "EMAIL_COMPLETED"),
        # A `stateSystem` value not known: not covered (a block stays one).
        ("emailsSent", None, "onHold", "uncovered", None),
        ("reviewed", None, "InProgress", "uncovered", None),
        ("emailsBounced", None, "onHold", "bounced", "EMAIL_BOUNCED"),
        # Not covered: not imported, counted with the raw values.
        ("emailsSent", "interested", None, "uncovered", None),
        ("emailsInterested", "notInterested", None, "uncovered", None),
        ("variableUnsubscribed", "running", None, "uncovered", None),
        ("linkedinVisitDone", "running", None, "uncovered", None),
        (None, "running", None, "uncovered", None),
        # No word on its progress at all.
        ("emailsSent", None, None, "status_unknown", None),
    ],
)
def test_lemlists_words_on_a_lead_map_to_one_status(
    state: str | None,
    status: str | None,
    state_system: str | None,
    rule: str,
    proposed: str | None,
) -> None:
    decided = propose(state, status, state_system)
    assert (decided.rule, decided.proposed) == (rule, proposed)
    if proposed is None:
        assert decided.not_imported is not None
    expected = {"EMAIL_UNSUBSCRIBED": "UNSUBSCRIBED", "EMAIL_BOUNCED": "BOUNCED"}.get(
        proposed or ""
    )
    assert decided.opposition == expected


def test_the_state_system_values_are_read_as_documented_statuses() -> None:
    """The five values the real team's export showed on 08/10/2026 (notes
    §17), each read as the documented status it stands for; no other."""
    assert STATE_SYSTEM_STATUS == {
        "inProgress": "running",
        "reviewed": "running",
        "paused": "paused",
        "done": "done",
        "unsubscribed": "unsubscribed",
    }
    assert read_status(None, "inProgress") == "running"
    assert read_status("done", "inProgress") == "done"
    assert read_status(None, "onHold") is None
    assert read_status(None, None) is None


def test_the_mapping_is_ordered_and_names_each_rule_once() -> None:
    names = [rule.rule for rule in RULES]
    assert len(names) == len(set(names))
    assert names[:3] == ["unsubscribed", "bounced", "replied"]
    assert all(rule.proposed or rule.not_imported for rule in RULES)


def test_the_import_order_is_lemlists_creation_oldest_first() -> None:
    """Sanity of the import's order: Lemlist's `createdAt`, oldest first, an
    unreadable one last."""
    from src.email_campaigns.email_import import _created

    rows = [
        ProviderCampaignRow("cam_c", "C", "paused", None),
        ProviderCampaignRow("cam_b", "B", "paused", "2026-09-01T00:00:00Z"),
        ProviderCampaignRow("cam_a", "A", "paused", "2026-08-01T00:00:00.000Z"),
        ProviderCampaignRow("cam_x", "X", "paused", "not a date"),
    ]
    assert [r.campaign_id for r in sorted(rows, key=_created)] == [
        "cam_a",
        "cam_b",
        "cam_c",
        "cam_x",
    ]
    assert _created(rows[2])[1] == datetime(2026, 8, 1, tzinfo=UTC)
