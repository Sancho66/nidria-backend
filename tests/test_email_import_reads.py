"""What the import of the running Lemlist campaigns reads (lot C), and how it
maps Lemlist's words on a lead to a Nidria status.

The adapter's reads run against `FakeLemlist` (httpx.MockTransport: no
socket, the network guard counts nothing). Each route is the one the notes
source (notes/lemlist-import-research.txt): the team's campaigns (§6), one
campaign (§4), its sequences (§5), its leads (§1.1), its export (§1.2 a,
under the API base), the contacts lookup (§1.2 d), its activities (§3); all
GET. `resume_campaign` is the one write an imported campaign's resume makes.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from src.email_campaigns.email_import import RULES, propose
from src.email_campaigns.providers.base import CampaignRef, ImportReader, ProviderError
from src.email_campaigns.providers.dry_run import DryRunEmailProvider
from src.email_campaigns.providers.lemlist import LemlistEmailProvider
from tests.email_fakes import FakeLemlist, FakeProvider, lemlist_double

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
    ("state", "status", "rule", "proposed"),
    [
        # Blocks and replies: the state is a fact, whatever the status.
        ("entityUnsubscribed", "running", "unsubscribed", "EMAIL_UNSUBSCRIBED"),
        ("emailsUnsubscribed", None, "unsubscribed", "EMAIL_UNSUBSCRIBED"),
        ("emailsSent", "unsubscribed", "unsubscribed", "EMAIL_UNSUBSCRIBED"),
        ("emailsBounced", "running", "bounced", "EMAIL_BOUNCED"),
        ("emailsBounced", "done", "bounced", "EMAIL_BOUNCED"),
        ("emailsReplied", "done", "replied", "EMAIL_REPLIED"),
        ("emailsReplied", "interested", "replied", "EMAIL_REPLIED"),
        # Not launched at Lemlist, or skipped there: not imported.
        ("skipped", "running", "skipped", None),
        ("scanned", "review", "not_launched", None),
        ("reviewed", "scanning", "not_launched", None),
        # The status, then the state for a running lead.
        ("emailsSent", "done", "completed", "EMAIL_COMPLETED"),
        ("emailsSent", "paused", "paused", "EMAIL_PAUSED"),
        ("paused", "running", "paused", "EMAIL_PAUSED"),
        ("reviewed", "running", "launched", "EMAIL_QUEUED"),
        ("scanned", "running", "launched", "EMAIL_QUEUED"),
        ("emailsSent", "running", "in_sequence", "EMAIL_ACTIVE"),
        ("emailsOpened", "running", "in_sequence", "EMAIL_ACTIVE"),
        ("emailsClicked", "running", "in_sequence", "EMAIL_ACTIVE"),
        # Not covered: not imported, counted with the raw values.
        ("emailsSent", "interested", "uncovered", None),
        ("emailsInterested", "notInterested", "uncovered", None),
        ("variableUnsubscribed", "running", "uncovered", None),
        ("linkedinVisitDone", "running", "uncovered", None),
        (None, "running", "uncovered", None),
        ("emailsSent", None, "status_unknown", None),
    ],
)
def test_lemlists_words_on_a_lead_map_to_one_status(
    state: str | None, status: str | None, rule: str, proposed: str | None
) -> None:
    decided = propose(state, status)
    assert (decided.rule, decided.proposed) == (rule, proposed)
    if proposed is None:
        assert decided.not_imported is not None
    expected = {"EMAIL_UNSUBSCRIBED": "UNSUBSCRIBED", "EMAIL_BOUNCED": "BOUNCED"}.get(
        proposed or ""
    )
    assert decided.opposition == expected


def test_the_mapping_is_ordered_and_names_each_rule_once() -> None:
    names = [rule.rule for rule in RULES]
    assert len(names) == len(set(names))
    assert names[:3] == ["unsubscribed", "bounced", "replied"]
    assert all(rule.proposed or rule.not_imported for rule in RULES)


def test_the_import_order_is_lemlists_creation_oldest_first() -> None:
    """Sanity of the import's order: Lemlist's `createdAt`, oldest first, an
    unreadable one last."""
    from src.email_campaigns.email_import import _created
    from src.email_campaigns.providers.base import ProviderCampaignRow

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
