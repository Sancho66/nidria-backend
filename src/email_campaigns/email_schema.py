"""Pydantic contracts of the Email module.

Two surfaces:
- the platform's (superadmin): activation and Lemlist team binding,
  `/agencies/{agency_id}/email-module` (phase 1);
- the agents' (`/email/*`, phase 3): the module's meta, campaigns, steps, AI
  variables, previews and recipients (memberships), ported from Prism's
  `email_schema.py` (reference commit 629a2b0).

Adaptations to Nidria: UUID identifiers; no project, scope, legacy workflow
or address verification; a recipient is a client record (`client_profile`),
its status next to the email status is the derived `prospect` / `client`;
every English sentence Prism returned (a refusal, a reason, a note) is a
stable code with named params the frontend translates (rule N9, decision
A12). In particular a membership's `status_reason` is a code, its params in
`status_reason_params` (see `email_manager.STATUS_REASON_PARAMS_KEY`).
"""

import uuid
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.email_campaigns.flags import ProviderMode
from src.email_campaigns.readiness import TemplateErrorKind
from src.email_campaigns.schedule import HoldCode
from src.email_campaigns.variables import VariableSource, VariableType

CampaignStatus = Literal["DRAFT", "ACTIVE", "PAUSED", "ARCHIVED"]
MemberStatus = Literal[
    "EMAIL_READY",
    "EMAIL_QUEUED",
    "EMAIL_ACTIVE",
    "EMAIL_PAUSED",
    "EMAIL_REPLIED",
    "EMAIL_BOUNCED",
    "EMAIL_COMPLETED",
    "EMAIL_REMOVED",
    "EMAIL_UNSUBSCRIBED",
]
#: The client's derived status (`derived_client_status`), never stored here.
ClientStatus = Literal["prospect", "client"]
DelayUnit = Literal["DAYS", "WEEKS"]
#: Where a campaign lives at the provider. NONE: not there yet; DRY_RUN:
#: only simulated (a `dry_` id); LIVE: a real Lemlist campaign.
CampaignProviderMode = Literal["NONE", "DRY_RUN", "LIVE"]
#: An email's furthest state; QUEUED = handed to the provider, not sent yet.
MessageStatus = Literal["QUEUED", "SENT", "DELIVERED", "OPENED", "CLICKED", "REPLIED", "BOUNCED"]
#: RENDERED = Nidria's frozen render is the content; a reply's content is
#: PENDING (read at the next sync), AVAILABLE, or UNAVAILABLE from the provider.
ContentState = Literal["RENDERED", "PENDING", "AVAILABLE", "UNAVAILABLE"]
#: Why one step cannot be deleted now (`EmailStepCapabilities.delete_reason`).
#: first_step_in_use: the first email of a sequence Lemlist holds leads of;
#: not_last_at_provider: a later step exists at Lemlist (it removes none);
#: already_sent: someone got this email, it stays with its history;
#: team_mismatch: the campaign lives in another Lemlist team (read only).
StepDeleteReason = Literal[
    "first_step_in_use", "not_last_at_provider", "already_sent", "team_mismatch"
]
#: Why a step cannot be skipped through Nidria: only Lemlist's app can.
StepSkipReason = Literal["provider_app_only"]
#: Why a sequence capability is off (`EmailSequenceCapabilities.reasons`).
SequenceReason = Literal["insert_in_use", "reorder_in_use", "archived", "team_mismatch"]
#: Why the senders list is empty or partial (`EmailSendersRead.note`).
SendersNote = Literal["dry_run", "no_sender", "provider_error"]


# ── Platform (phase 1) ──────────────────────────────────────────────────────


class EmailTeamInfo(BaseModel):
    """A Lemlist team: its id and, when Lemlist gave one, its name."""

    id: str
    name: str | None = None


class EmailCampaignCounts(BaseModel):
    """An agency's campaigns by status, every status present (0 when none)."""

    draft: int = 0
    active: int = 0
    paused: int = 0
    archived: int = 0
    # ACTIVE campaigns that exist at Lemlist (a real provider id, not a
    # dry-run one): what makes switching the module off refused.
    live_at_provider: int = 0


class AgencyEmailModuleResponse(BaseModel):
    """The Email module's state for one agency, as the platform sees it."""

    agency_id: uuid.UUID
    # The agency's own switch (its `agency_email_module` row; False without a row).
    enabled: bool
    # EMAIL_MODULE_ENABLED, the platform's master switch.
    master_enabled: bool
    # Both locks open: what the agency's agents get (`email_enabled` on its fiche).
    effective: bool
    # EMAIL_PROVIDER_EXTERNAL_EFFECTS AND Lemlist not mocked AND a key.
    external_effects: bool
    provider: str
    # The Lemlist team bound to this agency, or null (dry run).
    team: EmailTeamInfo | None
    # LIVE: real Lemlist calls. DRY_RUN: nothing leaves. UNAVAILABLE: bound,
    # effects on, and Lemlist did not say which team the key opens.
    provider_mode: ProviderMode
    # Bound, effects on, and the key opens ANOTHER team than the bound one.
    team_mismatch: bool
    enabled_at: datetime | None
    disabled_at: datetime | None
    # The last superadmin who changed the switch or the team.
    changed_by_agent_id: uuid.UUID | None
    campaigns: EmailCampaignCounts


class AgencyEmailModuleUpdateRequest(BaseModel):
    """PATCH /agencies/{agency_id}/email-module: the agency's switch."""

    enabled: bool


# ── Meta ────────────────────────────────────────────────────────────────────


class EmailVariableRead(BaseModel):
    """One variable a template may use. No label (decision A12): the frontend
    names each token in the interface's language."""

    token: str
    source: VariableSource
    type: VariableType
    #: A neutral sample value; None when any sample would be a sentence.
    example: str | None = None


class EmailMetaRead(BaseModel):
    """What the agency's Email pages need before anything else."""

    #: Always true on a 200: a module switched off answers 409
    #: `email.module_disabled` instead.
    enabled: bool
    provider: str
    #: LIVE: real Lemlist calls; DRY_RUN: nothing leaves Nidria;
    #: UNAVAILABLE: bound, effects on, and Lemlist did not say which team the
    #: platform key opens (decision A4).
    provider_mode: ProviderMode
    #: The Lemlist team bound to the agency, or null (dry run).
    team: EmailTeamInfo | None
    #: The key opens another team than the bound one: the agency's campaigns
    #: at Lemlist are read only (TEAM_MISMATCH).
    team_mismatch: bool
    can_manage: bool
    can_enroll: bool
    can_pause: bool
    can_remove: bool
    can_sync: bool
    campaign_statuses: list[CampaignStatus]
    member_statuses: list[MemberStatus]
    client_statuses: list[ClientStatus]
    variables: list[EmailVariableRead]


class EmailVariableFillRead(BaseModel):
    """How often each variable has a value among the clients a campaign
    reaches: a variable without one blocks that client unless the template
    gives it a fallback."""

    #: "campaign": the campaign's live recipients; "agency": the agency's
    #: clients with an email (a campaign without recipients yet).
    population: Literal["campaign", "agency"]
    #: The campaign counted, when the population is its recipients.
    campaign_id: uuid.UUID | None = None
    #: Clients in that population.
    total: int
    #: Clients read to count: all of them, or a random sample beyond a cap.
    considered: int
    #: Token -> clients among `considered` with a value.
    filled: dict[str, int]


# ── Senders ─────────────────────────────────────────────────────────────────


class EmailSender(BaseModel):
    """Who sends a campaign: a provider user (Lemlist: one of the team's
    senders) and the mailbox address shown for them."""

    user_id: str = Field(min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=255)


class EmailSenderOption(EmailSender):
    #: How many provider campaigns this sender already sends.
    campaigns: int = 0


class EmailSendersRead(BaseModel):
    """Who a campaign of this agency can be sent from."""

    #: False in a dry run: Lemlist is not asked.
    external: bool
    senders: list[EmailSenderOption] = Field(default_factory=list)
    #: Why the list is empty: dry_run, no_sender (none connected at Lemlist)
    #: or provider_error (`error_code` says which).
    note: SendersNote | None = None
    #: The `email.provider_*` code when Lemlist could not be read.
    error_code: str | None = None


# ── Campaigns ───────────────────────────────────────────────────────────────


class EmailCampaignSettings(BaseModel):
    """Sending settings. Timezone, days and hours become the provider
    campaign's schedule; the daily limit caps how many new leads Nidria
    hands over per day (Lemlist's own limits belong to each mailbox)."""

    timezone: str = "Europe/Paris"
    sending_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    sending_start_time: str = "09:00"
    sending_end_time: str = "18:00"
    daily_limit: int = Field(default=50, ge=1, le=1000)
    #: Delay given to a new follow-up step.
    default_delay_days: int = Field(default=3, ge=0, le=60)
    #: Who sends. None: the provider's default (Lemlist: the team's only sender).
    sender: EmailSender | None = None
    provider_settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA name, e.g. Europe/Paris") from exc
        return value

    @field_validator("sending_days")
    @classmethod
    def _days(cls, value: list[int]) -> list[int]:
        if not value or any(d < 1 or d > 7 for d in value):
            raise ValueError("sending_days are ISO weekdays 1 (Monday) to 7 (Sunday)")
        return sorted(set(value))

    @field_validator("sending_start_time", "sending_end_time")
    @classmethod
    def _time(cls, value: str) -> str:
        hours, _, minutes = value.partition(":")
        if not (
            hours.isdigit()
            and minutes.isdigit()
            and 0 <= int(hours) < 24
            and 0 <= int(minutes) < 60
        ):
            raise ValueError("times are HH:MM")
        return f"{int(hours):02d}:{int(minutes):02d}"

    @model_validator(mode="after")
    def _window(self) -> "EmailCampaignSettings":
        if self.sending_start_time >= self.sending_end_time:
            raise ValueError("the sending window must end after it starts")
        return self


class EmailCampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    settings: EmailCampaignSettings = Field(default_factory=EmailCampaignSettings)


class EmailCampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    settings: EmailCampaignSettings | None = None
    status: CampaignStatus | None = None


class EmailDuplicateRequest(BaseModel):
    """A copy's name, written by the frontend in the interface's language
    (decision A13). Omitted: the original's name, unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=255)


class EmailAiVariableRead(BaseModel):
    id: uuid.UUID
    key: str
    prompt_template: str
    model: str | None

    model_config = ConfigDict(from_attributes=True)


class EmailStepCapabilities(BaseModel):
    """What can happen to one step now."""

    can_delete: bool
    delete_reason: StepDeleteReason | None = None
    #: Lemlist skips a step in its app only: never offered through Nidria.
    can_skip: bool = False
    skip_reason: StepSkipReason | None = None
    #: Recipients this step was sent to.
    reached: int = 0
    #: Added once recipients were already in the campaign.
    added_after_launch: bool = False
    #: Not in the provider's sequence yet: the next Sync adds it there.
    pending: bool = False


class EmailSequenceCapabilities(BaseModel):
    """What can change in a campaign's sequence now. Before any lead is at
    the provider, everything; after, the provider only adds a step at the end."""

    #: The provider holds (or held) leads of this campaign.
    active_sequence: bool
    can_edit_step_content: bool
    can_edit_step_delay: bool
    can_add_step: bool
    can_insert_step: bool
    can_reorder_steps: bool
    #: Capability name -> why it is off.
    reasons: dict[str, SequenceReason] = Field(default_factory=dict)


class EmailStepImpactRead(BaseModel):
    """Who a step added at `position` reaches. Never retroactive: a recipient
    past that position, done or stopped, does not get it."""

    position: int
    mode: Literal["APPEND", "INSERT"]
    allowed: bool
    reason: Literal["insert_in_use"] | None = None
    total: int
    will_receive: int
    already_past: int
    completed: int
    paused: int
    replied: int
    removed: int
    #: Bounced or unsubscribed.
    ended: int


class EmailStepRead(BaseModel):
    id: uuid.UUID
    campaign_id: uuid.UUID
    position: int
    name: str
    type: str
    delay_value: int
    delay_unit: DelayUnit
    subject_template: str
    body_template: str
    provider_step_id: str | None
    version: int
    content_hash: str
    #: True when the provider has not received this content yet.
    unsynced: bool
    variables_count: int
    ai_variables: list[EmailAiVariableRead]
    #: Filled on a campaign's detail.
    capabilities: EmailStepCapabilities | None = None


class EmailCampaignLeadStats(BaseModel):
    """Unique leads (memberships), never messages: launched for real, reached
    by at least one email, replied at least once."""

    launched: int
    reached: int
    replied: int


class EmailCampaignRead(BaseModel):
    id: uuid.UUID
    name: str
    provider: str
    provider_campaign_id: str | None
    provider_mode: CampaignProviderMode
    status: CampaignStatus
    settings: EmailCampaignSettings
    steps_count: int
    #: Live memberships (ready, queued, active, paused).
    members_count: int
    members_by_status: dict[str, int]
    last_synced_at: datetime | None
    #: What stopped the last sync: a code (phase 4), never a sentence.
    sync_error: str | None
    unsynced_changes: bool
    #: From Nidria's synced messages: emails sent, emails opened / clicked
    #: (once each), replies, bounces.
    metrics: dict[str, int] = Field(default_factory=dict)
    #: The Email Sync job's reconciliation: the provider's own word on the
    #: campaign, when it last succeeded, and what went wrong if it did.
    provider_status: str | None = None
    last_provider_sync_at: datetime | None = None
    last_provider_error: str | None = None
    #: Lives in another Lemlist team than the one the platform key opens
    #: (rule N4): read only, its ids are never sent to the current team.
    team_mismatch: bool = False
    #: The campaign list's leads and replies; not read on a single campaign.
    lead_stats: EmailCampaignLeadStats | None = None
    created_by_agent_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class EmailCampaignDetailRead(EmailCampaignRead):
    steps: list[EmailStepRead]
    sequence: EmailSequenceCapabilities
    #: Step position -> running recipients last sent that step.
    members_by_step: dict[int, int]
    #: Step position -> sent / opened / clicked / replied / bounced, from
    #: Nidria's synced messages (a reply counts for the step it answers).
    metrics_by_step: dict[int, dict[str, int]] = Field(default_factory=dict)


class EmailCampaignDeleteRead(BaseModel):
    """What deleting a campaign did."""

    id: uuid.UUID
    name: str
    #: The provider's campaign was paused first (Lemlist's API cannot delete one).
    provider_paused: bool = False
    #: The campaign stays at Lemlist (paused), to delete in Lemlist's app.
    provider_kept: bool = False


# ── Steps and AI variables ──────────────────────────────────────────────────


class EmailStepCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    #: Omitted: 0 for the first step, the campaign's default delay after.
    delay_value: int | None = Field(default=None, ge=0, le=365)
    delay_unit: DelayUnit = "DAYS"
    subject_template: str = Field(default="", max_length=500)
    body_template: str = Field(default="", max_length=20000)
    #: 1-based; omitted = at the end.
    position: int | None = Field(default=None, ge=1)


class EmailStepUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    delay_value: int | None = Field(default=None, ge=0, le=365)
    delay_unit: DelayUnit | None = None
    subject_template: str | None = Field(default=None, max_length=500)
    body_template: str | None = Field(default=None, max_length=20000)


class EmailStepReorder(BaseModel):
    step_ids: list[uuid.UUID] = Field(min_length=1)


class EmailAiVariableCreate(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    prompt_template: str = Field(min_length=1, max_length=8000)
    model: str | None = Field(default=None, max_length=100)


class EmailAiVariableUpdate(BaseModel):
    key: str | None = Field(default=None, min_length=1, max_length=64)
    prompt_template: str | None = Field(default=None, min_length=1, max_length=8000)
    model: str | None = Field(default=None, max_length=100)


# ── Preview and AI test ─────────────────────────────────────────────────────


class EmailRenderErrorRead(BaseModel):
    """Why an email cannot be sent: a code (the renderer's error kind, or
    `first_subject_required` / `empty_body`) and the token it is about."""

    kind: TemplateErrorKind
    token: str | None = None


class EmailResolvedVariableRead(BaseModel):
    token: str
    value: str | None
    source: str
    #: The template's fallback, used because the value is missing.
    fallback: str | None = None


class EmailPreviewAiVariable(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    prompt_template: str = Field(min_length=1, max_length=8000)
    model: str | None = Field(default=None, max_length=100)


class EmailPreviewRequest(BaseModel):
    """Renders the editor's current (possibly unsaved) content for one client."""

    client_profile_id: uuid.UUID
    #: The saved step being edited, if any: a live recipient of its campaign
    #: then gets the AI values its email will carry (frozen when generated).
    step_id: uuid.UUID | None = None
    subject_template: str = Field(default="", max_length=500)
    body_template: str = Field(default="", max_length=20000)
    ai_variables: list[EmailPreviewAiVariable] = Field(default_factory=list)
    #: Calls the model; off for the live, debounced preview.
    generate_ai: bool = False
    #: Values this editor generated earlier, shown again while the user types.
    #: Rendering only: never stored, never sent.
    ai_values: dict[str, str] = Field(default_factory=dict)
    #: Position of the step being written (the first email needs a subject);
    #: taken from the saved step when previewing one.
    position: int | None = Field(default=None, ge=1)


class EmailPreviewClient(BaseModel):
    id: uuid.UUID
    first_name: str | None
    last_name: str | None
    full_name: str
    #: The effective address (rule N2): the client-space account's, else the
    #: record's own.
    email: str | None
    #: The client's company when exactly one is linked (what `company.*` reads).
    company_id: uuid.UUID | None
    company_name: str | None


class EmailPreviewResponse(BaseModel):
    client: EmailPreviewClient
    subject: str
    body: str
    variables: list[EmailResolvedVariableRead]
    errors: list[EmailRenderErrorRead]
    ai_values: dict[str, str]
    #: AI key -> the `email.*` code of its generation failure.
    ai_errors: dict[str, str]
    sendable: bool


class EmailAiTestRequest(BaseModel):
    client_profile_id: uuid.UUID
    key: str = Field(min_length=1, max_length=64)
    prompt_template: str = Field(min_length=1, max_length=8000)
    model: str | None = Field(default=None, max_length=100)


class EmailAiTestResponse(BaseModel):
    """The prompt resolved for one client and the model's answer. Not served
    while AI generation is not configured (409 `email.ai_not_configured`)."""

    input_resolved: str
    output: str | None
    model: str | None
    errors: list[EmailRenderErrorRead]
    #: The `email.*` code of a generation failure.
    error: str | None


# ── Recipients (memberships) ────────────────────────────────────────────────


class EmailIssueRead(BaseModel):
    """One thing between a lead and its emails (`readiness.Issue`): a code
    and its named params, translated by the frontend. A blocking one keeps
    it from being added or launched; a warning decides nothing."""

    code: str
    params: dict[str, str | int] = Field(default_factory=dict)
    blocking: bool


class EmailMemberRead(BaseModel):
    """One client's membership of one campaign."""

    id: uuid.UUID
    campaign_id: uuid.UUID
    campaign_name: str
    client_profile_id: uuid.UUID
    #: The client's effective identity (rule N2).
    first_name: str | None
    last_name: str | None
    full_name: str
    email: str | None
    #: The client's company when exactly one is linked.
    company_id: uuid.UUID | None
    company_name: str | None
    client_status: ClientStatus
    status: MemberStatus
    #: Why it is in that state when it matters: a stable code (`removed`,
    #: `moved`, ...), never a sentence; its params apart.
    status_reason: str | None
    status_reason_params: dict[str, Any] = Field(default_factory=dict)
    current_step: int | None
    steps_count: int
    last_event_type: str | None
    last_event_at: datetime | None
    last_sent_at: datetime | None = None
    last_opened_at: datetime | None = None
    last_clicked_at: datetime | None = None
    last_replied_at: datetime | None = None
    added_at: datetime
    updated_at: datetime
    #: Handed to the provider (launched); None while it is still to launch.
    queued_at: datetime | None = None
    #: Its first email sent.
    started_at: datetime | None = None
    paused_at: datetime | None = None
    removed_at: datetime | None = None
    #: Still to launch: what stands between it and a launch.
    issues: list[EmailIssueRead] = Field(default_factory=list)
    provider_lead_id: str | None
    #: The provider's own state for the lead, as it said it.
    provider_status: str | None = None
    #: The lead exists only in a dry run.
    dry_run: bool = False
    #: The agents behind the gestures (the module writes no activity log, N7).
    added_by_agent_id: uuid.UUID | None = None
    paused_by_agent_id: uuid.UUID | None = None
    removed_by_agent_id: uuid.UUID | None = None
    #: The membership a move opened in the target campaign.
    moved_to_id: uuid.UUID | None = None


class EmailEventRead(BaseModel):
    id: uuid.UUID
    type: str
    provider_type: str
    step_position: int | None
    occurred_at: datetime
    campaign_member_id: uuid.UUID | None


class EmailReplyRead(BaseModel):
    """A reply: detected by its event; its text when the provider gave it."""

    id: uuid.UUID
    step_position: int | None
    received_at: datetime | None
    subject: str | None
    #: The new message as text, without the history it quotes. The HTML the
    #: provider returned is kept, never sent to the browser.
    body_text: str | None
    content_state: ContentState


class EmailStepTimeline(BaseModel):
    """One step of one membership: what was (or will be) sent, and what came of it."""

    position: int
    step_id: uuid.UUID | None
    step_name: str | None
    delay_days: int | None
    #: Not sent yet: the day it should go out, an estimate in the campaign's
    #: timezone, never an hour. None when it cannot be dated (see the hold).
    planned_on: date | None
    #: Estimated for a day already past and not reported sent.
    planned_overdue: bool = False
    #: None while the provider has not received this email.
    status: MessageStatus | None
    subject: str | None
    body: str | None
    #: The AI values this email carries, frozen with its render.
    ai_values: dict[str, str] = Field(default_factory=dict)
    template_version: int | None
    #: Not sent yet and the template changed since: the next sync refreshes it.
    outdated: bool
    rendered_at: datetime | None
    provider_synced_at: datetime | None
    sent_at: datetime | None
    open_count: int = 0
    click_count: int = 0
    first_opened_at: datetime | None = None
    last_opened_at: datetime | None = None
    last_clicked_at: datetime | None = None
    replied_at: datetime | None = None
    bounced_at: datetime | None = None
    events: list[EmailEventRead] = Field(default_factory=list)
    replies: list[EmailReplyRead] = Field(default_factory=list)


class EmailConversationItem(BaseModel):
    """One email of the conversation, in time order: sent by Nidria or received."""

    direction: Literal["OUTBOUND", "INBOUND"]
    at: datetime
    step_position: int | None
    subject: str | None
    body: str | None
    status: MessageStatus | None
    content_state: ContentState
    open_count: int = 0
    click_count: int = 0


class EmailScheduleHoldRead(BaseModel):
    """Why no next email can be dated (`schedule.hold_of`): a code and its params."""

    code: HoldCode
    params: dict[str, str] = Field(default_factory=dict)


class EmailMemberDetail(EmailMemberRead):
    steps: list[EmailStepTimeline]
    schedule_hold: EmailScheduleHoldRead | None = None
    conversation: list[EmailConversationItem]
    #: Every provider event of the membership, newest first (the audit trail).
    events: list[EmailEventRead]


class EmailMemberListResponse(BaseModel):
    items: list[EmailMemberRead]
    total: int
    page: int
    page_size: int
    #: Memberships per status under every filter but the status one.
    status_counts: dict[str, int]


class EmailMemberAction(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)


class EmailMemberMove(EmailMemberAction):
    target_campaign_id: uuid.UUID


class EmailActionResultRow(BaseModel):
    """One membership of a bulk action. A failed row carries a CODE
    (`email.not_found`, `permission.denied`, `email.provider_*`, ...) and its
    params, never a sentence."""

    id: uuid.UUID
    ok: bool
    status: str | None = None
    code: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    #: The membership a move opened in the target campaign.
    new_id: uuid.UUID | None = None


class EmailActionResult(BaseModel):
    done: int
    failed: int
    rows: list[EmailActionResultRow]
