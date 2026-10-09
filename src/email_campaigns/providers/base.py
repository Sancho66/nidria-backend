"""EmailProvider: what Nidria asks of an email-sending service.

Nidria's domain never sees a provider DTO: the manager speaks in these specs
and references, and each adapter maps them to its own API. Nidria renders every
email itself, so a provider's sequence holds one positional step per Nidria
step and each lead receives its own exact content for each of them.

Ported from Prism's `providers/base.py` (reference commit 629a2b0). One
addition: `ProviderError.params`, the named values a refusal carries so the
frontend composes the sentence (Nidria rule N9: errors are codes plus params).

Lot C (decision of 08/10/2026): the import of the campaigns already running
in the agency's provider workspace reads them through `ImportReader`, reads
only; and `EmailProvider.resume_campaign` starts again a campaign Nidria
does not own (an imported one) without touching its senders.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class ProviderError(Exception):
    """A provider refused or failed. `code` is stable enough to branch on.

    `params` are the values the refusal names (a mailbox, a count), for the
    `email.provider_*` error the caller raises; the message is only a
    technical English detail, never what the interface shows."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_ERROR",
        status: int | None = None,
        params: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.params: dict[str, Any] = dict(params or {})


class ProviderUnsupported(ProviderError):
    """The provider has no such operation; the adapter says what it did instead."""

    def __init__(self, message: str):
        super().__init__(message, code="UNSUPPORTED")


@dataclass(frozen=True)
class ScheduleSpec:
    """When the provider may send: ISO weekdays (1 = Monday) and a daily window."""

    timezone: str
    weekdays: tuple[int, ...]
    start: str
    end: str


@dataclass(frozen=True)
class CampaignSpec:
    name: str
    schedule: ScheduleSpec | None = None
    #: Stop a lead's sequence as soon as they reply.
    stop_on_reply: bool = True
    #: The provider users who send the campaign's emails (Lemlist: its
    #: senders). Empty: the provider's default (Lemlist: the team's only one).
    sender_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderSender:
    """Someone a campaign can be sent from, with the mailbox address shown."""

    user_id: str
    email: str | None
    #: How many of the provider's campaigns this sender already sends.
    campaigns: int = 0


@dataclass(frozen=True)
class CampaignRef:
    campaign_id: str
    sequence_id: str | None = None
    schedule_id: str | None = None


@dataclass(frozen=True)
class StepSpec:
    position: int
    name: str
    delay_days: int


@dataclass(frozen=True)
class ProviderStep:
    """One step of the provider's sequence, in its order."""

    step_id: str
    #: 1-based place in the provider's sequence.
    position: int
    delay_days: int


@dataclass(frozen=True)
class LeadSpec:
    """The person handed to the provider.

    Decision A8 (data minimisation): Nidria fills `email`, `first_name` and
    `last_name` only, from the client's N2 identity
    (`variables.client_identity`). The company, job title and LinkedIn fields
    stay in the contract (the Lemlist adapter maps them, as in Prism) but the
    code that builds a lead (phase 4, `email_sync`) leaves them empty."""

    email: str
    first_name: str | None = None
    last_name: str | None = None
    company_name: str | None = None
    company_domain: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None


@dataclass(frozen=True)
class LeadRef:
    lead_id: str
    contact_id: str | None = None


@dataclass(frozen=True)
class StepContent:
    subject: str
    html: str


@dataclass(frozen=True)
class ProviderLead:
    lead_id: str
    email: str | None
    is_paused: bool
    state: str | None = None
    contact_id: str | None = None
    unsubscribed: bool = False
    #: What the provider said about the lead, verbatim (known keys only).
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderLeadListing:
    """A campaign's leads, and whether all of them were read: a lead's absence
    proves nothing unless the listing is complete."""

    leads: list[ProviderLead]
    complete: bool


@dataclass(frozen=True)
class ProviderCampaign:
    campaign_id: str
    #: False: the provider no longer knows the campaign (deleted there).
    exists: bool
    #: The provider's own lifecycle word (Lemlist: running, paused, draft,
    #: ended, archived, errors), kept as said.
    status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: The campaign's main sequence (Lemlist: `sequenceId`, "Main sequence ID").
    sequence_id: str | None = None


@dataclass(frozen=True)
class ProviderMessage:
    """One email of a contact's thread in the provider's inbox."""

    #: The provider's activity id: the same id as the SENT / REPLIED event.
    message_id: str
    direction: str
    occurred_at: datetime
    campaign_id: str | None
    lead_id: str | None
    contact_id: str | None
    step_position: int | None
    subject: str | None
    body_html: str | None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderEvent:
    """One activity, normalized. `type` is Nidria's vocabulary; `provider_type`
    and `payload` keep what the provider actually said."""

    event_id: str
    type: str
    provider_type: str
    occurred_at: datetime
    campaign_id: str | None
    lead_id: str | None
    email: str | None
    #: 1-based Nidria position of the step, when the event belongs to one.
    step_position: int | None
    reason: str | None
    payload: dict[str, Any] = field(default_factory=dict)


# ── what the import reads of the provider's own campaigns (lot C) ──────────


@dataclass(frozen=True)
class ProviderCampaignRow:
    """One campaign of the provider workspace's list, as the provider said
    it: its lifecycle word (Lemlist: running, paused, draft, ended,
    archived, errors) and its creation time (ISO), the import's order."""

    campaign_id: str
    name: str | None
    status: str | None
    created_at: str | None = None


@dataclass(frozen=True)
class ProviderCampaignListing:
    """The workspace's campaigns, and whether all of them were read."""

    campaigns: list[ProviderCampaignRow]
    complete: bool
    #: Pages read, one call each.
    pages: int = 1


@dataclass(frozen=True)
class ProviderSequenceStep:
    """One step of a campaign's main sequence, in its order, with the step
    as the provider returned it: its content is the provider's own."""

    step_id: str
    #: 1-based place in the main sequence.
    position: int
    #: The provider's step type (Lemlist: email, linkedinSend, manual...).
    type: str | None
    delay_days: int
    #: Under an A/B test: the content kept is variant A's.
    ab_test: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderSequences:
    """A campaign's sequences: the main one's steps in order, and the
    conditional branches, counted only (they are not imported)."""

    main_sequence_id: str | None
    steps: list[ProviderSequenceStep]
    branches: int = 0
    branch_steps: int = 0


@dataclass(frozen=True)
class ProviderLeadIdentity:
    """Who one lead of a campaign is, and where it stands (Lemlist: a row
    of the campaign's export)."""

    lead_id: str
    email: str | None
    first_name: str | None = None
    last_name: str | None = None
    company_name: str | None = None
    #: Lemlist's `status` (notInterested, interested, unsubscribed, review,
    #: scanning, running, paused, done) and the lead's last state (`state`,
    #: else `lastState`), as said; any of them may be missing.
    status: str | None = None
    last_state: str | None = None
    #: Lemlist's `stateSystem`, as said: undocumented, the only word on the
    #: lead's progress the real export gives (inProgress, reviewed, paused,
    #: done, unsubscribed).
    state_system: str | None = None


@dataclass(frozen=True)
class ProviderContact:
    """A workspace contact: its address, its names and, per (campaign id,
    lead id), the lead's status in that campaign."""

    contact_id: str
    email: str | None
    first_name: str | None = None
    last_name: str | None = None
    lead_statuses: dict[tuple[str, str], str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderActivityRead:
    """A campaign's whole history: the email events Nidria tracks, the
    activities read (every type), the pages, and whether all were read."""

    events: list[ProviderEvent]
    rows: int
    pages: int
    complete: bool


@runtime_checkable
class ImportReader(Protocol):
    """What the import of the campaigns already running in the provider
    workspace reads, and only reads. Outside `EmailProvider`: the dry run
    has nothing to import, and an import needs the agency to talk to the
    provider for real."""

    def list_campaigns(self) -> ProviderCampaignListing: ...
    def read_campaign(self, ref: CampaignRef) -> ProviderCampaign: ...
    def read_sequences(self, ref: CampaignRef) -> ProviderSequences: ...
    def list_contacts(self, ref: CampaignRef) -> ProviderLeadListing: ...
    def export_leads(self, ref: CampaignRef) -> list[ProviderLeadIdentity]: ...
    def lookup_contacts(self, ids_or_emails: Sequence[str]) -> list[ProviderContact]: ...
    def read_activities(self, ref: CampaignRef) -> ProviderActivityRead: ...


class EmailProvider(Protocol):
    name: str
    #: False for the dry-run provider: the UI says so and nothing leaves Nidria.
    external: bool

    def create_campaign(self, spec: CampaignSpec) -> CampaignRef: ...
    def update_campaign(self, ref: CampaignRef, spec: CampaignSpec) -> None: ...
    #: `mailbox`: the one chosen in Nidria; nothing starts until it sends.
    def start_campaign(self, ref: CampaignRef, *, mailbox: str | None = None) -> None: ...
    #: Start again, as it stands at the provider, a campaign Nidria does not
    #: own (an imported one): no sender assigned, no mailbox checked.
    def resume_campaign(self, ref: CampaignRef) -> None: ...
    def pause_campaign(self, ref: CampaignRef) -> None: ...
    def archive_campaign(self, ref: CampaignRef) -> None: ...
    def read_campaign(self, ref: CampaignRef) -> ProviderCampaign: ...
    def list_senders(self) -> list[ProviderSender]: ...
    #: The provider workspace (Lemlist: team) the platform key opens. None:
    #: the provider has none (the dry run). A provider that has one and
    #: cannot say which raises ProviderError(code="TEAM_UNKNOWN"): unknown is
    #: never "no team" nor "another team" (decision A4).
    def team_id(self) -> str | None: ...

    #: Appended after the provider's last step: once contacts are in a
    #: campaign, Lemlist adds nowhere else.
    def create_step(self, ref: CampaignRef, step: StepSpec) -> str: ...
    def update_step(self, ref: CampaignRef, step_id: str, step: StepSpec) -> None: ...
    def delete_step(self, ref: CampaignRef, step_id: str) -> None: ...
    #: The provider's sequence as it is, in order.
    def read_steps(self, ref: CampaignRef) -> list[ProviderStep]: ...
    #: A step the provider no longer lets anyone remove, put out of reach: no
    #: lead gets to it, and the next step added reuses it.
    def park_step(self, ref: CampaignRef, step_id: str) -> None: ...
    def reorder_steps(self, ref: CampaignRef, steps: list[tuple[str, StepSpec]]) -> None: ...

    def add_contact(self, ref: CampaignRef, lead: LeadSpec) -> LeadRef: ...
    def set_step_content(
        self, ref: CampaignRef, lead: LeadRef, step_id: str, content: StepContent
    ) -> bool: ...
    def launch_contact(self, lead: LeadRef) -> None: ...
    def pause_contact(self, ref: CampaignRef, lead: LeadRef) -> None: ...
    def resume_contact(self, ref: CampaignRef, lead: LeadRef) -> None: ...
    def remove_contact(self, ref: CampaignRef, lead: LeadRef) -> None: ...
    def get_contact(self, lead: LeadRef) -> ProviderLead | None: ...
    def list_contacts(self, ref: CampaignRef) -> ProviderLeadListing: ...

    def sync_campaign(self, ref: CampaignRef, since: datetime | None) -> list[ProviderEvent]: ...
    def sync_contact(self, ref: CampaignRef, lead: LeadRef) -> list[ProviderEvent]: ...
    #: The contact's thread, replies included when the provider has their content.
    def get_messages(self, lead: LeadRef) -> list[ProviderMessage]: ...
