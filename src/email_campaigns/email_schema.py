"""Pydantic contracts of the Email module.

Three surfaces:
- the platform's (superadmin): activation and Lemlist team binding,
  `/agencies/{agency_id}/email-module` (phase 1), and the platform's Lemlist
  account, `/admin/email/provider` (phase 4);
- Lemlist's webhook, `/webhooks/lemlist` (phase 4, its acknowledgement);
- the agents' (`/email/*`, phase 3): the module's meta, campaigns, steps, AI
  variables, previews and recipients (memberships), then the add wizard, a
  client record's email state, the Sync, the Launch, the performance and the
  outreach page, ported from Prism's `email_schema.py` (reference commit
  629a2b0); and the import of the Lemlist campaigns already running,
  `/email/import/lemlist` (lot C): its plan, in counts only, and its
  application, per campaign.

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
#: Where a campaign comes from: made in Nidria, or a mirror of a campaign
#: that already ran in the agency's Lemlist team (read only, lot C).
CampaignOrigin = Literal["NIDRIA", "LEMLIST_IMPORT"]
#: An email's furthest state; QUEUED = handed to the provider, not sent yet.
MessageStatus = Literal["QUEUED", "SENT", "DELIVERED", "OPENED", "CLICKED", "REPLIED", "BOUNCED"]
#: RENDERED = Nidria's frozen render is the content; a reply's content is
#: PENDING (read at the next sync), AVAILABLE, or UNAVAILABLE from the provider;
#: PROVIDER_ONLY = an email of an imported campaign, sent by Lemlist from its
#: own template: its personalized content is not in Nidria.
ContentState = Literal["RENDERED", "PENDING", "AVAILABLE", "UNAVAILABLE", "PROVIDER_ONLY"]
#: Why one step cannot be deleted now (`EmailStepCapabilities.delete_reason`).
#: first_step_in_use: the first email of a sequence Lemlist holds leads of;
#: not_last_at_provider: a later step exists at Lemlist (it removes none);
#: already_sent: someone got this email, it stays with its history;
#: team_mismatch: the campaign lives in another Lemlist team (read only);
#: imported: the campaign mirrors Lemlist's, whose steps are Lemlist's.
StepDeleteReason = Literal[
    "first_step_in_use", "not_last_at_provider", "already_sent", "team_mismatch", "imported"
]
#: Why a step cannot be skipped through Nidria: only Lemlist's app can.
StepSkipReason = Literal["provider_app_only"]
#: Why a sequence capability is off (`EmailSequenceCapabilities.reasons`).
SequenceReason = Literal["insert_in_use", "reorder_in_use", "archived", "team_mismatch", "imported"]
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
    """PATCH /agencies/{agency_id}/email-module: the agency's switch.

    `pause_campaigns`, read when switching OFF only: every ACTIVE campaign
    of the agency is paused first, at Lemlist and in Nidria (the
    `pause-all` gesture), then the module is switched off. A campaign still
    ACTIVE at Lemlist after that keeps the module on (409
    `email.campaigns_live`). Without it, an ACTIVE campaign at Lemlist
    refuses the switch-off, as before."""

    enabled: bool
    pause_campaigns: bool = False


class EmailPauseFailure(BaseModel):
    """A campaign the pause could not stop, and why (an `email.*` code)."""

    campaign_id: uuid.UUID
    #: Its name, for the platform's card: the superadmin reads none of the
    #: agency's campaign lists.
    campaign_name: str
    code: str
    params: dict[str, Any] = Field(default_factory=dict)


class EmailPauseAllResult(BaseModel):
    """POST /agencies/{agency_id}/email-module/pause-all: every ACTIVE
    campaign of the agency paused, at Lemlist and in Nidria. Idempotent: a
    second call pauses nothing more."""

    #: ACTIVE campaigns paused by this call.
    paused: int
    #: Campaigns that were not ACTIVE (paused, draft or archived): nothing to do.
    already_paused: int
    #: ACTIVE campaigns left running, each with its name and its code.
    failed: list[EmailPauseFailure] = Field(default_factory=list)
    #: The module's state after the call.
    module: AgencyEmailModuleResponse


# ── Platform: the Lemlist account (phase 4, spec 10.5) ──────────────────────


class EmailProviderSenderRead(BaseModel):
    """A mailbox of the team a campaign can be sent from."""

    user_id: str
    email: str | None = None
    #: How many of the team's campaigns this sender already sends.
    campaigns: int = 0


class EmailProviderWebhookRead(BaseModel):
    """The team's webhook for EMAIL_WEBHOOK_URL, as Lemlist lists it. Its
    secret is never returned by Lemlist, and never by Nidria."""

    #: EMAIL_WEBHOOK_URL (null when the env does not set it).
    configured_url: str | None = None
    #: A webhook of the team targets exactly that URL.
    exists: bool = False
    hook_id: str | None = None
    #: The webhook is limited to one event type (Nidria wants none: every event).
    has_type: bool = False


class EmailBoundAgencyRead(BaseModel):
    id: uuid.UUID
    name: str


class EmailProviderAccountRead(BaseModel):
    """GET /admin/email/provider: the platform's Lemlist account, read only.

    `dry_run` true: real effects are off (or Lemlist is mocked, or there is
    no key) and Lemlist was NOT asked: only the local facts are filled.
    The key is designated by `key_fingerprint` (sha256[:12]), never itself."""

    dry_run: bool
    external_effects: bool
    mocked: bool
    key_configured: bool
    key_fingerprint: str | None = None
    team: EmailTeamInfo | None = None
    #: The plan as Lemlist names it (`GET /user/channels`), when it says.
    plan: str | None = None
    senders: list[EmailProviderSenderRead] = Field(default_factory=list)
    webhook: EmailProviderWebhookRead | None = None
    #: The agency bound to the team the key opens (rule N4), if any.
    bound_agency: EmailBoundAgencyRead | None = None


class EmailProviderHookRead(BaseModel):
    hook_id: str
    target_url: str | None = None
    has_type: bool = False


class EmailProviderWebhookResult(BaseModel):
    """POST /admin/email/provider/webhook: the team's webhook for
    EMAIL_WEBHOOK_URL, created by this call or found already (idempotent).
    With `recreate`, the existing one for that URL is deleted and created
    again with the current EMAIL_WEBHOOK_SECRET. Never its secret."""

    created: bool
    #: An existing webhook of that URL was deleted, then created again.
    recreated: bool
    webhook: EmailProviderHookRead


# ── The provider's webhook (phase 4, spec 7.2) ──────────────────────────────


class EmailWebhookAck(BaseModel):
    """What the webhook did with one event (Prism's answer): stored now,
    already stored (a redelivery, or the job / a Sync read it first), and
    matched to a membership. An event of a type Nidria does not track, or
    of no campaign Nidria runs, is acknowledged and not stored; an event
    about a whole contact (an unsubscribe since 30/03/2026) is stored for
    the agency bound to its Lemlist team, matched or not."""

    stored: bool
    duplicate: bool
    matched: bool


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
    #: Empty for an imported campaign's step: Lemlist names no step.
    name: str
    #: EMAIL, or OTHER_CHANNEL for an imported step of another channel.
    type: str
    delay_value: int
    delay_unit: DelayUnit
    #: Nidria's templates; empty for an imported campaign's step, whose
    #: content is Lemlist's (`raw_subject`, `raw_body_text`).
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
    #: An imported campaign's step: its Lemlist type (email, linkedinSend,
    #: manual...), and Lemlist's own template, its subject as written and
    #: its message read as text (never its HTML). That template is what
    #: Lemlist personalizes for each lead, never the text a lead received.
    provider_type: str | None = None
    raw_subject: str | None = None
    raw_body_text: str | None = None


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
    #: NIDRIA, or LEMLIST_IMPORT: a mirror of a Lemlist campaign, whose
    #: content, settings and recipients are Lemlist's (lot C).
    origin: CampaignOrigin = "NIDRIA"
    #: An imported campaign: the leads Lemlist lists that Nidria does not
    #: know (neither imported nor set aside by the last import), as last
    #: counted (the import, the Email Sync job, a Sync); null when never
    #: counted or when Lemlist's listing was incomplete. Above zero, running
    #: the import again brings them in.
    unknown_leads: int | None = None
    #: An imported campaign: when it was last imported.
    last_imported_at: datetime | None = None
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
    #: The client's company when exactly one is linked; for a lead imported
    #: from Lemlist whose record has none, the company Lemlist gave
    #: (`company_from_provider`, no company record behind it).
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
    #: `company_name` is the one Lemlist gave an imported lead (no company
    #: record of the agency is linked to the client).
    company_from_provider: bool = False


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
    #: Where this email's content is: RENDERED (`subject` and `body` are the
    #: email Nidria rendered for this lead), PROVIDER_ONLY (an imported
    #: campaign's: sent by Lemlist from its own template, its personalized
    #: content is not in Nidria, `subject` and `body` are null), or null
    #: (nothing for this step yet).
    content_state: ContentState | None = None
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


# ── A client record's email state (route 33) ────────────────────────────────

#: A client's email state, derived from its memberships when read, never
#: stored (spec 8.4): a reply anywhere wins, then anything still sending
#: (queued or active), a pause, a lead waiting to start, then how finished
#: sequences ended. A removed membership does not count.
ClientEmailStatus = Literal[
    "EMAIL_REPLIED",
    "EMAIL_IN_PROGRESS",
    "EMAIL_PAUSED",
    "EMAIL_READY",
    "EMAIL_COMPLETED",
    "EMAIL_BOUNCED",
    "EMAIL_UNSUBSCRIBED",
]


class EmailClientProfileRead(BaseModel):
    """One client record's email state next to its client status: two
    independent things, the first derived from its memberships."""

    client_profile_id: uuid.UUID
    #: None: never in a campaign of the agency (or only removed from them).
    email_status: ClientEmailStatus | None
    client_status: ClientStatus
    #: Every membership of the record in the agency's campaigns, the ended
    #: and removed ones included (its history), newest first.
    memberships: list[EmailMemberRead]


# ── The add wizard (routes 34 to 37) ────────────────────────────────────────

#: Why a client is added or not (spec 8.4). READY: added. The others are
#: kept out: no address, a malformed one, a demo one (rule N11), a block on
#: record (rule N6, never overridable), live in another campaign (never moved
#: from the wizard, decision A10), already in this campaign, or an address a
#: client chosen earlier in the same selection already carries.
Eligibility = Literal[
    "READY",
    "NO_EMAIL",
    "INVALID_EMAIL",
    "DEMO_RECIPIENT",
    "UNSUBSCRIBED",
    "BOUNCED",
    "IN_OTHER_CAMPAIGN",
    "ALREADY_IN_CAMPAIGN",
    "DUPLICATE_EMAIL",
]
#: What the add does with a row: add it, or leave it out.
PlannedAction = Literal["ADD", "SKIP"]


class EmailCandidateClient(BaseModel):
    """A client record the wizard may add: its effective identity (rule N2),
    its client status and where it stands in the agency's campaigns."""

    id: uuid.UUID
    first_name: str | None
    last_name: str | None
    full_name: str
    email: str | None
    has_email: bool
    #: The client's company when exactly one is linked.
    company_id: uuid.UUID | None
    company_name: str | None
    client_status: ClientStatus
    #: Its live membership's status, if it has one.
    email_status: MemberStatus | None
    current_campaign_id: uuid.UUID | None
    current_campaign_name: str | None


class EmailCandidateClientList(BaseModel):
    items: list[EmailCandidateClient]
    total: int
    page: int
    page_size: int


class EmailCandidateCompany(BaseModel):
    """A company the wizard may add: the clients linked to it by a role,
    those with an email and no block on record (eligible), and those also in
    no live campaign (ready). Each client counts once, whatever its roles."""

    id: uuid.UUID
    name: str
    email_status: ClientEmailStatus | None
    clients_count: int
    eligible_clients_count: int
    ready_clients_count: int


class EmailCandidateCompanyList(BaseModel):
    items: list[EmailCandidateCompany]
    total: int
    page: int
    page_size: int


class EmailAddMembersRequest(BaseModel):
    """Who to add: client records, and companies whose linked clients with
    an email come along. Nothing is ever moved from the wizard (decision
    A10) nor added despite an unsubscribe (rule N6): Prism's
    `move_existing` and `include_unsubscribed` are refused (422)."""

    model_config = ConfigDict(extra="forbid")

    client_profile_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    #: Each company brings every client linked to it that has an email.
    company_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2000)
    #: Rows taken out by hand in the preview, companies' clients included.
    exclude_client_profile_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)


class EmailAddPreviewRow(BaseModel):
    client_profile_id: uuid.UUID
    first_name: str | None
    last_name: str | None
    full_name: str
    email: str | None
    company_id: uuid.UUID | None
    company_name: str | None
    #: The live membership elsewhere (IN_OTHER_CAMPAIGN), if any.
    current_campaign_id: uuid.UUID | None
    current_campaign_name: str | None
    #: Another record the verdict comes from: the one with the same address
    #: whose block or live membership applies, or the row of this selection
    #: that already carries the address (DUPLICATE_EMAIL).
    via_client_profile_id: uuid.UUID | None = None
    eligibility: Eligibility
    action: PlannedAction


class EmailAddPreviewSummary(BaseModel):
    selected: int
    ready: int
    no_email: int
    invalid_email: int
    demo_recipient: int
    unsubscribed: int
    bounced: int
    in_other_campaign: int
    already_in_campaign: int
    duplicate_email: int
    will_add: int
    will_skip: int


class EmailAddPreviewResponse(BaseModel):
    campaign_id: uuid.UUID
    summary: EmailAddPreviewSummary
    rows: list[EmailAddPreviewRow]


class EmailAddMembersResponse(BaseModel):
    added: int
    skipped: int
    rows: list[EmailAddPreviewRow]


# ── Sync and Launch (routes 10, 13, 14) ─────────────────────────────────────

#: What a sync says beyond its counts (decision A12: codes, never sentences).
#: ready_to_launch {count}: leads wait for a Launch (a sync never hands one
#: over); daily_limit_reached {launched_today, daily_limit}; step_not_written
#: {position}: a step added to a sequence in use has no email yet, it reaches
#: Lemlist once written; follow_up_added: appended at the end of Lemlist's
#: sequence with each running lead's own email; plan_refused: Lemlist's plan
#: refuses launches, the batch stopped (Nidria never changes the plan).
SyncNoteCode = Literal[
    "ready_to_launch",
    "daily_limit_reached",
    "step_not_written",
    "follow_up_added",
    "plan_refused",
]
#: What went wrong in a sync without stopping it. follow_up_not_added {code};
#: member_not_updated {member_id, client_profile_id, then position, kind,
#: token, error for an email that cannot be rendered, or code for a provider
#: refusal}; campaign_kept_paused {exposed}: running leads could reach a step
#: without their own email, or {reason: lead_not_paused, code}: one could
#: not be paused; campaign_not_started {code}; events_not_read {code}: the
#: provider's events could not be read (the next sync reads them again).
SyncErrorCode = Literal[
    "follow_up_not_added",
    "member_not_updated",
    "campaign_kept_paused",
    "campaign_not_started",
    "events_not_read",
]


class EmailSyncNote(BaseModel):
    code: SyncNoteCode
    params: dict[str, str | int] = Field(default_factory=dict)


class EmailSyncError(BaseModel):
    code: SyncErrorCode
    params: dict[str, str | int] = Field(default_factory=dict)


class EmailSyncRequest(BaseModel):
    #: Leads handled per call; the frontend calls again while work remains.
    limit: int = Field(default=20, ge=1, le=100)


class EmailSyncResult(BaseModel):
    campaign_id: uuid.UUID
    #: False: dry run, nothing left Nidria.
    external: bool
    campaign_created: bool = False
    steps_created: int = 0
    steps_updated: int = 0
    #: Provider events read and stored now (each one once, whoever reads it).
    events_ingested: int = 0
    #: Running leads whose next emails were refreshed at the provider.
    members_updated: int = 0
    #: Running leads still to refresh: call again.
    updates_remaining: int = 0
    #: Leads handed over by this call (a launch only), and those that could not be.
    members_pushed: int = 0
    members_failed: int = 0
    #: Ready leads still waiting (a Launch, the next call, the next day).
    remaining: int = 0
    #: Running leads paused at the provider by this sync: their email for a
    #: new step is not ready yet, or they completed the sequence before it.
    members_held: int = 0
    daily_limit: int = 0
    pushed_today: int = 0
    errors: list[EmailSyncError] = Field(default_factory=list)
    notes: list[EmailSyncNote] = Field(default_factory=list)
    last_synced_at: datetime | None = None
    #: An imported campaign (whose Sync only reads Lemlist): the leads
    #: Lemlist lists that Nidria does not know; null when not counted.
    unknown_leads: int | None = None


class EmailLaunchRequest(BaseModel):
    """Launch: hand READY leads over to the provider, the selected ones or
    the next eligible ones in order. The only way a new lead reaches it;
    each is checked again by the shared rules first (spec 8.5)."""

    member_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=100)
    #: Leads handled per call; the frontend calls again while work remains.
    limit: int = Field(default=20, ge=1, le=100)


class EmailLaunchFailure(BaseModel):
    """A lead this launch did not hand over, and why: a code with its params
    (decision A12). A failure: `not_handed_over` {issue and the issue's
    params}, `not_sendable` {position, kind, token, error}, the provider's
    `unsubscribed_at_provider`, `in_other_lemlist_campaign`, `plan_refused`
    or `provider_error` {code}; the lead stays READY with that code as its
    `status_reason` (an unsubscribe ends it). A selected lead skipped:
    `not_in_campaign`, `already_launched` or `not_ready` {status}."""

    member_id: uuid.UUID
    client_profile_id: uuid.UUID | None = None
    full_name: str = ""
    code: str
    params: dict[str, str | int] = Field(default_factory=dict)


class EmailLaunchResult(EmailSyncResult):
    #: Leads (memberships) handed over by this call.
    launched_ids: list[uuid.UUID] = Field(default_factory=list)
    failures: list[EmailLaunchFailure] = Field(default_factory=list)
    #: Selected leads not to launch: launched already, ended, another campaign's.
    skipped: list[EmailLaunchFailure] = Field(default_factory=list)
    #: The provider refuses launches (its plan): no further call helps.
    stopped: bool = False


LaunchReadinessFilter = Literal["all", "ready", "blocked"]


class EmailLaunchReadinessRow(BaseModel):
    lead: EmailMemberRead
    #: READY TO LAUNCH, or BLOCKED by an issue below.
    ready: bool
    issues: list[EmailIssueRead]
    #: Unsent steps already rendered (and still current) for this lead.
    steps_rendered: int
    steps_total: int


class EmailLaunchReadiness(BaseModel):
    """What a launch would do now, read without the provider's leads and
    without a model: the campaign's gates, today's room, and each lead still
    to launch with what stands in its way (the rules the hand-over applies)."""

    campaign_id: uuid.UUID
    gates: list[EmailIssueRead]
    #: No blocking gate (the agent's right to launch aside).
    can_launch: bool
    daily_limit: int
    launched_today: int
    remaining_today: int
    to_launch: int
    ready: int
    blocked: int
    launched: int
    #: Every lead ready to launch, in launch order (at most 2,000).
    ready_ids: list[uuid.UUID]
    items: list[EmailLaunchReadinessRow]
    total: int
    page: int
    page_size: int


# ── Performance (route 12) ──────────────────────────────────────────────────


class EmailLeadFunnelRead(BaseModel):
    """Leads (one client in this campaign, a membership), never messages."""

    #: Handed to Lemlist for real (a dry run's are counted apart).
    launched: int
    launched_dry_run: int
    #: At least one email sent to them.
    reached: int
    #: At least one email opened (pixel tracking: blocked images hide opens).
    opened: int
    #: At least one link clicked.
    clicked: int
    #: At least one reply received.
    replied: int
    #: An email bounced, or the lead ended as bounced.
    bounced: int
    #: Unsubscribed (the provider's event, or the lead ended so).
    unsubscribed: int


class EmailPerformanceRates(BaseModel):
    """Each rate over the leads reached; None when no lead was reached (no
    rate is shown rather than 0)."""

    open_rate: float | None
    click_rate: float | None
    reply_rate: float | None
    bounce_rate: float | None
    unsubscribe_rate: float | None


class EmailMessageTotalsRead(BaseModel):
    """Messages, never leads: emails sent, opened / clicked at least once,
    every open and click (repeats included), replies, bounced emails."""

    sent: int
    opened: int
    opens: int
    clicked: int
    clicks: int
    replies: int
    bounced: int


class EmailStepPerformanceRead(BaseModel):
    """One step's leads: entered (its email sent to them), and of those, who
    opened, clicked, replied to it (a reply counts for the step it answers),
    bounced on it, unsubscribed from it; rates over entered. `position`
    None: replies and unsubscribes the provider tied to no step."""

    position: int | None
    name: str | None
    entered: int
    sent: int
    opened: int
    clicked: int
    replied: int
    bounced: int
    unsubscribed: int
    open_rate: float | None
    click_rate: float | None
    reply_rate: float | None
    bounce_rate: float | None
    unsubscribe_rate: float | None


class EmailPerformanceDay(BaseModel):
    """One day of a campaign's cohort, in its timezone: leads launched that
    day, and leads emailed / opening / clicking / replying / bouncing /
    unsubscribing that day (each lead once a day, never messages)."""

    day: date
    launched: int
    sent: int
    opened: int
    clicked: int
    replied: int
    bounced: int
    unsubscribed: int


class EmailPerformanceRead(BaseModel):
    """A campaign's performance from Nidria's own records (events ingested
    once, messages, memberships), never Lemlist's figures. With a period,
    the cohort is the leads launched in it (in the campaign's timezone),
    followed whenever their emails went out; `leads_in_campaign` and
    `leads_to_launch` are always now. Lemlist reports no delivery: there is
    no "delivered"."""

    campaign_id: uuid.UUID
    timezone: str
    launched_from: date | None
    launched_to: date | None
    leads_in_campaign: int
    leads_to_launch: int
    funnel: EmailLeadFunnelRead
    rates: EmailPerformanceRates
    messages: EmailMessageTotalsRead
    #: The cohort's leads by their state now.
    states: dict[str, int]
    steps: list[EmailStepPerformanceRead]
    #: Day by day, only the days with activity, oldest first.
    timeline: list[EmailPerformanceDay] = Field(default_factory=list)


# ── Outreach (route 11) ─────────────────────────────────────────────────────

OutreachKind = Literal["SENT", "REPLIED", "BOUNCED", "UNSUBSCRIBED", "SCHEDULED", "OVERDUE"]


class EmailOutreachItem(BaseModel):
    """One activity of the queue. Past ones (SENT, REPLIED, BOUNCED,
    UNSUBSCRIBED) are what the provider reported, at their time; SCHEDULED
    is an estimated DAY (no hour: Lemlist chooses it within the window);
    OVERDUE was estimated for a day already past and is not reported sent."""

    kind: OutreachKind
    #: The day it belongs to: an actual activity's day in the viewer's
    #: timezone, an estimate's in its campaign's.
    day: date
    at: datetime | None = None
    estimated: bool = False
    #: An overdue email's original estimate.
    estimated_day: date | None = None
    member_id: uuid.UUID
    client_profile_id: uuid.UUID
    full_name: str
    email: str | None = None
    company_id: uuid.UUID | None = None
    company_name: str | None = None
    campaign_id: uuid.UUID
    campaign_name: str
    step_position: int | None = None
    step_name: str | None = None
    status: MemberStatus
    sender: str | None = None


class EmailOutreachCampaign(BaseModel):
    id: uuid.UUID
    name: str
    status: CampaignStatus
    sender: str | None = None


class EmailOutreachRead(BaseModel):
    """The agency's outreach across its campaigns, day by day, read from
    Nidria's records only, never the provider."""

    start: date
    end: date
    #: The viewer's timezone the actual activities are dated in.
    timezone: str
    items: list[EmailOutreachItem]
    #: More activities than one answer carries: narrow the filters.
    truncated: bool = False
    #: Leads not launched yet: no date until someone launches them.
    waiting_launch: int
    #: Launched leads with no next date: paused, or their campaign not active
    #: (a dry run's leads are counted apart, in `dry_run`).
    on_hold: int
    #: Leads launched in a dry run whose sequence has not ended (the hold
    #: `dry_run`): nothing is sent to them, so they are never dated.
    dry_run: int
    overdue: int
    campaigns: list[EmailOutreachCampaign]


# ── The import of the Lemlist campaigns already running (lot C) ─────────────

#: The rule of the written mapping that decided a lead's fate (`RULES` of
#: `email_import`): unsubscribed, bounced, replied, skipped (during
#: Lemlist's review), not_launched (status review or scanning), completed
#: (status done), paused, launched (running, not contacted yet),
#: in_sequence (running, an email sent, opened or clicked), status_unknown
#: (neither a status nor a `stateSystem` read), uncovered (any other
#: combination, a `stateSystem` value the mapping does not know included).
ImportRule = Literal[
    "unsubscribed",
    "bounced",
    "replied",
    "skipped",
    "not_launched",
    "completed",
    "paused",
    "launched",
    "in_sequence",
    "status_unknown",
    "uncovered",
]
#: Why a lead of a Lemlist campaign is not imported, counted. no_email: no
#: address in the export nor the contacts lookup; duplicate_email: another
#: lead of the campaign carries the address; already_in_campaign: the
#: person already holds another membership of this campaign; conflict: the
#: person is live in another campaign of the agency; conflict_in_plan: live
#: in an importable campaign planned before (older at Lemlist);
#: skipped_at_provider, not_launched, status_unknown, status_uncovered: the
#: mapping's own (see `ImportRule`).
ImportSkipReason = Literal[
    "no_email",
    "duplicate_email",
    "already_in_campaign",
    "conflict",
    "conflict_in_plan",
    "skipped_at_provider",
    "not_launched",
    "status_unknown",
    "status_uncovered",
]
#: Why an address goes on the agency's opposition list.
OppositionReason = Literal["UNSUBSCRIBED", "BOUNCED"]
#: Where the word a lead's status is read from comes from, in this order:
#: the export's `status` (documented); its `stateSystem` (undocumented, the
#: only one the real export gives, observed on 08/10/2026), read as the
#: documented status it stands for; the contacts lookup's `leadState` (a
#: lead the export did not identify).
ImportStatusSource = Literal["status", "stateSystem", "leadState"]


class EmailImportRuleRead(BaseModel):
    """One line of the mapping from Lemlist's words on a lead to its Nidria
    status. A lead matches when its `state` (Lemlist's listing) is one of
    `states` or its status (`ImportStatusSource`: a `stateSystem` read as
    the status it stands for) one of `statuses` (`combine` either), or both
    (`combine` both); the first rule matching decides. `proposed`: the
    status the membership gets, null when the lead is not imported
    (`not_imported` says why)."""

    rule: ImportRule
    states: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    combine: Literal["either", "both"] = "either"
    proposed: MemberStatus | None = None
    opposition: OppositionReason | None = None
    not_imported: ImportSkipReason | None = None


class EmailImportStateRead(BaseModel):
    """One combination of Lemlist's words met in a campaign, how many leads
    carry it, and what the import does with them."""

    #: The lead's last state: Lemlist's listing, else its export.
    state: str | None
    #: The word the lead's status is read from, as Lemlist gave it, and
    #: where (`status_source`); null when Lemlist gave none.
    status: str | None
    status_source: ImportStatusSource | None = None
    #: The documented status the mapping read that word as (a `stateSystem`
    #: is read as the status it stands for); null when no word was read, or
    #: for a `stateSystem` value the mapping does not know (rule uncovered).
    status_read_as: str | None = None
    count: int
    rule: ImportRule
    proposed: MemberStatus | None = None
    #: These leads' addresses go on the agency's opposition list (an
    #: unsubscribe, a bounce), imported or not.
    opposition: OppositionReason | None = None


class EmailImportStepsRead(BaseModel):
    """A campaign's steps at Lemlist: its main sequence's, imported (by
    Lemlist type), and what is not imported (the conditional branches and
    their steps). Under an A/B test, the text imported is variant A's."""

    by_type: dict[str, int]
    imported: int
    branches: int
    branch_steps: int
    ab_tests: int


class EmailImportLeadsRead(BaseModel):
    """A campaign's leads, in counts only."""

    #: Distinct leads of Lemlist's listing, whether it was read in full,
    #: and its rows naming a lead already read (a page shifting meanwhile).
    listed: int
    listing_complete: bool
    listing_duplicates: int
    #: Leads whose identity was found: by the campaign's export, else by
    #: the contacts lookup; and those left without an address.
    identities: int
    from_export: int
    from_contacts: int
    without_email: int
    #: Among the memberships to create: leads matched to a client record of
    #: the agency by their address (rule N2), records to create, and records
    #: an importable campaign planned before creates (shared people).
    existing_profiles: int
    profiles_to_create: int
    profiles_planned_before: int
    #: Live in another campaign of the agency, or in an importable campaign
    #: planned before: not imported.
    conflicts: int
    conflicts_in_plan: int
    #: Another lead of the campaign carries the same address.
    duplicate_emails: int
    #: Memberships the import creates, and those it refreshes (imported
    #: before).
    to_create: int
    to_update: int
    #: Leads not imported, by reason.
    not_imported: dict[str, int]


class EmailImportCallsRead(BaseModel):
    """Lemlist calls, retries aside. `reads`: this campaign's in the plan:
    the campaign (1), its sequences (1), its listing (one page per 100
    leads, plus the short or empty page that ends it), its export (1) and
    its contacts lookups (one per 100 leads the export did not identify).
    `apply_min`: its share of an application: the same reads again (an
    application computes the whole plan again) and its activities, read
    only then, one page per 100 activities: counted 1 here."""

    reads: int
    apply_min: int


class EmailImportCampaignPlan(BaseModel):
    """What importing one Lemlist campaign (running or paused there) would
    do. Counts only: never an address nor a person's name."""

    provider_campaign_id: str
    name: str | None
    #: Lemlist's word on the campaign (running, paused).
    status: str | None
    #: Imported before: an import refreshes it.
    already_imported: bool
    #: Lemlist could not be read for it (an `email.provider_*` code): not
    #: importable now, the rest of the plan is empty.
    error: str | None = None
    steps: EmailImportStepsRead | None = None
    leads: EmailImportLeadsRead | None = None
    states: list[EmailImportStateRead] = Field(default_factory=list)
    calls: EmailImportCallsRead


class EmailImportCampaignRef(BaseModel):
    """A campaign of the team listed apart, never imported: a draft, an
    ended, archived or erroneous one, or one Nidria created itself."""

    provider_campaign_id: str
    name: str | None
    status: str | None


class EmailImportPlanCallsRead(BaseModel):
    """The plan's Lemlist calls (retries aside): the team's campaign list
    (one page per 100 campaigns, plus the page that ends it) and every
    importable campaign's reads (`EmailImportCallsRead`). `apply_min`: what
    one application of every importable campaign costs at least. Any
    application reads the list and every importable campaign again (each
    one's conflicts depend on those planned before it), then the
    activities of the campaigns it imports."""

    listing_pages: int
    reads: int
    total: int
    apply_min: int


class EmailImportPlanRead(BaseModel):
    """GET /email/import/lemlist: the plan, reads only. Counts, Lemlist
    campaign ids and campaign names: never an address nor a person's name.
    `campaigns`: the importable ones (running or paused at Lemlist), in the
    order an import processes them (Lemlist's creation, oldest first),
    which decides `conflicts_in_plan`."""

    listing_complete: bool
    campaigns: list[EmailImportCampaignPlan]
    not_importable: list[EmailImportCampaignRef]
    created_by_nidria: list[EmailImportCampaignRef]
    rules: list[EmailImportRuleRead]
    calls: EmailImportPlanCallsRead


class EmailImportRequest(BaseModel):
    """POST /email/import/lemlist: the Lemlist campaign ids to import, taken
    from the plan."""

    model_config = ConfigDict(extra="forbid")

    campaign_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("campaign_ids")
    @classmethod
    def _ids(cls, value: list[str]) -> list[str]:
        ids = [item.strip() for item in value]
        if any(not item or len(item) > 100 for item in ids):
            raise ValueError("campaign ids are 1 to 100 characters")
        return list(dict.fromkeys(ids))


class EmailImportCampaignResult(BaseModel):
    """What the import did for one Lemlist campaign, in its own transaction:
    `ok` false means nothing of it was written (`code`, `params`)."""

    provider_campaign_id: str
    name: str | None = None
    ok: bool
    #: Why it was refused or failed: `email.import_campaign_not_found`,
    #: `email.import_not_importable` {status}, `email.import_created_by_nidria`,
    #: an `email.provider_*` code (Lemlist could not be read), or
    #: `email.import_failed`.
    code: str | None = None
    params: dict[str, str | int] = Field(default_factory=dict)
    #: The Nidria campaign, created by this import or refreshed.
    campaign_id: uuid.UUID | None = None
    created: bool = False
    steps_created: int = 0
    steps_updated: int = 0
    members_created: int = 0
    members_updated: int = 0
    profiles_created: int = 0
    #: Not imported: live in another campaign, or in one planned before.
    conflicts: int = 0
    #: Leads not imported, by reason.
    not_imported: dict[str, int] = Field(default_factory=dict)
    #: Addresses the mapping put (or kept) on the agency's opposition list.
    opposition_listed: int = 0
    #: The campaign's history: events stored now, and already stored ones
    #: (a run again, or the webhook first).
    events_stored: int = 0
    events_duplicate: int = 0
    activities_read: int = 0
    activity_pages: int = 0
    activities_complete: bool = True
    #: Replies whose text the Email Sync job reads next (PENDING).
    replies_pending: int = 0
    unknown_leads: int | None = None


class EmailImportResult(BaseModel):
    """POST /email/import/lemlist: one result per campaign asked for, in
    the plan's order (unknown ids last). `calls`: Lemlist calls made, by the
    plan's definition, activities included."""

    campaigns: list[EmailImportCampaignResult]
    calls: int
