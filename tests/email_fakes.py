"""Test doubles of the Email module, shared by its test files.

`FakeProvider` stands for the provider itself (the manager's tests): see its
docstring. `FakeLemlist` answers like Lemlist's documented routes through an
`httpx.MockTransport` and records every call: no socket is ever opened, so the
network guard counts nothing. Ported from Prism's `tests/test_email_rendering.py`
(`FakeLemlist` L316-336, fixture L339-348), plus what Nidria's tests check on
top: the Authorization header of each call and the waits asked of `time.sleep`.
Where the real team answered otherwise than documented, `observed_*` builds
the answer as observed (the export's rows, notes §17).

Use: `fake, provider = lemlist_double(monkeypatch)`, or the `fake_lemlist`
fixture once this module is listed in a `pytest_plugins`.
"""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx
import pytest

from src.email_campaigns.providers import lemlist as lemlist_module
from src.email_campaigns.providers.base import (
    CampaignRef,
    CampaignSpec,
    LeadRef,
    LeadSpec,
    ProviderActivityRead,
    ProviderCampaign,
    ProviderCampaignListing,
    ProviderCampaignRow,
    ProviderContact,
    ProviderError,
    ProviderEvent,
    ProviderLead,
    ProviderLeadIdentity,
    ProviderLeadListing,
    ProviderMessage,
    ProviderSender,
    ProviderSequences,
    ProviderStep,
    StepContent,
    StepSpec,
)
from src.email_campaigns.providers.lemlist import LemlistEmailProvider

#: (method, path without the `/api` prefix, query parameters, JSON body or None)
Call = tuple[str, str, dict[str, str], Any]

FAKE_LEMLIST_KEY = "test-key"


class FakeLemlist:
    """Answers like Lemlist's documented routes and records every call."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        #: The full URL of each call (base and query included), in call order.
        self.urls: list[str] = []
        #: The Authorization header of each call, in call order.
        self.authorizations: list[str | None] = []
        #: Every wait asked of `time.sleep` (retries, Retry-After, throttle).
        self.sleeps: list[float] = []
        self.responses: dict[tuple[str, str], list[httpx.Response]] = {}
        #: Answers given to every call of a route once its queue is empty.
        self.standing: dict[tuple[str, str], tuple[int, Any]] = {}

    def answer(self, method: str, path: str, status: int, body: Any) -> None:
        """What `method path` answers each time nothing is queued for it."""
        self.standing[(method, path)] = (status, body)

    def queue(
        self,
        method: str,
        path: str,
        status: int,
        body: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        """The next answer to `method path`; unqueued calls get `200 {"ok": true}`."""
        content = body if isinstance(body, str) else json.dumps(body)
        self.responses.setdefault((method, path), []).append(
            httpx.Response(status, content=content.encode(), headers=headers)
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, dict(request.url.params), body))
        self.urls.append(str(request.url))
        self.authorizations.append(request.headers.get("authorization"))
        queued = self.responses.get((request.method, path))
        if queued:
            return queued.pop(0)
        standing = self.standing.get((request.method, path))
        if standing is not None:
            status, body = standing
            content = body if isinstance(body, str) else json.dumps(body)
            return httpx.Response(status, content=content.encode())
        return httpx.Response(200, json={"ok": True})

    def routes(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _, _ in self.calls]


def lemlist_double(
    monkeypatch: pytest.MonkeyPatch, **options: Any
) -> tuple[FakeLemlist, LemlistEmailProvider]:
    """A Lemlist adapter wired to a fresh `FakeLemlist`: no real wait (sleeps
    are recorded), a fresh rate-limit window, the test key. `options` go to
    the adapter's constructor."""
    fake = FakeLemlist()
    monkeypatch.setattr("src.email_campaigns.providers.lemlist.time.sleep", fake.sleeps.append)
    monkeypatch.setattr(lemlist_module, "_THROTTLE", lemlist_module._Throttle())
    provider = LemlistEmailProvider(
        FAKE_LEMLIST_KEY,
        base_url="https://api.lemlist.com/api",
        transport=httpx.MockTransport(fake.handler),
        **options,
    )
    return fake, provider


@pytest.fixture
def fake_lemlist(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeLemlist, LemlistEmailProvider]:
    return lemlist_double(monkeypatch)


def observed_export_row(
    lead_id: str,
    email: str,
    state: str,
    state_system: str,
    *,
    company: str | None = None,
    **variables: str,
) -> dict[str, Any]:
    """One row of `GET /v2/campaigns/{id}/export/leads?state=all&format=json`
    as the real team's export answered on 08/10/2026 (notes §17), not as
    documented: `email`, `_id`, `state`, `stateSystem`, `companyName`, then
    the campaign's own variables; never `status`, `lastState`, `contactId`
    nor `isPaused`, and `firstName` / `lastName` only when the campaign has
    them (given here among `variables`)."""
    return {
        "email": email,
        "_id": lead_id,
        "state": state,
        "stateSystem": state_system,
        "companyName": company,
        **variables,
    }


class FakeProvider:
    """An external provider for the manager's tests: real-looking ids, every
    call recorded, states and refusals scripted. Ported from Prism's
    `tests/test_email_campaigns.py` (`FakeProvider` L64-246); installed in
    place of `email_manager.provider_for` (signature `(settings, row)`) by
    the `fake_email_provider` fixture of the email plugin. Nothing leaves the
    process: it never opens a socket.

    Nidria additions: `failures` also covers the per-lead operations
    (pause, resume, remove: keyed by lead id; adding a lead: keyed by its
    address) and the campaign's pause and step parking (keyed by campaign id
    and step id), so a bulk action's failed row and a refused gesture can be
    shown. An event without a campaign id (an event about a whole contact)
    is in every campaign's read of the activities.

    Lot C: the import's reads (`ImportReader`), scripted per campaign: the
    team's campaigns (`campaign_rows`), a campaign's sequences
    (`sequence_reads`), the leads Lemlist lists beyond those Nidria added
    (`listed`), its export (`exports`), the contacts lookup (`contacts`); a
    campaign's activities are `events`. All are reads, recorded in `ops`
    like the rest; `resume_campaign` is the one write of an imported
    campaign's resume."""

    name = "LEMLIST"

    def __init__(self) -> None:
        #: False makes it answer like the dry run's place (a real id refused).
        self.external = True
        self.ops: list[tuple[str, dict[str, Any]]] = []
        self.events: list[ProviderEvent] = []
        #: (lead id, step id) the provider already reached: content refused.
        self.reached: set[tuple[str, str]] = set()
        self.paused: set[str] = set()
        self.unsubscribed: set[str] = set()
        self.contents: dict[tuple[str, str], StepContent] = {}
        #: campaign id -> the leads it holds, as added.
        self.added: dict[str, list[LeadRef]] = {}
        #: Leads Lemlist no longer lists (removed in its UI).
        self.gone: set[str] = set()
        #: campaign id -> what Lemlist says of it (default: running).
        self.remote: dict[str, ProviderCampaign] = {}
        #: contact id -> the inbox thread.
        self.inbox: dict[str, list[ProviderMessage]] = {}
        #: (operation, campaign, step or lead id) -> the error it raises.
        self.failures: dict[tuple[str, str], ProviderError] = {}
        #: `since` of every activities read.
        self.since: list[datetime | None] = []
        #: The team's senders, as the provider lists them.
        self.senders = [ProviderSender("usr_1", "sender@nidria.test", 2)]
        #: campaign id -> its sequence's step ids, in order.
        self.sequences: dict[str, list[str]] = {}
        self.parked: set[str] = set()
        #: The Lemlist team the key opens.
        self.team: str | None = "tea_current"
        #: lead id -> Lemlist's word on its progress ("inProgress", "done").
        self.lead_status: dict[str, str] = {}
        #: The import's reads (lot C): see the class docstring.
        self.campaign_rows: list[ProviderCampaignRow] = []
        self.sequence_reads: dict[str, ProviderSequences] = {}
        self.listed: dict[str, list[ProviderLead]] = {}
        self.exports: dict[str, list[ProviderLeadIdentity]] = {}
        self.contacts: dict[str, ProviderContact] = {}
        self._n = 0

    def _fail(self, operation: str, key: str) -> None:
        error = self.failures.get((operation, key))
        if error is not None:
            raise error

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n}"

    def _op(self, operation: str, /, **kwargs: Any) -> None:
        self.ops.append((operation, kwargs))

    def names(self) -> list[str]:
        return [name for name, _ in self.ops]

    def create_campaign(self, spec: CampaignSpec) -> CampaignRef:
        self._op("create_campaign", name=spec.name, schedule=spec.schedule, senders=spec.sender_ids)
        return CampaignRef(self._id("cam"), self._id("seq"), self._id("skd"))

    def update_campaign(self, ref: CampaignRef, spec: CampaignSpec) -> None:
        self._op("update_campaign", campaign=ref.campaign_id, senders=spec.sender_ids)

    def list_senders(self) -> list[ProviderSender]:
        self._op("list_senders")
        self._fail("list_senders", "team")
        return list(self.senders)

    def team_id(self) -> str | None:
        return self.team

    def start_campaign(self, ref: CampaignRef, *, mailbox: str | None = None) -> None:
        self._op("start_campaign", campaign=ref.campaign_id, mailbox=mailbox)
        self._fail("start_campaign", ref.campaign_id)

    def resume_campaign(self, ref: CampaignRef) -> None:
        self._op("resume_campaign", campaign=ref.campaign_id)
        self._fail("resume_campaign", ref.campaign_id)

    def pause_campaign(self, ref: CampaignRef) -> None:
        self._op("pause_campaign", campaign=ref.campaign_id)
        self._fail("pause_campaign", ref.campaign_id)

    def archive_campaign(self, ref: CampaignRef) -> None:
        self._op("archive_campaign", campaign=ref.campaign_id)

    def read_campaign(self, ref: CampaignRef) -> ProviderCampaign:
        self._op("read_campaign", campaign=ref.campaign_id)
        self._fail("read_campaign", ref.campaign_id)
        return self.remote.get(
            ref.campaign_id, ProviderCampaign(ref.campaign_id, exists=True, status="running")
        )

    def create_step(self, ref: CampaignRef, step: StepSpec) -> str:
        self._op("create_step", position=step.position, delay=step.delay_days)
        self._fail("create_step", ref.campaign_id)
        step_id = self._id("stp")
        self.sequences.setdefault(ref.campaign_id, []).append(step_id)
        return step_id

    def update_step(self, ref: CampaignRef, step_id: str, step: StepSpec) -> None:
        self._op("update_step", step=step_id, delay=step.delay_days)
        self.parked.discard(step_id)

    def delete_step(self, ref: CampaignRef, step_id: str) -> None:
        self._op("delete_step", step=step_id)
        self._fail("delete_step", step_id)
        if step_id in self.sequences.get(ref.campaign_id, []):
            self.sequences[ref.campaign_id].remove(step_id)

    def read_steps(self, ref: CampaignRef) -> list[ProviderStep]:
        self._op("read_steps", campaign=ref.campaign_id)
        return [
            ProviderStep(step_id, index + 1, 1500 if step_id in self.parked else 1)
            for index, step_id in enumerate(self.sequences.get(ref.campaign_id, []))
        ]

    def park_step(self, ref: CampaignRef, step_id: str) -> None:
        self._op("park_step", step=step_id)
        self._fail("park_step", step_id)
        self.parked.add(step_id)

    def reorder_steps(self, ref: CampaignRef, steps: list[tuple[str, StepSpec]]) -> None:
        self._op("reorder_steps", steps=[s for s, _ in steps])

    def add_contact(self, ref: CampaignRef, lead: LeadSpec) -> LeadRef:
        self._op("add_contact", email=lead.email, first=lead.first_name, last=lead.last_name)
        self._fail("add_contact", lead.email)
        added = LeadRef(self._id("lea"), self._id("ctc"))
        self.added.setdefault(ref.campaign_id, []).append(added)
        return added

    def set_step_content(
        self, ref: CampaignRef, lead: LeadRef, step_id: str, content: StepContent
    ) -> bool:
        self._op("set_step_content", lead=lead.lead_id, step=step_id, subject=content.subject)
        if (lead.lead_id, step_id) in self.reached:
            return False
        self.contents[(lead.lead_id, step_id)] = content
        return True

    def launch_contact(self, lead: LeadRef) -> None:
        self._op("launch_contact", lead=lead.lead_id)

    def pause_contact(self, ref: CampaignRef, lead: LeadRef) -> None:
        self._op("pause_contact", lead=lead.lead_id)
        self._fail("pause_contact", lead.lead_id)
        self.paused.add(lead.lead_id)

    def resume_contact(self, ref: CampaignRef, lead: LeadRef) -> None:
        self._op("resume_contact", lead=lead.lead_id)
        self._fail("resume_contact", lead.lead_id)
        self.paused.discard(lead.lead_id)

    def remove_contact(self, ref: CampaignRef, lead: LeadRef) -> None:
        self._op("remove_contact", lead=lead.lead_id)
        self._fail("remove_contact", lead.lead_id)

    def get_contact(self, lead: LeadRef) -> ProviderLead | None:
        return ProviderLead(lead.lead_id, None, lead.lead_id in self.paused)

    def list_contacts(self, ref: CampaignRef) -> ProviderLeadListing:
        self._op("list_contacts", campaign=ref.campaign_id)
        self._fail("list_contacts", ref.campaign_id)
        leads = [
            ProviderLead(
                lead.lead_id,
                None,
                lead.lead_id in self.paused,
                state="paused" if lead.lead_id in self.paused else "emailsSent",
                contact_id=lead.contact_id,
                unsubscribed=lead.lead_id in self.unsubscribed,
                raw={
                    "state": "emailsSent",
                    "contactId": lead.contact_id,
                    "status": self.lead_status.get(lead.lead_id, "inProgress"),
                },
            )
            for lead in self.added.get(ref.campaign_id, [])
            if lead.lead_id not in self.gone
        ]
        # The leads a campaign holds at Lemlist without Nidria adding them
        # (an imported campaign's), as listed: a pause here shows as Lemlist
        # shows it, in the state.
        for lead in self.listed.get(ref.campaign_id, []):
            if lead.lead_id in self.gone:
                continue
            paused = lead.lead_id in self.paused
            leads.append(
                ProviderLead(
                    lead.lead_id,
                    lead.email,
                    paused,
                    state="paused" if paused else lead.state,
                    contact_id=lead.contact_id,
                    unsubscribed=lead.unsubscribed or lead.lead_id in self.unsubscribed,
                    raw={**lead.raw, **({"state": "paused"} if paused else {})},
                )
            )
        return ProviderLeadListing(leads=leads, complete=True)

    # ── the import's reads (lot C) ───────────────────────────────────────

    def list_campaigns(self) -> ProviderCampaignListing:
        self._op("list_campaigns")
        self._fail("list_campaigns", "team")
        return ProviderCampaignListing(
            list(self.campaign_rows), complete=True, pages=len(self.campaign_rows) // 100 + 1
        )

    def read_sequences(self, ref: CampaignRef) -> ProviderSequences:
        self._op("read_sequences", campaign=ref.campaign_id)
        self._fail("read_sequences", ref.campaign_id)
        return self.sequence_reads.get(ref.campaign_id, ProviderSequences(ref.sequence_id, []))

    def export_leads(self, ref: CampaignRef) -> list[ProviderLeadIdentity]:
        self._op("export_leads", campaign=ref.campaign_id)
        self._fail("export_leads", ref.campaign_id)
        return list(self.exports.get(ref.campaign_id, []))

    def lookup_contacts(self, contact_ids: Sequence[str]) -> list[ProviderContact]:
        self._op("lookup_contacts", count=len(contact_ids))
        self._fail("lookup_contacts", "team")
        return [self.contacts[c] for c in contact_ids if c in self.contacts]

    def read_activities(self, ref: CampaignRef) -> ProviderActivityRead:
        self._op("read_activities", campaign=ref.campaign_id)
        self._fail("read_activities", ref.campaign_id)
        events = [e for e in self.events if e.campaign_id in (ref.campaign_id, None)]
        return ProviderActivityRead(
            events, rows=len(events), pages=len(events) // 100 + 1, complete=True
        )

    def sync_campaign(self, ref: CampaignRef, since: datetime | None) -> list[ProviderEvent]:
        self._op("sync_campaign", campaign=ref.campaign_id)
        self._fail("sync_campaign", ref.campaign_id)
        self.since.append(since)
        return [e for e in self.events if e.campaign_id in (ref.campaign_id, "cam_not_ours", None)]

    def sync_contact(self, ref: CampaignRef, lead: LeadRef) -> list[ProviderEvent]:
        return []

    def get_messages(self, lead: LeadRef) -> list[ProviderMessage]:
        self._op("get_messages", contact=lead.contact_id)
        self._fail("get_messages", lead.contact_id or "")
        return list(self.inbox.get(lead.contact_id or "", []))
