"""EmailProvider: what Nidria asks of an email-sending service.

Nidria's domain never sees a provider DTO: the manager speaks in these specs
and references, and each adapter maps them to its own API. Nidria renders every
email itself, so a provider's sequence holds one positional step per Nidria
step and each lead receives its own exact content for each of them.

Ported from Prism's `providers/base.py` (reference commit 629a2b0). One
addition: `ProviderError.params`, the named values a refusal carries so the
frontend composes the sentence (Nidria rule N9: errors are codes plus params).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


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


class EmailProvider(Protocol):
    name: str
    #: False for the dry-run provider: the UI says so and nothing leaves Nidria.
    external: bool

    def create_campaign(self, spec: CampaignSpec) -> CampaignRef: ...
    def update_campaign(self, ref: CampaignRef, spec: CampaignSpec) -> None: ...
    #: `mailbox`: the one chosen in Nidria; nothing starts until it sends.
    def start_campaign(self, ref: CampaignRef, *, mailbox: str | None = None) -> None: ...
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
