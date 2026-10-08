"""Nidria and Lemlist: the manual Sync and the Launch (phase 3B), the
provider's events, the reconciliation and the webhook (phase 4).

Ported from Prism's `email_sync.py` (reference commit 629a2b0).

`sync_campaign`, the manual Sync, in this order, each part idempotent:
1. the campaign at the provider: created once (the Lemlist team the key
   opens is read FIRST and noted in `provider_metadata.team_id`, rule N4),
   then kept current: name, sending days and hours (its own schedule), the
   sender chosen in Nidria, stop on reply; and one positional step per
   Nidria step (a step the provider holds beyond the last one Nidria knows,
   parked or left by a lost answer, is reused rather than added);
2. the provider's events since the campaign's checkpoint (`pull_events`,
   the same ingestion as the webhook and the job), so what was already sent
   is known before anything is pushed again;
3. for a sequence in use (spec 8.2, Prism's exact order): the campaign is
   paused at Lemlist, a step added in Nidria takes a spare step at the end
   or a new one, each running lead's OWN email for it is pushed before
   anything else (AI values included), a lead whose email cannot be
   rendered is paused (`needs_render`), a completed lead Lemlist still
   shows in progress is paused (`kept_out`), and the campaign runs again
   only if no running lead can reach a step without its own email;
4. the leads already at the provider whose next emails changed get their
   new content, only the steps they have not reached (what was sent is
   never touched, and a step Lemlist says it reached keeps the render
   Nidria holds as the email really sent); a lead the sync paused for a
   missing email is resumed once it has all of them.
It NEVER hands a new lead over (spec 8.5): that is the Launch's alone.

`launch`: the same sync, then READY leads handed over, the selected ones or
the next eligible ones in launch order, within the batch and the campaign's
daily limit. Each lead is checked again by the shared rules (readiness)
first; then, in this order: the lead added (email, first and last name
only, decision A8), each of its steps personalized with Nidria's render,
and only then the lead launched. A refusal of Lemlist's plan (402, or a 403
that names the plan) stops the batch (`email.provider_plan_required`):
Nidria never changes the plan, and the lead, added and personalized, is
taken up again by the next Launch.

Turning real effects on drops what the dry run simulated (its `dry_` ids),
and the leads it "launched" are READY again: nothing was sent to them.

`reconcile_campaign`, the Email Sync job's (`sync_job.py`), per campaign of
an agency bound to the team the platform key opens: READ ONLY toward the
provider. The campaign's state there (`provider_status`, its metadata
merged: Nidria's own keys such as `team_id` are kept), its leads (a pause
or resume done in Lemlist's app mirrored, an unsubscribe applied, a lead
absent from a complete listing flagged `missing`, never ended), the
activities since the checkpoint (overlapping it by an hour, by a day on the
first read of each UTC day), and the content of new replies from the
contact's inbox. It pushes nothing, launches nobody, pauses nothing at the
provider and generates no AI value; name, status, settings, steps and
contents stay Nidria's.

`ingest`, the ONE ingestion of the webhook, the job and the Sync: the same
normalized events, each stored once (`uq_email_provider_event`), its agency
the campaign's (rule N1), the webhook's `secret` never kept; then applied
- to the membership: SENT starts it and freezes that step's render, and
  the last step sent completes it; REPLIED, BOUNCED and UNSUBSCRIBED end
  the sequence; the last time of each kind. BOUNCED and UNSUBSCRIBED reach
  a removed membership too, its `removed_at` kept (decision A1, rule N6),
  nothing ever lifts them, and the person's address goes on the agency's
  opposition list (`email_suppression`, decision of 08/10/2026), as it does
  when the reconciliation reads an unsubscribed lead and when a Launch
  meets an address Lemlist's unsubscribe list holds;
- an event about a whole CONTACT (Lemlist's `entityUnsubscribed` and
  `variableUnsubscribed`, emitted instead of `emailsUnsubscribed` since
  30/03/2026) is filed even without a campaign or a lead: in the agency
  bound to its team, under the membership of its contact id or address. A
  contact's unsubscribe ends every live membership of that contact in the
  agency; a variable's is stored and counted, never applied (Lemlist does
  not document which variable it names);
- to the email messages: one OUTBOUND message per step (opens and clicks
  counted on it, the first fixing `first_*`), one INBOUND message per
  reply, whose text comes ONLY from the provider's inbox (`GET
  /inbox/{contactId}`), read afterwards by the job, or marked UNAVAILABLE
  after three reads without it.
A reply Lemlist attributes to a third party (`isThirdPartyReply`) matches
nothing and is ignored. No business status is written (no
`status_override`, nothing the derived prospect / client status reads), and
nothing in `activity_log` (rule N7).

An imported campaign (lot C, `email_import`) is only READ, by everything
here: its Sync is the reconciliation (`_sync_imported`), never a push; the
job and the webhook read it as any other. On it, an event's step is the one
Lemlist names (`stepId`), else its rank in the main sequence, and none for
a conditional branch (not imported); an email it sent has no render
(`PROVIDER_ONLY`). The import replays a campaign's whole history through
`ingest` in its `history` mode: events, messages, times and the steps sent
are kept, the opposition list is fed, but no status moves (the import set
each one from what Lemlist says now, and a block is never lifted). The job
and a Sync count its `unknown_leads`, and since Lemlist's listing proves a
pause (`state` paused) but never its end, they never resume an imported
lead from it (an event or the import does).

`webhook`: Lemlist's activity webhook (`POST /webhooks/lemlist`, PUBLIC):
the body's `secret` against EMAIL_WEBHOOK_SECRET (`hmac.compare_digest`),
then the same ingestion. It depends on the env only, never on an agency's
switch: what left converges.

Every reason, note and failure is a code with named params (decision A12):
see `email_manager`'s docstring for the `status_reason` codes, and
`email_schema.SyncNoteCode` / `SyncErrorCode` / `EmailLaunchFailure`.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.email_campaign import (
    BLOCKING_MEMBER_STATUSES,
    TERMINAL_MEMBER_STATUSES,
    EmailCampaign,
    EmailCampaignMember,
    EmailCampaignStep,
    EmailMemberRender,
    EmailMessage,
)
from src.core.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NidriaError,
    NotFoundError,
)
from src.core.rbac.permissions import Permission
from src.email_campaigns.email_manager import (
    IMPORT_METADATA_KEY,
    STATUS_REASON_PARAMS_KEY,
    Access,
    EmailManager,
    People,
    campaign_imported,
    campaign_ref,
    chosen_mailbox,
    import_metadata,
    is_imported,
    lead_ref,
    prompt_hash,
    provider_error_code,
    provider_params,
    provider_refusal,
    set_status_reason,
)
from src.email_campaigns.email_repository import MemberRow
from src.email_campaigns.email_schema import (
    EmailCampaignSettings,
    EmailLaunchFailure,
    EmailLaunchRequest,
    EmailLaunchResult,
    EmailSyncError,
    EmailSyncNote,
    EmailSyncRequest,
    EmailSyncResult,
    EmailWebhookAck,
)
from src.email_campaigns.flags import is_real_provider_id
from src.email_campaigns.html_text import html_to_text
from src.email_campaigns.lemlist_team import CAMPAIGN_TEAM_KEY
from src.email_campaigns.providers.base import (
    CampaignRef,
    CampaignSpec,
    EmailProvider,
    LeadSpec,
    ProviderError,
    ProviderEvent,
    ProviderLeadListing,
    ProviderMessage,
    ScheduleSpec,
    StepContent,
    StepSpec,
)
from src.email_campaigns.providers.dry_run import is_dry_run_id
from src.email_campaigns.providers.lemlist import CONTACT_ACTIVITY_TYPES, normalize_activity
from src.email_campaigns.readiness import (
    ARCHIVED,
    EXTERNAL_EFFECTS_OFF,
    NO_EMAIL,
    NO_STEPS,
    NOT_ACTIVE,
    TEAM_MISMATCH,
    TEMPLATE,
    Issue,
    campaign_gates,
    day_start,
    membership_issues,
    render_errors,
    template_issues,
)
from src.email_campaigns.readiness import team_mismatch as on_other_team
from src.email_campaigns.renderer import EmailTemplateRenderer, body_to_html, tokens_in
from src.email_campaigns.schedule import delay_days
from src.email_campaigns.suppression import REASON_OF_STATUS, email_hash
from src.email_campaigns.variables import client_identity

#: `status_reason` of a running lead the sync paused: its email for a step
#: Lemlist holds is not there yet. The sync resumes it once it is.
REASON_NEEDS_RENDER = "needs_render"
#: `status_reason` of a lead that completed the sequence before a follow-up
#: was added: paused at Lemlist so it never gets it.
REASON_KEPT_OUT = "kept_out"
#: Codes of a selected lead a launch does not take (`EmailLaunchResult.skipped`).
SKIP_NOT_IN_CAMPAIGN = "not_in_campaign"
SKIP_ALREADY_LAUNCHED = "already_launched"
SKIP_NOT_READY = "not_ready"
#: The `email.*` code of a launch Lemlist's plan refuses: the batch stops.
PLAN_REFUSED_CODE = "email.provider_plan_required"
#: `status_reason` codes the provider's events and the reconciliation write
#: (see `email_manager`'s docstring).
REASON_PAUSED_AT_PROVIDER = "paused_at_provider"
REASON_MISSING_AT_PROVIDER = "missing_at_provider"
REASON_UNSUBSCRIBED_AT_PROVIDER = "unsubscribed_at_provider"
REASON_BOUNCED = "bounced"
REASON_SEND_FAILED = "send_failed"
#: A lead absent from the provider's complete listing, and a campaign the
#: provider no longer has: their `provider_status`.
MISSING = "missing"
#: A reconciliation that failed on something other than the provider.
UNEXPECTED_CODE = "internal_error"

_RUNNING = ("EMAIL_QUEUED", "EMAIL_ACTIVE")
_TERMINAL = frozenset(TERMINAL_MEMBER_STATUSES)
_BLOCKING = frozenset(BLOCKING_MEMBER_STATUSES)
_UNSUBSCRIBED = "EMAIL_UNSUBSCRIBED"
#: Event types that put an address on the opposition list, and its reason.
_EVENT_REASON = {
    "UNSUBSCRIBED": REASON_OF_STATUS[_UNSUBSCRIBED],
    "BOUNCED": REASON_OF_STATUS["EMAIL_BOUNCED"],
}
_BLOCKING_EVENTS = frozenset(_EVENT_REASON)
#: A contact's variable unsubscribed (Lemlist's `variableUnsubscribed`):
#: stored and counted, never applied.
VARIABLE_UNSUBSCRIBED = "VARIABLE_UNSUBSCRIBED"
#: Activities are read again over this window behind the checkpoint, and
#: over a day on the first read of each UTC day: late-indexed activities are
#: not missed, and storing is idempotent (Prism's values).
ACTIVITY_OVERLAP = timedelta(hours=1)
DAILY_OVERLAP = timedelta(days=1)
#: Events about one step's email, aggregated on its OUTBOUND message.
_OUTBOUND_EVENTS = frozenset({"SENT", "OPENED", "CLICKED", "BOUNCED", "REPLIED"})
#: An outbound message keeps its furthest state (QUEUED is never stored).
_MESSAGE_RANK = {
    "QUEUED": 0,
    "SENT": 1,
    "DELIVERED": 2,
    "BOUNCED": 3,
    "OPENED": 4,
    "CLICKED": 5,
    "REPLIED": 6,
}
#: Replies whose content is read per campaign and per run, and how many
#: reads of an inbox that does not have it before it is UNAVAILABLE.
REPLIES_PER_RUN = 50
CONTENT_ATTEMPTS = 3
#: A reply found in the inbox under another id still matches its event when
#: they are this close in time.
REPLY_MATCH_WINDOW = timedelta(minutes=10)
#: A reply's HTML kept, at most (its text is capped by `html_text`: 50,000).
MAX_REPLY_HTML = 500_000
#: The provider's words kept in a `status_reason` param, at most.
_MAX_REASON = 500
#: The keys Nidria itself keeps in a provider metadata, never overwritten by
#: what the provider says (a membership's reason params, a campaign's team,
#: what an import noted on either).
_MEMBER_OWN_KEYS = (STATUS_REASON_PARAMS_KEY, IMPORT_METADATA_KEY)
_CAMPAIGN_OWN_KEYS = (CAMPAIGN_TEAM_KEY, IMPORT_METADATA_KEY)
#: An outbound email's content: Nidria's render, or (an imported campaign's)
#: Lemlist's own template, personalized there and never seen by Nidria.
_RENDERED = "RENDERED"
_PROVIDER_ONLY = "PROVIDER_ONLY"

logger = logging.getLogger(__name__)

Params = dict[str, str | int]
LogFn = Callable[[str], None]


def _scalars(params: Mapping[str, Any]) -> Params:
    """Params as the API carries them: strings and integers only."""
    return {
        key: value if isinstance(value, int | str) else str(value)
        for key, value in params.items()
        if value is not None
    }


@dataclass(frozen=True)
class PlanError:
    """Why one step's email cannot be rendered for one lead: the renderer's
    error kind (or the hand-over's own rules), the token it is about and,
    for an AI value, the `email.*` code of the failed generation."""

    position: int
    kind: str
    token: str | None = None
    error: str | None = None

    @property
    def params(self) -> Params:
        params: Params = {"position": self.position, "kind": self.kind}
        if self.token is not None:
            params["token"] = self.token
        if self.error is not None:
            params["error"] = self.error
        return params


@dataclass
class PlannedRender:
    step: EmailCampaignStep
    stored: EmailMemberRender | None
    subject: str
    body: str
    resolved: dict[str, str | None]
    ai_values: dict[str, Any]

    @property
    def already_pushed(self) -> bool:
        """The provider already holds exactly this email."""
        stored = self.stored
        return (
            stored is not None
            and stored.provider_synced_at is not None
            and stored.campaign_step_id == self.step.id
            and stored.template_hash == self.step.content_hash
            and stored.subject == self.subject
            and stored.body == self.body
        )


@dataclass
class MemberPlan:
    renders: list[PlannedRender] = field(default_factory=list)
    errors: list[PlanError] = field(default_factory=list)


@dataclass
class PushFailure:
    """Why a lead was not handed over: its new `status_reason` code."""

    code: str
    params: Params = field(default_factory=dict)
    #: Stop handing leads over in this launch (the plan refuses launches).
    stop: bool = False


@dataclass
class IngestStats:
    """What one ingestion did with the events it was given."""

    stored: int = 0
    duplicates: int = 0
    #: Not Nidria's: no campaign id, a third party's reply, a campaign Nidria
    #: does not run, or another campaign than the one being read; a
    #: contact's event no agency bound to its team can take.
    ignored: int = 0
    #: Stored for a known campaign (or a known team's contact) whose lead
    #: or contact matches no membership.
    unmatched: int = 0
    #: New events applied, per type.
    by_type: Counter[str] = field(default_factory=Counter)
    #: New `variableUnsubscribed` events: stored, never applied.
    variable_unsubscribed: int = 0
    #: Memberships whose pause state the provider changed: re-read from it.
    pause_changes: set[uuid.UUID] = field(default_factory=set)


@dataclass
class CampaignReconcile:
    """What the Email Sync job did for one campaign. `errors` are codes."""

    campaign_id: uuid.UUID
    contacts: int = 0
    events_new: int = 0
    events_duplicate: int = 0
    by_type: Counter[str] = field(default_factory=Counter)
    reply_contents: int = 0
    leads_missing: int = 0
    unsubscribed: int = 0
    #: A contact's variable unsubscribed: stored, never applied.
    variable_unsubscribed: int = 0
    pause_changes: int = 0
    #: An imported campaign: the leads Lemlist lists that Nidria does not
    #: know (None: not counted, the listing being incomplete).
    unknown_leads: int | None = None
    #: Nothing of this run was kept for the campaign (its checkpoint stays).
    failed: bool = False
    #: Not read at all: a campaign of another Lemlist team (read only, N4).
    skipped: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class AgencyReconcile:
    """What the Email Sync job did for one agency."""

    agency_id: uuid.UUID
    campaigns: list[CampaignReconcile] = field(default_factory=list)
    dry_run_campaigns: int = 0
    #: Why nothing of the agency was read (a code), if so.
    error: str | None = None


def activity_since(checkpoint: datetime | None, now: datetime) -> datetime | None:
    """Where to read a campaign's activities from: everything the first
    time, then the checkpoint minus the overlap: a day's on the first read
    of a UTC day, an hour's otherwise."""
    if checkpoint is None:
        return None
    first_today = checkpoint.astimezone(UTC).date() < now.astimezone(UTC).date()
    return checkpoint - (DAILY_OVERLAP if first_today else ACTIVITY_OVERLAP)


def _earlier(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate < current else current


def _later(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate > current else current


def _step_id_at(campaign: EmailCampaign, position: int) -> uuid.UUID | None:
    """The campaign's step at this position, if it has one."""
    return next((s.id for s in campaign.steps if s.position == position), None)


def _third_party(payload: Mapping[str, Any]) -> bool:
    """A reply Lemlist attributes to someone else than the lead: it belongs
    to no lead of Nidria's (spec 8.9)."""
    return payload.get("isThirdPartyReply") is True


def _contact_id(payload: Mapping[str, Any]) -> str | None:
    """The provider's contact id an event carries (Lemlist: `contactId`,
    "present on every activity, campaign or not")."""
    contact = payload.get("contactId")
    return contact if isinstance(contact, str) and contact else None


def _reason_params(reason: str | None) -> Params:
    return {"reason": reason[:_MAX_REASON]} if reason else {}


def imported_step_position(campaign: EmailCampaign, event: ProviderEvent) -> int | None:
    """Where an event of an imported campaign stands in the steps Nidria
    imported (Lemlist's main sequence): the step Lemlist names (`stepId`,
    stable, "present on every activity created from now on") when it is one
    of them; otherwise its rank (`sequenceStep` + 1, the adapter's
    position) only when it belongs to the main sequence (`sequenceId`
    absent or the main one): `sequenceStep` is relative to its own
    sequence, so an event of a conditional branch (not imported) has no
    step."""
    payload = event.payload
    step_id = payload.get("stepId")
    if isinstance(step_id, str) and step_id:
        for step in campaign.steps:
            if step.provider_step_id == step_id:
                return step.position
    sequence_id = payload.get("sequenceId")
    if (
        isinstance(sequence_id, str)
        and sequence_id
        and campaign.provider_sequence_id
        and sequence_id != campaign.provider_sequence_id
    ):
        return None
    return event.step_position


def merged_metadata(
    current: Mapping[str, Any] | None, said: Mapping[str, Any], own: Sequence[str]
) -> dict[str, Any]:
    """The provider's word on an object, with Nidria's own keys (`own`)
    kept as they are: what the provider says replaces what it said before,
    never what Nidria noted."""
    kept = {key: value for key, value in (current or {}).items() if key in own}
    return {**{k: v for k, v in said.items() if k not in own}, **kept}


def set_paused(member: EmailCampaignMember, paused: bool) -> bool:
    """Mirror the provider's pause state onto a membership still running;
    True when it changed. Nidria's own pause goes through the provider
    first, so the two only differ when someone paused or resumed in
    Lemlist's app. A pause seen there is no agent's."""
    if paused and member.status in ("EMAIL_READY", "EMAIL_QUEUED", "EMAIL_ACTIVE"):
        member.status = "EMAIL_PAUSED"
        member.paused_at = datetime.now(UTC)
        member.paused_by_agent_id = None
        set_status_reason(member, REASON_PAUSED_AT_PROVIDER)
        return True
    if not paused and member.status == "EMAIL_PAUSED":
        member.status = "EMAIL_ACTIVE" if member.started_at else "EMAIL_QUEUED"
        member.paused_at = None
        member.paused_by_agent_id = None
        set_status_reason(member, None)
        return True
    return False


def _content_missed(message: EmailMessage) -> None:
    message.content_attempts = (message.content_attempts or 0) + 1
    if message.content_attempts >= CONTENT_ATTEMPTS:
        message.content_state = "UNAVAILABLE"


def _matching_reply(
    message: EmailMessage, replies: Sequence[ProviderMessage], campaign_id: str | None
) -> ProviderMessage | None:
    """The inbox reply a REPLIED event announced: the same id (Lemlist gives
    the inbox message its activity's id), else the closest reply of the
    campaign received within minutes of it. A third party's reply never
    matches."""
    own = [r for r in replies if not _third_party(r.payload)]
    for reply in own:
        if reply.message_id == message.provider_message_id:
            return reply
    received = message.received_at
    if received is None:
        return None
    candidates = [
        r
        for r in own
        if r.campaign_id in (None, campaign_id)
        and abs(r.occurred_at - received) <= REPLY_MATCH_WINDOW
    ]
    return min(candidates, key=lambda r: abs(r.occurred_at - received), default=None)


def describe(report: CampaignReconcile) -> str:
    """One log line of the job for a campaign: ids, counts and codes only
    (no em dash, no sentence a translation would need)."""
    if report.skipped:
        return f"campaign {report.campaign_id}: skipped (another Lemlist team)"
    if report.failed:
        return f"campaign {report.campaign_id}: failed ({', '.join(report.errors) or 'error'})"
    counts = ", ".join(f"{k.lower()}={v}" for k, v in sorted(report.by_type.items())) or "none"
    line = (
        f"campaign {report.campaign_id}: {report.contacts} leads, {report.events_new} new "
        f"events ({counts}), {report.events_duplicate} already stored, "
        f"{report.reply_contents} reply contents"
    )
    if report.leads_missing:
        line += f", {report.leads_missing} missing at the provider"
    if report.unknown_leads:
        line += f", {report.unknown_leads} leads unknown to Nidria"
    if report.variable_unsubscribed:
        line += f", {report.variable_unsubscribed} variable unsubscribes (not applied)"
    if report.errors:
        line += f", errors: {', '.join(report.errors)}"
    return line


def campaign_spec(campaign: EmailCampaign) -> CampaignSpec:
    settings = EmailCampaignSettings.model_validate(campaign.settings or {})
    return CampaignSpec(
        name=campaign.name,
        schedule=ScheduleSpec(
            timezone=settings.timezone,
            weekdays=tuple(settings.sending_days),
            start=settings.sending_start_time,
            end=settings.sending_end_time,
        ),
        stop_on_reply=True,
        sender_ids=(settings.sender.user_id,) if settings.sender else (),
    )


def is_outdated(
    steps: Sequence[EmailCampaignStep],
    renders: Mapping[int, EmailMemberRender],
    passed: int = 0,
) -> bool:
    """Whether a running lead's next emails differ from what the provider
    has. Steps it has passed (`passed`: the last one sent) do not count."""
    for step in steps:
        if step.position <= passed:
            continue
        render = renders.get(step.position)
        if render is None:
            return True
        if render.sent_at is None and (
            render.provider_synced_at is None
            or render.campaign_step_id != step.id
            or render.template_hash != step.content_hash
        ):
            return True
    return False


def gate_error(gate: Issue, campaign: EmailCampaign) -> NidriaError:
    """A campaign gate (readiness) as the API answers it: what the Launch
    page shows, refused with its `email.*` code."""
    if gate.code == TEAM_MISMATCH:
        return ConflictError(
            "This campaign lives in another Lemlist team: it is read only.",
            code="email.team_mismatch",
        )
    if gate.code == ARCHIVED:
        return BadRequestError(
            "An archived campaign is neither synced nor launched.",
            code="email.campaign_archived",
        )
    if gate.code == NO_STEPS:
        return BadRequestError(
            "Add at least one step before syncing the campaign.", code="email.no_steps"
        )
    if gate.code == EXTERNAL_EFFECTS_OFF:
        return ConflictError(
            "This campaign exists at Lemlist and the agency runs in dry run: nothing changes "
            "until it talks to Lemlist again.",
            code="email.provider_unavailable",
            params={"reason": "external_effects_off"},
        )
    assert gate.code == NOT_ACTIVE, gate.code
    return ConflictError(
        "Activate the campaign to launch leads.",
        code="email.campaign_not_active",
        params={"status": campaign.status},
    )


def _full_name(row: MemberRow) -> str:
    identity = client_identity(row.profile, row.account)
    return " ".join(part for part in (identity.first_name, identity.last_name) if part)


class EmailSyncManager:
    """The Sync and the Launch of one campaign of the agent's agency. The
    campaign's row is locked for the whole run: a double click, two agents
    or a launch during a sync never push the same lead twice."""

    def __init__(self, db: AsyncSession, manager: EmailManager | None = None) -> None:
        self.db = db
        self.manager = manager or EmailManager(db)
        self.repo = self.manager.repo
        #: Why the campaign cannot run at the provider yet (a lead that
        #: could not be paused); reset on each run.
        self._unsafe: list[Params] = []

    # ── entry points ────────────────────────────────────────────────────

    async def sync_campaign(
        self, agent: Agent, campaign_id: Any, request: EmailSyncRequest
    ) -> EmailSyncResult:
        """Sync: the campaign and its steps at the provider, its events, the
        leads it already holds (their next emails, new steps, pauses). It
        never hands a new lead over: that is the Launch's alone."""
        return await self._run(agent, campaign_id, request.limit, None)

    async def launch(
        self, agent: Agent, campaign_id: Any, request: EmailLaunchRequest
    ) -> EmailLaunchResult:
        """Launch: the same sync, then READY leads handed over, the selected
        ones or the next eligible ones in order, within the batch and the
        campaign's daily limit. The only path by which a new lead reaches
        the provider; every lead is checked again by the shared rules."""
        result = await self._run(agent, campaign_id, request.limit, request)
        assert isinstance(result, EmailLaunchResult)
        return result

    async def pull_events(
        self, provider: EmailProvider, campaign: EmailCampaign, ref: CampaignRef
    ) -> IngestStats:
        """The campaign's events since its checkpoint (with the overlap),
        through the one ingestion; its own events only. The checkpoint moves
        only when the provider is real (a dry run reads nothing). Not
        committed: the caller's transaction holds it."""
        started = datetime.now(UTC)
        since = activity_since(campaign.last_activity_sync_at, started)
        events = await asyncio.to_thread(provider.sync_campaign, ref, since)
        stats = await self.ingest(
            provider.name, events, commit=False, campaign=campaign, provider=provider
        )
        if provider.external:
            campaign.last_activity_sync_at = started
        return stats

    # ── the provider's events: one ingestion (spec 8.9) ──────────────────

    async def ingest(
        self,
        provider_name: str,
        events: Sequence[ProviderEvent],
        *,
        commit: bool = True,
        campaign: EmailCampaign | None = None,
        provider: EmailProvider | None = None,
        history: bool = False,
        members: Mapping[str, EmailCampaignMember] | None = None,
    ) -> IngestStats:
        """Store and apply provider events, in time order, each once. An
        event of a campaign Nidria does not run is neither stored nor
        applied; with `campaign` (a campaign's own read), an event of any
        other campaign (another agency's included) is ignored as well.
        `provider`: the one that read them, to re-read a pause state; the
        webhook has none and asks the campaign's agency's own.

        `history`: an import replaying a campaign's past (`_apply`): no
        status moves, the record and the messages are filled. `members`:
        the campaign's memberships by lead id, already locked by the caller
        (an import's thousands of events then cost no lookup each).

        An event about a whole CONTACT (`CONTACT_ACTIVITY_TYPES`) is filed
        even without a campaign Nidria runs: in the campaign's agency when
        it names one, else in the agency being read, else in the agency
        bound to its Lemlist team (`teamId`); under the membership of its
        lead, else of its contact id, else of its address. A contact's
        unsubscribe ends every live membership of that contact or address
        in the agency (`_unsubscribe_contact`). A variable's unsubscribe is
        stored and counted, never applied (`ACTIVITY_TYPES`)."""
        stats = IngestStats()
        found: dict[str, EmailCampaign | None] = {}
        teams: dict[str, uuid.UUID | None] = {}
        for event in sorted(events, key=lambda e: e.occurred_at):
            if _third_party(event.payload):
                stats.ignored += 1
                continue
            whole_contact = event.provider_type in CONTACT_ACTIVITY_TYPES
            contact = _contact_id(event.payload)
            target: EmailCampaign | None = None
            if event.campaign_id:
                if campaign is not None:
                    if event.campaign_id != campaign.provider_campaign_id:
                        stats.ignored += 1
                        continue
                    target = campaign
                else:
                    if event.campaign_id not in found:
                        found[event.campaign_id] = await self.repo.campaign_by_provider_id(
                            provider_name, event.campaign_id
                        )
                    target = found[event.campaign_id]
            member: EmailCampaignMember | None
            if target is not None:
                agency_id = target.agency_id
                if is_imported(target):
                    event = replace(event, step_position=imported_step_position(target, event))
                member = (members or {}).get(event.lead_id or "")
                if member is None:
                    member = await self.repo.member_for_event(
                        target.id, event.lead_id, event.email, contact
                    )
            elif whole_contact:
                if campaign is not None:
                    agency_id = campaign.agency_id
                else:
                    team_agency = await self._team_agency(provider_name, event.payload, teams)
                    if team_agency is None:
                        stats.ignored += 1
                        continue
                    agency_id = team_agency
                member = await self.repo.member_for_contact(agency_id, contact, event.email)
            else:
                stats.ignored += 1
                continue
            # A contact's event filed under a membership is its campaign's.
            filed_under = target.id if target is not None else None
            if filed_under is None and member is not None:
                filed_under = member.campaign_id
            stored = await self.repo.store_event(
                {
                    "provider": provider_name,
                    "provider_event_id": event.event_id,
                    "agency_id": agency_id,
                    "campaign_id": filed_under,
                    "campaign_member_id": member.id if member is not None else None,
                    "type": event.type,
                    "provider_type": event.provider_type,
                    "step_position": event.step_position,
                    # The webhook's secret is never kept, whoever passes it.
                    "payload": {k: v for k, v in event.payload.items() if k != "secret"},
                    "occurred_at": event.occurred_at,
                }
            )
            if not stored:
                stats.duplicates += 1
                continue
            stats.stored += 1
            if member is None:
                stats.unmatched += 1
            if event.type == VARIABLE_UNSUBSCRIBED:
                # Lemlist does not document which variable it names: kept
                # and counted, nothing applied, nothing invented.
                stats.variable_unsubscribed += 1
                continue
            if member is not None:
                stats.by_type[event.type] += 1
                home = target
                if home is None:
                    # A contact's event: the campaign of the membership it is filed under.
                    if campaign is not None and member.campaign_id == campaign.id:
                        home = campaign
                    else:
                        home = await self.repo.campaign(agency_id, member.campaign_id)
                assert home is not None  # the membership's own campaign, same agency
                await self._apply(home, member, event, stats, history=history)
            elif event.type in _BLOCKING_EVENTS and event.email:
                # A lead of a campaign Nidria runs, or a contact of its
                # team, that no membership carries: its address is listed.
                await self._suppress(agency_id, event.email, _EVENT_REASON[event.type], None)
            if whole_contact and event.type == "UNSUBSCRIBED":
                await self._unsubscribe_contact(agency_id, contact, event.email, member)
        await self._reconcile_pauses(stats, provider)
        if commit:
            await self.db.commit()
        return stats

    async def _team_agency(
        self, provider_name: str, payload: Mapping[str, Any], cache: dict[str, uuid.UUID | None]
    ) -> uuid.UUID | None:
        """The agency bound to the Lemlist team an event names (`teamId`,
        documented on every activity), or None (no team said, or a team no
        agency is bound to)."""
        team = payload.get("teamId")
        if not isinstance(team, str) or not team:
            return None
        if team not in cache:
            owner = await self.repo.team_owner(provider_name, team)
            cache[team] = owner[0] if owner is not None else None
        return cache[team]

    async def _suppress(
        self,
        agency_id: uuid.UUID,
        address: str | None,
        reason: str,
        source_member_id: uuid.UUID | None,
    ) -> None:
        """One address on the agency's opposition list (decision of
        08/10/2026); nothing for no address."""
        digest = email_hash(address)
        if digest is not None:
            await self.repo.suppress(agency_id, digest, reason, source_member_id)

    async def _block(
        self, member: EmailCampaignMember, *also: str | None, reason: str | None = None
    ) -> None:
        """A membership just unsubscribed or bounced: the person's EFFECTIVE
        address (rule N2), and every address the provider named for it, go
        on its agency's opposition list with the reason its status says (or
        `reason`, for a past event an import replays)."""
        reason = reason or REASON_OF_STATUS.get(member.status)
        if reason is None:
            return
        addresses = [await self.repo.effective_email(member.client_profile_id), *also]
        for address in dict.fromkeys(a for a in addresses if a):
            await self._suppress(member.agency_id, address, reason, member.id)

    async def _unsubscribe_contact(
        self,
        agency_id: uuid.UUID,
        contact: str | None,
        address: str | None,
        member: EmailCampaignMember | None,
    ) -> None:
        """A whole contact unsubscribed at the provider (`entityUnsubscribed`:
        "unsubscribed from all communications"): every LIVE membership of
        that contact (its contact id) or of its addresses (the event's, and
        the effective address of the membership it was filed under) in the
        agency ends UNSUBSCRIBED, and each address goes on the opposition
        list. Ended memberships of other campaigns keep their history: the
        list blocks the person from now on."""
        addresses = [address]
        if member is not None:
            addresses.append(await self.repo.effective_email(member.client_profile_id))
        wanted = [a for a in dict.fromkeys(addresses) if a]
        digests = [d for d in (email_hash(a) for a in wanted) if d is not None]
        for other in await self.repo.live_members_of_contact(agency_id, contact, digests):
            if member is not None and other.id == member.id:
                continue
            other.status = _UNSUBSCRIBED
            set_status_reason(other, REASON_UNSUBSCRIBED_AT_PROVIDER)
            await self._block(other)
        for each in wanted:
            await self._suppress(
                agency_id,
                each,
                _EVENT_REASON["UNSUBSCRIBED"],
                member.id if member is not None else None,
            )

    async def _apply(
        self,
        campaign: EmailCampaign,
        member: EmailCampaignMember,
        event: ProviderEvent,
        stats: IngestStats,
        *,
        history: bool = False,
    ) -> None:
        """One new event on its membership and messages. Never a business
        status, never `activity_log` (rules N7, spec 8.9).

        `history` (an import's replay): the membership's status is what
        Lemlist says NOW, set by the import; a past event never moves it,
        so it neither contradicts that state nor lifts a block. It keeps
        the record (times, the steps sent, the first send) and the
        messages, and a past bounce or unsubscribe still puts the address
        on the opposition list (nothing ever lifts that)."""
        at = event.occurred_at
        if member.last_event_at is None or at >= member.last_event_at:
            member.last_event_at = at
            member.last_event_type = event.type
        contact = event.payload.get("contactId")
        if not member.provider_contact_id and isinstance(contact, str) and contact:
            # The inbox, where reply contents are, is keyed by it.
            member.provider_contact_id = contact
        if event.type == "SENT":
            member.last_sent_at = _later(member.last_sent_at, at)
        elif event.type == "OPENED":
            member.last_opened_at = _later(member.last_opened_at, at)
        elif event.type == "CLICKED":
            member.last_clicked_at = _later(member.last_clicked_at, at)
        elif event.type == "REPLIED":
            member.last_replied_at = _later(member.last_replied_at, at)
        render: EmailMemberRender | None = None
        if event.step_position is not None and not is_imported(campaign):
            # An imported campaign's emails have no render.
            render = (await self.repo.renders(member.id)).get(event.step_position)
        removed = member.removed_at is not None
        ended = removed or member.status in _TERMINAL

        if history:
            if event.type == "SENT" and not removed:
                member.started_at = _earlier(member.started_at, at)
                if event.step_position is not None:
                    member.current_step = max(member.current_step or 0, event.step_position)
            elif event.type in _BLOCKING_EVENTS:
                await self._block(member, event.email, reason=_EVENT_REASON[event.type])
            await self._record_message(campaign, member, event, render)
            return

        if event.type == "SENT":
            if render is not None and render.sent_at is None:
                render.sent_at = at
            if not ended:
                member.started_at = member.started_at or at
                if member.status in ("EMAIL_READY", "EMAIL_QUEUED"):
                    member.status = "EMAIL_ACTIVE"
                if event.step_position is not None:
                    member.current_step = max(member.current_step or 0, event.step_position)
                    # The last step went out: the sequence is over (a reply
                    # would still count).
                    if (
                        event.step_position >= len(campaign.steps)
                        and member.status == "EMAIL_ACTIVE"
                    ):
                        member.status = "EMAIL_COMPLETED"
        elif event.type == "REPLIED":
            # The provider stops the sequence on a reply (stopOnEmailReplied).
            # A removed membership stays removed, and a block is never lifted.
            if not removed and member.status not in _BLOCKING:
                member.status = "EMAIL_REPLIED"
                set_status_reason(member, None)
        elif event.type == "BOUNCED":
            # Decision A1: a removed membership takes it too, `removed_at`
            # kept. An unsubscribe outranks a bounce. The address goes on
            # the opposition list (decision of 08/10/2026).
            if member.status != _UNSUBSCRIBED:
                member.status = "EMAIL_BOUNCED"
            if not removed:
                set_status_reason(member, REASON_BOUNCED, _reason_params(event.reason))
            await self._block(member, event.email)
        elif event.type == "UNSUBSCRIBED":
            member.status = _UNSUBSCRIBED
            if not removed:
                set_status_reason(member, None)
            await self._block(member, event.email)
        elif event.type == "FAILED" and not ended:
            set_status_reason(member, REASON_SEND_FAILED, _reason_params(event.reason))
        elif event.type in ("PAUSED", "RESUMED") and not ended:
            # Arrival order says little about the current state: it is re-read.
            stats.pause_changes.add(member.id)

        await self._record_message(campaign, member, event, render)

    async def _record_message(
        self,
        campaign: EmailCampaign,
        member: EmailCampaignMember,
        event: ProviderEvent,
        render: EmailMemberRender | None,
    ) -> None:
        """The event on the email messages: its step's OUTBOUND message
        (created at its first event, its counts and times kept), and for a
        reply an INBOUND message whose content the job reads next."""
        at = event.occurred_at
        thread = member.provider_contact_id
        common: dict[str, Any] = {
            "agency_id": member.agency_id,
            "campaign_id": campaign.id,
            "campaign_member_id": member.id,
            "provider": campaign.provider,
            "provider_thread_id": thread,
            "type": "EMAIL",
            "open_count": 0,
            "click_count": 0,
            "content_attempts": 0,
        }
        if event.type in _OUTBOUND_EVENTS and event.step_position is not None:
            imported = is_imported(campaign)
            message = await self.repo.outbound_message(
                {
                    **common,
                    "step_position": event.step_position,
                    "campaign_step_id": (
                        render.campaign_step_id
                        if render
                        else _step_id_at(campaign, event.step_position)
                        if imported
                        else None
                    ),
                    "render_id": render.id if render else None,
                    "direction": "OUTBOUND",
                    "status": "SENT",
                    # Lemlist sent an imported campaign's emails from its own
                    # template: their personalized content is not in Nidria.
                    "content_state": _PROVIDER_ONLY if imported else _RENDERED,
                    "payload": {},
                }
            )
            if message.render_id is None and render is not None:
                message.render_id = render.id
                message.campaign_step_id = render.campaign_step_id
            if message.provider_thread_id is None and thread:
                message.provider_thread_id = thread
            if event.type == "SENT":
                message.sent_at = _earlier(message.sent_at, at)
                if message.provider_message_id is None:
                    message.provider_message_id = event.event_id
                if not message.payload:
                    message.payload = {k: v for k, v in event.payload.items() if k != "secret"}
            elif event.type == "OPENED":
                message.open_count += 1
                message.first_opened_at = _earlier(message.first_opened_at, at)
                message.last_opened_at = _later(message.last_opened_at, at)
            elif event.type == "CLICKED":
                message.click_count += 1
                message.last_clicked_at = _later(message.last_clicked_at, at)
            elif event.type == "BOUNCED":
                message.bounced_at = _earlier(message.bounced_at, at)
            elif event.type == "REPLIED":
                message.replied_at = _earlier(message.replied_at, at)
            if _MESSAGE_RANK[event.type] > _MESSAGE_RANK.get(message.status, 0):
                message.status = event.type
        if event.type == "REPLIED":
            subject = event.payload.get("subject")
            await self.repo.insert_message(
                {
                    **common,
                    "step_position": event.step_position,
                    "campaign_step_id": (
                        render.campaign_step_id
                        if render
                        else _step_id_at(campaign, event.step_position)
                        if is_imported(campaign) and event.step_position is not None
                        else None
                    ),
                    "provider_message_id": event.event_id,
                    "direction": "INBOUND",
                    "status": "REPLIED",
                    "subject": subject if isinstance(subject, str) else None,
                    "content_state": "PENDING",
                    "received_at": at,
                    "payload": {k: v for k, v in event.payload.items() if k != "secret"},
                }
            )

    async def _reconcile_pauses(self, stats: IngestStats, provider: EmailProvider | None) -> None:
        """Re-read from the provider the pause state of the memberships a
        PAUSED / RESUMED event touched. Only a real provider is asked: in a
        dry run, or when the agency's provider cannot be chosen, the state
        is left as it is (the job's listing or the next Sync mirrors it)."""
        chosen: dict[uuid.UUID, EmailProvider | None] = {}
        for member_id in sorted(stats.pause_changes):
            member = await self.repo.member_locked(member_id)
            if (
                member is None
                or not is_real_provider_id(member.provider_lead_id)
                or member.removed_at is not None
                or member.status in _TERMINAL
            ):
                continue
            reader = provider
            if reader is None:
                if member.agency_id not in chosen:
                    chosen[member.agency_id] = await self._agency_provider(member.agency_id)
                reader = chosen[member.agency_id]
            if reader is None or not reader.external:
                continue
            try:
                lead = await asyncio.to_thread(reader.get_contact, lead_ref(member))
            except ProviderError as exc:
                logger.warning("pause state of membership %s not re-read: %s", member_id, exc.code)
                continue
            if lead is not None:
                set_paused(member, lead.is_paused)

    async def _agency_provider(self, agency_id: uuid.UUID) -> EmailProvider | None:
        """The agency's provider for a read the webhook needs, or None when
        it cannot be chosen (Lemlist does not say which team the key opens)."""
        try:
            return await self.manager.provider_of(await self.repo.get_module(agency_id))
        except ConflictError:
            return None

    # ── the webhook (spec 7.2, 10.4) ─────────────────────────────────────

    async def webhook(self, provider_name: str, payload: Any) -> EmailWebhookAck:
        """Lemlist's activity webhook: refused (403 `email.webhook_forbidden`)
        unless EMAIL_WEBHOOK_SECRET is set and the body's `secret` matches
        it; then the one ingestion. Depends on the env only, never on an
        agency's switch: the campaign, and its agency, are found by the
        provider's campaign id, archived ones included."""
        expected = self.manager.settings.email_webhook_secret
        received = payload.get("secret") if isinstance(payload, dict) else None
        if (
            not expected
            or not isinstance(received, str)
            or not hmac.compare_digest(received.encode(), expected.encode())
        ):
            raise ForbiddenError("Webhook refused.", code="email.webhook_forbidden")
        assert isinstance(payload, dict)
        event = normalize_activity(payload)
        if event is None:
            return EmailWebhookAck(stored=False, duplicate=False, matched=False)
        stats = await self.ingest(provider_name, [event])
        return EmailWebhookAck(
            stored=stats.stored == 1,
            duplicate=stats.duplicates == 1,
            matched=stats.stored == 1 and stats.unmatched == 0,
        )

    # ── the Email Sync job's reconciliation (spec 10.3) ──────────────────

    async def reconcile_agency(
        self, agency_id: uuid.UUID, log: LogFn | None = None
    ) -> AgencyReconcile:
        """Every reconcilable campaign of the agency (ACTIVE or PAUSED, at
        Lemlist for real), one at a time, each in its own transaction: a
        failing campaign is reported and the next one goes on."""
        report = AgencyReconcile(agency_id)
        try:
            provider = await self.manager.provider_of(await self.repo.get_module(agency_id))
        except ConflictError as exc:
            await self.db.rollback()
            report.error = exc.code
            return report
        if not provider.external:
            # Unbound, switched off or another team since the job chose it.
            await self.db.rollback()
            report.error = "email.provider_unavailable"
            return report
        try:
            key_team = await asyncio.to_thread(provider.team_id)
        except ProviderError as exc:
            await self.db.rollback()
            report.error = provider_error_code(exc)
            return report
        report.dry_run_campaigns = await self.repo.dry_run_campaign_count(agency_id)
        campaign_ids = [c.id for c in await self.repo.reconcilable_campaigns(agency_id)]
        await self.db.rollback()
        for campaign_id in campaign_ids:
            try:
                done = await self.reconcile_campaign(
                    campaign_id, agency_id, provider=provider, key_team=key_team
                )
            except Exception:  # noqa: BLE001 (one campaign never stops the others)
                logger.exception("email sync of campaign %s failed", campaign_id)
                await self.db.rollback()
                done = CampaignReconcile(campaign_id, failed=True, errors=[UNEXPECTED_CODE])
                await self._record_failure(campaign_id, agency_id, UNEXPECTED_CODE)
            report.campaigns.append(done)
            if log is not None:
                log(describe(done))
        return report

    async def reconcile_campaign(
        self,
        campaign_id: uuid.UUID,
        agency_id: uuid.UUID,
        *,
        provider: EmailProvider,
        key_team: str | None,
    ) -> CampaignReconcile:
        """Bring one campaign of the agency in line with the provider, in
        one transaction: if any read fails, nothing of this run is kept for
        it, its checkpoint stays, and the next run reads the same window
        again. Another agency's campaign is not found (rule N1). Read only
        toward the provider: nothing is pushed, launched or paused there."""
        started = datetime.now(UTC)
        campaign = await self.repo.campaign(agency_id, campaign_id, lock=True)
        if campaign is None:
            raise NotFoundError("Campaign not found.", code="email.campaign_not_found")
        report = CampaignReconcile(campaign.id)
        ref = campaign_ref(campaign)
        if ref is None or is_dry_run_id(ref.campaign_id) or not provider.external:
            await self.db.rollback()
            return report
        if on_other_team(campaign, key_team):
            # Another team's campaign is history: its ids do not exist in the
            # key's team, and their absence there is no deletion.
            await self.db.rollback()
            report.skipped = True
            return report
        try:
            remote = await asyncio.to_thread(provider.read_campaign, ref)
            if not remote.exists:
                raise ProviderError("Lemlist no longer has this campaign.", code="CAMPAIGN_MISSING")
            if campaign.provider_status != remote.status:
                campaign.provider_status = remote.status
            merged = merged_metadata(
                campaign.provider_metadata, remote.metadata, _CAMPAIGN_OWN_KEYS
            )
            if campaign.provider_metadata != merged:
                campaign.provider_metadata = merged
            listing = await asyncio.to_thread(provider.list_contacts, ref)
            await self._reconcile_leads(campaign, listing, report)
            since = activity_since(campaign.last_activity_sync_at, started)
            events = await asyncio.to_thread(provider.sync_campaign, ref, since)
            stats = await self.ingest(
                provider.name, events, commit=False, campaign=campaign, provider=provider
            )
        except ProviderError as exc:
            await self.db.rollback()
            # CAMPAIGN_MISSING: `email.provider_campaign_missing`.
            code = provider_error_code(exc)
            report.failed = True
            report.errors.append(code)
            fresh = await self.repo.campaign(agency_id, campaign_id, lock=True)
            if fresh is not None:
                fresh.last_provider_error = code
                if exc.code == "CAMPAIGN_MISSING":
                    fresh.provider_status = MISSING
            await self.db.commit()
            return report
        report.events_new = stats.stored
        report.events_duplicate = stats.duplicates
        report.by_type = stats.by_type
        report.variable_unsubscribed = stats.variable_unsubscribed
        campaign.last_activity_sync_at = started
        await self._read_replies(provider, campaign, report)
        campaign.last_provider_sync_at = datetime.now(UTC)
        campaign.last_provider_error = report.errors[0] if report.errors else None
        await self.db.commit()
        return report

    async def _reconcile_leads(
        self, campaign: EmailCampaign, listing: ProviderLeadListing, report: CampaignReconcile
    ) -> None:
        """The provider's word on each lead it holds: its raw state kept
        (Nidria's own keys untouched), a pause or resume done in Lemlist's
        app mirrored, an unsubscribe applied. A lead it no longer lists is
        flagged, never ended: absence is not a removal, and it counts only
        from a complete listing that is not empty while leads are held.

        An imported campaign: its leads are only ever paused from the
        listing (`state` paused proves a pause; no state proves its end, and
        the import set the status from Lemlist's own `status`), and the
        leads Lemlist lists that Nidria does not know are counted
        (`unknown_leads`)."""
        imported = is_imported(campaign)
        members = await self.repo.at_provider(campaign.id)
        report.contacts = len(members)
        remote = {lead.lead_id: lead for lead in listing.leads}
        absence_counts = listing.complete and (bool(remote) or not members)
        if imported:
            report.unknown_leads = await self.count_unknown_leads(campaign, listing)
        for member in members:
            lead = remote.get(member.provider_lead_id or "")
            if lead is None:
                if (
                    absence_counts
                    and member.status not in _TERMINAL
                    and member.provider_status != MISSING
                ):
                    member.provider_status = MISSING
                    set_status_reason(member, REASON_MISSING_AT_PROVIDER)
                    report.leads_missing += 1
                continue
            if member.provider_status != lead.state:
                member.provider_status = lead.state
            merged = merged_metadata(member.provider_metadata, lead.raw, _MEMBER_OWN_KEYS)
            if member.provider_metadata != merged:
                member.provider_metadata = merged
            if member.status_reason == REASON_MISSING_AT_PROVIDER:
                set_status_reason(member, None)
            if not member.provider_contact_id and lead.contact_id:
                member.provider_contact_id = lead.contact_id
            if member.status in _TERMINAL:
                continue
            if lead.unsubscribed:
                member.status = _UNSUBSCRIBED
                set_status_reason(member, REASON_UNSUBSCRIBED_AT_PROVIDER)
                await self._block(member, lead.email)
                report.unsubscribed += 1
            elif imported and not lead.is_paused:
                continue
            elif set_paused(member, lead.is_paused):
                report.pause_changes += 1

    async def count_unknown_leads(
        self, campaign: EmailCampaign, listing: ProviderLeadListing
    ) -> int | None:
        """The leads of an imported campaign that Lemlist lists and Nidria
        does not know: no membership of the campaign holds them and the
        last import did not set them aside. Kept on the campaign (its
        import metadata) with when it was counted; None (not counted) from
        an incomplete listing. Writes the campaign, never commits."""
        count: int | None = None
        noted = import_metadata(campaign.provider_metadata)
        if listing.complete:
            known = await self.repo.lead_ids_of_campaign(campaign.id)
            aside = {str(lead_id) for lead_id in noted.get("set_aside") or []}
            count = len({lead.lead_id for lead in listing.leads} - known - aside)
        noted.update(unknown_leads=count, unknown_leads_at=datetime.now(UTC).isoformat())
        campaign.provider_metadata = {
            **(campaign.provider_metadata or {}),
            IMPORT_METADATA_KEY: noted,
        }
        return count

    async def _read_replies(
        self, provider: EmailProvider, campaign: EmailCampaign, report: CampaignReconcile
    ) -> None:
        """The text of new replies, from each contact's inbox thread ONLY
        (`GET /inbox/{contactId}`), at most `REPLIES_PER_RUN` per run. A
        failing read is retried next run; a reply the inbox does not have
        after `CONTENT_ATTEMPTS` reads is UNAVAILABLE: its REPLIED stands."""
        pending = await self.repo.pending_replies(campaign.id, REPLIES_PER_RUN)
        by_member: dict[uuid.UUID, list[EmailMessage]] = {}
        for message in pending:
            by_member.setdefault(message.campaign_member_id, []).append(message)
        for member_id, messages in by_member.items():
            member = await self.db.get(EmailCampaignMember, member_id)
            if member is None or not member.provider_contact_id:
                for message in messages:
                    _content_missed(message)
                continue
            try:
                thread = await asyncio.to_thread(provider.get_messages, lead_ref(member))
            except ProviderError as exc:
                report.errors.append(provider_error_code(exc))
                continue
            replies = [m for m in thread if m.direction == "INBOUND"]
            for message in messages:
                found = _matching_reply(message, replies, campaign.provider_campaign_id)
                if found is None or not found.body_html:
                    _content_missed(message)
                    continue
                message.body_html = found.body_html[:MAX_REPLY_HTML]
                message.body_text = html_to_text(found.body_html)
                message.subject = message.subject or found.subject
                message.content_state = "AVAILABLE"
                message.payload = {**(message.payload or {}), "inbox": found.payload}
                report.reply_contents += 1

    async def _record_failure(
        self, campaign_id: uuid.UUID, agency_id: uuid.UUID, code: str
    ) -> None:
        try:
            campaign = await self.repo.campaign(agency_id, campaign_id)
            if campaign is not None:
                campaign.last_provider_error = code
            await self.db.commit()
        except Exception:  # noqa: BLE001 (the report already carries the error)
            logger.exception("could not record the sync error of campaign %s", campaign_id)
            await self.db.rollback()

    # ── the run ─────────────────────────────────────────────────────────

    async def _run(
        self,
        agent: Agent,
        campaign_id: Any,
        limit: int,
        launch: EmailLaunchRequest | None,
    ) -> EmailSyncResult:
        # What reaches the provider: its own permission, not the editing one.
        access = await self.manager.access(agent, Permission.EMAIL_SYNC)
        try:
            return await self._sync(access, campaign_id, limit, launch)
        except BaseException:
            await self.db.rollback()
            raise

    async def _sync(
        self,
        access: Access,
        campaign_id: Any,
        limit: int,
        launch: EmailLaunchRequest | None,
    ) -> EmailSyncResult:
        campaign = await self.manager.campaign_for(access, campaign_id, lock=True)
        if is_imported(campaign):
            return await self._sync_imported(access, campaign, launch)
        steps = sorted(campaign.steps, key=lambda s: s.position)
        provider = await self.manager.provider(access)
        # The campaign's gates (readiness): the same the Launch page shows.
        for gate in campaign_gates(
            campaign,
            steps,
            external=provider.external,
            key_team_id=await self.manager.key_team_id(access),
            launching=launch is not None,
        ):
            if gate.blocking:
                raise gate_error(gate, campaign)
        settings = EmailCampaignSettings.model_validate(campaign.settings or {})
        result: EmailSyncResult = (EmailLaunchResult if launch is not None else EmailSyncResult)(
            campaign_id=campaign.id, external=provider.external, daily_limit=settings.daily_limit
        )
        if provider.external:
            await self._forget_dry_run(campaign, steps)
        # Once the provider holds a lead, its sequence only grows at the end.
        in_use = (await self.manager.sequence_facts(campaign.id)).in_use
        self._unsafe = []

        try:
            ref = await self._push_campaign(provider, campaign, steps, result, in_use=in_use)
        except ProviderError as exc:
            campaign.sync_error = provider_error_code(exc)
            await self.db.commit()
            raise provider_refusal(exc) from exc

        try:
            result.events_ingested = (await self.pull_events(provider, campaign, ref)).stored
        except ProviderError as exc:
            # The next sync (or the job, or the webhook) reads them again.
            result.errors.append(
                EmailSyncError(code="events_not_read", params={"code": provider_error_code(exc)})
            )

        if in_use:
            await self._append_steps(access, provider, campaign, steps, ref, limit, result)

        refresh_failed = await self._refresh_in_flight(
            access, provider, campaign, steps, ref, limit, result
        )

        result.pushed_today = await self.repo.queued_since(
            campaign.id, day_start(settings.timezone)
        )
        allowance = max(settings.daily_limit - result.pushed_today, 0)
        if launch is not None:
            assert isinstance(result, EmailLaunchResult)
            await self._launch_batch(
                access, provider, campaign, steps, ref, launch, min(limit, allowance), result
            )
            if result.remaining and result.pushed_today >= settings.daily_limit:
                result.notes.append(
                    EmailSyncNote(
                        code="daily_limit_reached",
                        params={
                            "launched_today": result.pushed_today,
                            "daily_limit": settings.daily_limit,
                        },
                    )
                )
        else:
            _, waiting = await self.repo.ready_to_push(access.agency_id, campaign.id, 0)
            result.remaining = waiting
            if waiting:
                # A sync never hands a new lead over: they wait for a Launch.
                result.notes.append(
                    EmailSyncNote(code="ready_to_launch", params={"count": waiting})
                )

        if in_use and campaign.status == "ACTIVE":
            await self._run_if_safe(provider, campaign, steps, ref, result)

        now = datetime.now(UTC)
        campaign.last_synced_at = now
        if result.updates_remaining == 0 and refresh_failed == 0:
            for step in steps:
                step.provider_synced_hash = step.content_hash
        campaign.sync_error = result.errors[0].code if result.errors else None
        await self.db.commit()
        result.last_synced_at = now
        return result

    async def _sync_imported(
        self, access: Access, campaign: EmailCampaign, launch: EmailLaunchRequest | None
    ) -> EmailSyncResult:
        """The Sync of an imported campaign: READ ONLY at Lemlist, the job's
        own reconciliation (`reconcile_campaign`): the campaign's state there,
        its leads (a pause mirrored, an unsubscribe applied, a lead gone
        flagged, the unknown ones counted), its events since the checkpoint
        and the text of its new replies. Never a campaign or schedule PATCH,
        a step, a personalization, a sender, an added lead nor a launch: the
        Launch itself is refused (409 `email.campaign_imported`)."""
        if launch is not None:
            raise campaign_imported()
        provider = await self.manager.provider(access)
        key_team = await self.manager.key_team_id(access)
        for gate in campaign_gates(
            campaign,
            list(campaign.steps),
            external=provider.external,
            key_team_id=key_team,
            launching=False,
        ):
            # Lemlist's steps are Lemlist's: a campaign without any is read too.
            if gate.blocking and gate.code != NO_STEPS:
                raise gate_error(gate, campaign)
        campaign_id = campaign.id
        report = await self.reconcile_campaign(
            campaign_id, access.agency_id, provider=provider, key_team=key_team
        )
        result = EmailSyncResult(
            campaign_id=campaign_id,
            external=provider.external,
            events_ingested=report.events_new,
            unknown_leads=report.unknown_leads,
            errors=[
                EmailSyncError(code="events_not_read", params={"code": code})
                for code in report.errors
            ],
        )
        fresh = await self.repo.campaign(access.agency_id, campaign_id, lock=True)
        now = datetime.now(UTC)
        if fresh is not None:
            fresh.last_synced_at = now
            fresh.sync_error = result.errors[0].code if result.errors else None
        await self.db.commit()
        result.last_synced_at = now
        return result

    # ── the campaign and its steps ──────────────────────────────────────

    async def _forget_dry_run(
        self, campaign: EmailCampaign, steps: Sequence[EmailCampaignStep]
    ) -> None:
        """Real effects turned on: what the dry run "created" never existed
        at the provider. Its ids are dropped, and the leads it "launched"
        are ready again (nothing was sent to them); a lead an agent paused
        stays paused."""
        if is_dry_run_id(campaign.provider_campaign_id):
            campaign.provider_campaign_id = None
            campaign.provider_sequence_id = None
            campaign.provider_schedule_id = None
            campaign.last_activity_sync_at = None
        for step in steps:
            if is_dry_run_id(step.provider_step_id) or campaign.provider_campaign_id is None:
                step.provider_step_id = None
                step.provider_synced_hash = None
        members = await self.repo.with_provider_ids(campaign.id)
        dry = [m for m in members if is_dry_run_id(m.provider_lead_id)]
        renders = await self.repo.renders_for([m.id for m in dry])
        for member in dry:
            member.provider_lead_id = None
            member.provider_contact_id = None
            member.queued_at = None
            if member.status in ("EMAIL_QUEUED", "EMAIL_ACTIVE", "EMAIL_PAUSED"):
                if member.status == "EMAIL_PAUSED" and member.paused_by_agent_id is not None:
                    set_status_reason(member, None)
                else:
                    member.status = "EMAIL_READY"
                    member.paused_at = None
                    set_status_reason(member, None)
            for render in renders.get(member.id, {}).values():
                if render.sent_at is None:
                    render.provider_synced_at = None
        await self.db.flush()

    async def _push_campaign(
        self,
        provider: EmailProvider,
        campaign: EmailCampaign,
        steps: Sequence[EmailCampaignStep],
        result: EmailSyncResult,
        *,
        in_use: bool,
    ) -> CampaignRef:
        """The campaign and its steps at the provider. A sequence in use only
        has its existing steps updated here: a new one is added by
        `_append_steps`, and the campaign starts at the end of the sync."""
        spec = campaign_spec(campaign)
        ref = campaign_ref(campaign)
        if ref is not None:
            await asyncio.to_thread(provider.update_campaign, ref, spec)
        else:
            team: str | None = None
            if provider.external:
                # Which team it will live in, read BEFORE it exists there: a
                # campaign of an unknown team would be read only for good
                # (TEAM_MISMATCH), so none is created then (decision A4).
                team = await asyncio.to_thread(provider.team_id)
                if not team:
                    raise ProviderError(
                        "Lemlist did not say which team the key opens.", code="TEAM_UNKNOWN"
                    )
            ref = await asyncio.to_thread(provider.create_campaign, spec)
            campaign.provider_campaign_id = ref.campaign_id
            campaign.provider_sequence_id = ref.sequence_id
            campaign.provider_schedule_id = ref.schedule_id
            if team:
                campaign.provider_metadata = {
                    **(campaign.provider_metadata or {}),
                    CAMPAIGN_TEAM_KEY: team,
                }
            result.campaign_created = True
            await self.db.flush()
            if campaign.status != "ACTIVE":
                # However the provider creates it, it mirrors Nidria from the start.
                await asyncio.to_thread(provider.pause_campaign, ref)
        spare: list[str] | None = None
        for step in steps:
            spec_step = StepSpec(
                position=step.position, name=step.name, delay_days=delay_days(step)
            )
            if step.provider_step_id:
                await asyncio.to_thread(provider.update_step, ref, step.provider_step_id, spec_step)
                result.steps_updated += 1
            elif not in_use:
                if spare is None:
                    spare = (
                        []
                        if result.campaign_created
                        else await self._spare_steps(provider, ref, steps)
                    )
                await self._place_step(provider, ref, step, spec_step, spare, result)
        if in_use:
            return ref
        if campaign.status == "ACTIVE":
            # On every sync, once its steps exist: starting gives the campaign
            # its sender when it has none, and does nothing when it sends.
            await asyncio.to_thread(provider.start_campaign, ref, mailbox=chosen_mailbox(campaign))
        return ref

    async def _spare_steps(
        self, provider: EmailProvider, ref: CampaignRef, steps: Sequence[EmailCampaignStep]
    ) -> list[str]:
        """Steps the provider has after the last one Nidria knows (one a lost
        answer left behind, or one Nidria parked): the next steps added
        reuse them instead of adding more."""
        known = {s.provider_step_id for s in steps if s.provider_step_id}
        listed = await asyncio.to_thread(provider.read_steps, ref)
        last_known = max((p.position for p in listed if p.step_id in known), default=0)
        return [p.step_id for p in listed if p.position > last_known and p.step_id not in known]

    async def _place_step(
        self,
        provider: EmailProvider,
        ref: CampaignRef,
        step: EmailCampaignStep,
        spec: StepSpec,
        spare: list[str],
        result: EmailSyncResult,
    ) -> None:
        """The step at the end of the provider's sequence: a spare one,
        given this step's delay and the placeholder, or a new one."""
        if spare:
            reused = spare.pop(0)
            await asyncio.to_thread(provider.update_step, ref, reused, spec)
            step.provider_step_id = reused
            result.steps_updated += 1
        else:
            step.provider_step_id = await asyncio.to_thread(provider.create_step, ref, spec)
            result.steps_created += 1
        await self.db.flush()

    # ── a sequence in use (spec 8.2) ────────────────────────────────────

    async def _append_steps(
        self,
        access: Access,
        provider: EmailProvider,
        campaign: EmailCampaign,
        steps: Sequence[EmailCampaignStep],
        ref: CampaignRef,
        limit: int,
        result: EmailSyncResult,
    ) -> None:
        """Steps added to a sequence in use, put at the end of the provider's
        (the only place Lemlist accepts), each running lead's own email
        first, while the campaign is paused there (`_run_if_safe` starts it
        again)."""
        pending = sorted((s for s in steps if not s.provider_step_id), key=lambda s: s.position)
        if not pending:
            return
        unfinished = [s for s in pending if not s.body_template.strip()]
        if unfinished:
            result.notes.append(
                EmailSyncNote(code="step_not_written", params={"position": unfinished[0].position})
            )
            return
        try:
            if campaign.status == "ACTIVE":
                await asyncio.to_thread(provider.pause_campaign, ref)
            spare = await self._spare_steps(provider, ref, steps)
            await self._hold_completed(provider, campaign, ref, result)
            for step in pending:
                spec = StepSpec(position=step.position, name=step.name, delay_days=delay_days(step))
                await self._place_step(provider, ref, step, spec, spare, result)
                await self._render_new_step(access, provider, campaign, step, ref, limit, result)
        except ProviderError as exc:
            result.errors.append(
                EmailSyncError(
                    code="follow_up_not_added", params={"code": provider_error_code(exc)}
                )
            )
            return
        result.notes.append(EmailSyncNote(code="follow_up_added"))

    async def _hold_completed(
        self,
        provider: EmailProvider,
        campaign: EmailCampaign,
        ref: CampaignRef,
        result: EmailSyncResult,
    ) -> None:
        """Leads that completed the sequence never get a step added after: a
        lead Lemlist still has in progress is paused there first. Lemlist's
        own word is read for the leads it already holds."""
        members = await self.repo.completed_with_leads(campaign.id)
        if not members:
            return
        listing = await asyncio.to_thread(provider.list_contacts, ref)
        known = {lead.lead_id: lead for lead in listing.leads}
        for member in members:
            lead = known.get(member.provider_lead_id or "")
            if lead is not None and (lead.is_paused or lead.raw.get("status") == "done"):
                continue
            try:
                await asyncio.to_thread(provider.pause_contact, ref, lead_ref(member))
            except ProviderError as exc:
                self._unsafe.append({"reason": "lead_not_paused", "code": provider_error_code(exc)})
                continue
            set_status_reason(member, REASON_KEPT_OUT)
            result.members_held += 1

    async def _render_new_step(
        self,
        access: Access,
        provider: EmailProvider,
        campaign: EmailCampaign,
        step: EmailCampaignStep,
        ref: CampaignRef,
        limit: int,
        result: EmailSyncResult,
    ) -> None:
        """Each running lead not past the new step gets its own email for
        it, AI values included. One whose email cannot be rendered or
        pushed, or beyond this call's batch, is paused until a sync has it."""
        rows = [
            row
            for row in await self.repo.in_flight(access.agency_id, campaign.id)
            if (row.member.current_step or 0) < step.position
        ]
        batch, later = rows[:limit], rows[limit:]
        renders = await self.repo.renders_for([row.member.id for row in batch])
        people = await self.manager.people(access, [row.profile for row in batch])
        now = datetime.now(UTC)
        for row in batch:
            member = row.member
            plan = await self._plan(access, people, [step], row, renders.get(member.id, {}))
            problem: Params | None = plan.errors[0].params if plan.errors else None
            if problem is None and plan.renders:
                planned = plan.renders[0]
                try:
                    accepted = await asyncio.to_thread(
                        provider.set_step_content,
                        ref,
                        lead_ref(member),
                        step.provider_step_id or "",
                        StepContent(subject=planned.subject, html=body_to_html(planned.body)),
                    )
                except ProviderError as exc:
                    problem = {"position": step.position, "code": provider_error_code(exc)}
                else:
                    if accepted:
                        self._write(member, planned, now)
                        result.members_updated += 1
                    else:
                        # The provider says the lead is past it already.
                        member.current_step = max(member.current_step or 0, step.position)
                    continue
            if problem is not None:
                await self._hold(provider, ref, member, problem, result)
        for row in later:
            await self._hold(
                provider,
                ref,
                row.member,
                {"position": step.position, "reason": "next_sync"},
                result,
            )

    async def _hold(
        self,
        provider: EmailProvider,
        ref: CampaignRef,
        member: EmailCampaignMember,
        params: Params,
        result: EmailSyncResult,
    ) -> None:
        """Pause a running lead whose email for a step is not at the
        provider: Lemlist would send it the step's placeholder. A pause is a
        system one (no agent); a lead already paused, by an agent or not,
        stays as it is."""
        if member.status == "EMAIL_PAUSED":
            return
        try:
            await asyncio.to_thread(provider.pause_contact, ref, lead_ref(member))
        except ProviderError as exc:
            self._unsafe.append({"reason": "lead_not_paused", "code": provider_error_code(exc)})
            return
        member.status = "EMAIL_PAUSED"
        member.paused_at = datetime.now(UTC)
        member.paused_by_agent_id = None
        set_status_reason(member, REASON_NEEDS_RENDER, params)
        result.members_held += 1

    async def _run_if_safe(
        self,
        provider: EmailProvider,
        campaign: EmailCampaign,
        steps: Sequence[EmailCampaignStep],
        ref: CampaignRef,
        result: EmailSyncResult,
    ) -> None:
        """Start the campaign at the provider only if no running lead can
        reach a step without its own email there; otherwise keep it paused,
        and say so."""
        exposed = await self._exposed(campaign, steps)
        if exposed or self._unsafe:
            params: Params = {"exposed": exposed} if exposed else dict(self._unsafe[0])
            try:
                await asyncio.to_thread(provider.pause_campaign, ref)
            except ProviderError as exc:
                params["pause_code"] = provider_error_code(exc)
            result.errors.append(EmailSyncError(code="campaign_kept_paused", params=params))
            return
        try:
            await asyncio.to_thread(provider.start_campaign, ref, mailbox=chosen_mailbox(campaign))
        except ProviderError as exc:
            result.errors.append(
                EmailSyncError(
                    code="campaign_not_started", params={"code": provider_error_code(exc)}
                )
            )

    async def _exposed(self, campaign: EmailCampaign, steps: Sequence[EmailCampaignStep]) -> int:
        """Running (not paused) leads missing their email for a step at the
        provider they can still reach."""
        rows = [
            row
            for row in await self.repo.in_flight(campaign.agency_id, campaign.id)
            if row.member.status in _RUNNING
        ]
        renders = await self.repo.renders_for([row.member.id for row in rows])
        at_provider = [s for s in steps if s.provider_step_id]
        count = 0
        for row in rows:
            member = row.member
            held = renders.get(member.id, {})
            for step in at_provider:
                if step.position <= (member.current_step or 0):
                    continue
                render = held.get(step.position)
                if (
                    render is None
                    or render.provider_synced_at is None
                    or render.campaign_step_id != step.id
                ):
                    count += 1
                    break
        return count

    # ── the leads already at the provider ───────────────────────────────

    async def _refresh_in_flight(
        self,
        access: Access,
        provider: EmailProvider,
        campaign: EmailCampaign,
        steps: Sequence[EmailCampaignStep],
        ref: CampaignRef,
        limit: int,
        result: EmailSyncResult,
    ) -> int:
        """Refresh a batch of running leads whose next emails changed;
        returns how many could not be. Nothing new is added: only the leads
        the provider already holds."""
        rows = await self.repo.in_flight(access.agency_id, campaign.id)
        if not rows:
            return 0
        renders = await self.repo.renders_for([row.member.id for row in rows])
        # Only steps at the provider: a step not added there yet reaches nobody.
        live_steps = [s for s in steps if s.provider_step_id]
        outdated = [
            row
            for row in rows
            if is_outdated(live_steps, renders.get(row.member.id, {}), row.member.current_step or 0)
        ]
        batch = outdated[:limit]
        result.updates_remaining = len(outdated) - len(batch)
        if not batch:
            return 0
        people = await self.manager.people(access, [row.profile for row in batch])
        failed = 0
        for row in batch:
            member = row.member
            who: Params = {"member_id": str(member.id), "client_profile_id": str(row.profile.id)}
            plan = await self._plan(
                access,
                people,
                live_steps,
                row,
                renders.get(member.id, {}),
                passed=member.current_step or 0,
            )
            if plan.errors:
                failed += 1
                result.errors.append(
                    EmailSyncError(
                        code="member_not_updated", params={**who, **plan.errors[0].params}
                    )
                )
                continue
            now = datetime.now(UTC)
            try:
                for planned in plan.renders:
                    if planned.already_pushed or not planned.step.provider_step_id:
                        continue
                    accepted = await asyncio.to_thread(
                        provider.set_step_content,
                        ref,
                        lead_ref(member),
                        planned.step.provider_step_id,
                        StepContent(subject=planned.subject, html=body_to_html(planned.body)),
                    )
                    # Refused: the provider reached that step already. The
                    # stored render stays the email it actually has.
                    if accepted:
                        self._write(member, planned, now)
                    else:
                        member.current_step = max(member.current_step or 0, planned.step.position)
                if member.status == "EMAIL_PAUSED" and member.status_reason == REASON_NEEDS_RENDER:
                    # Paused by the sync for a missing email: it has all of them now.
                    await asyncio.to_thread(provider.resume_contact, ref, lead_ref(member))
                    member.status = "EMAIL_ACTIVE" if member.started_at else "EMAIL_QUEUED"
                    member.paused_at = None
                    set_status_reason(member, None)
            except ProviderError as exc:
                failed += 1
                result.errors.append(
                    EmailSyncError(
                        code="member_not_updated",
                        params={**who, "code": provider_error_code(exc)},
                    )
                )
                continue
            result.members_updated += 1
        return failed

    # ── rendering for the provider ──────────────────────────────────────

    async def _ai_values(
        self,
        access: Access,
        people: People,
        row: MemberRow,
        step: EmailCampaignStep,
        stored: EmailMemberRender | None,
    ) -> tuple[dict[str, str], dict[str, Any], list[PlanError]]:
        """This step's AI values for this lead: the stored ones while their
        prompt is unchanged, else generated now (`generate_ai_values`, phase
        5's seam). Only the variables the step's subject or body uses: one
        it does not use writes nothing of the email. A value not generated
        is an error, never a fallback. (values, their stored form, errors)."""
        prior = (
            stored.ai_values if stored is not None and isinstance(stored.ai_values, dict) else {}
        )
        values: dict[str, str] = {}
        kept: dict[str, Any] = {}
        missing: list[tuple[str, str, str | None]] = []
        used = {
            token.removeprefix("ai.")
            for token in tokens_in(step.subject_template, step.body_template)
            if token.startswith("ai.")
        }
        for variable in step.ai_variables:
            if variable.key not in used:
                continue
            digest = prompt_hash(variable.prompt_template, variable.model)
            entry = prior.get(variable.key)
            if (
                isinstance(entry, dict)
                and entry.get("prompt_hash") == digest
                and entry.get("value")
            ):
                values[variable.key] = str(entry["value"])
                kept[variable.key] = {"value": values[variable.key], "prompt_hash": digest}
            else:
                missing.append((variable.key, variable.prompt_template, variable.model))
        errors: list[PlanError] = []
        if missing:
            try:
                generated, failures = await self.manager.generate_ai_values(
                    access, row.profile, people, missing
                )
            except ConflictError as exc:
                generated, failures = {}, {key: exc.code for key, _, _ in missing}
            for key, prompt, model in missing:
                if key in generated:
                    values[key] = generated[key]
                    kept[key] = {"value": generated[key], "prompt_hash": prompt_hash(prompt, model)}
                else:
                    errors.append(
                        PlanError(
                            step.position,
                            "ai_not_generated",
                            f"ai.{key}",
                            failures.get(key, "email.ai_not_generated"),
                        )
                    )
        return values, kept, errors

    async def _plan(
        self,
        access: Access,
        people: People,
        steps: Sequence[EmailCampaignStep],
        row: MemberRow,
        stored: Mapping[int, EmailMemberRender],
        passed: int = 0,
    ) -> MemberPlan:
        """Every email this lead has not been sent yet and can still reach
        (after `passed`, the last step sent), rendered by the same code as a
        preview. Writes nothing: `_write` stores what the provider took.
        Leads are planned one after the other (the session is not shared
        across tasks); the model calls are phase 5's to parallelize."""
        plan = MemberPlan()
        for step in steps:
            if step.position <= passed:
                continue
            render = stored.get(step.position)
            if render is not None and render.sent_at is not None:
                continue
            values, kept, errors = await self._ai_values(access, people, row, step, render)
            if errors:
                plan.errors.extend(errors)
                continue
            rendered = EmailTemplateRenderer(require_ai=True).render(
                step.subject_template,
                step.body_template,
                people.context(
                    row.profile, row.account, values, [v.key for v in step.ai_variables]
                ),
            )
            plan.errors.extend(
                PlanError(e.position, e.kind, e.token)
                for e in render_errors(step.position, rendered)
            )
            plan.renders.append(
                PlannedRender(
                    step, render, rendered.subject, rendered.body, rendered.resolved_map(), kept
                )
            )
        return plan

    def _write(
        self, member: EmailCampaignMember, planned: PlannedRender, pushed_at: datetime
    ) -> None:
        """The render the provider took, frozen once it is sent (the
        database refuses to change a sent one)."""
        render = planned.stored
        if render is None:
            render = EmailMemberRender(
                campaign_member_id=member.id,
                step_position=planned.step.position,
                template_version=planned.step.version,
            )
            self.db.add(render)
            planned.stored = render
        render.campaign_step_id = planned.step.id
        render.subject = planned.subject
        render.body = planned.body
        render.resolved_variables = planned.resolved
        render.ai_values = planned.ai_values
        render.template_version = planned.step.version
        render.template_hash = planned.step.content_hash
        render.rendered_at = pushed_at
        render.provider_synced_at = pushed_at

    # ── the launch: the only hand-over (spec 8.5) ───────────────────────

    async def _launch_batch(
        self,
        access: Access,
        provider: EmailProvider,
        campaign: EmailCampaign,
        steps: Sequence[EmailCampaignStep],
        ref: CampaignRef,
        launch: EmailLaunchRequest,
        allowance: int,
        result: EmailLaunchResult,
    ) -> None:
        """The leads of this launch: the selected ones still to launch, or
        the next READY ones in order (those refused last time come last).
        Selected leads that are not to launch are told, never pushed."""
        if launch.member_ids:
            requested = list(dict.fromkeys(launch.member_ids))
            rows, waiting = await self.repo.ready_to_push(
                access.agency_id, campaign.id, len(requested), only=requested
            )
            found = {row.member.id for row in rows}
            result.skipped = await self._not_to_launch(
                access, campaign.id, [i for i in requested if i not in found]
            )
        else:
            # Read ahead: a lead the rules refuse never hides eligible ones.
            rows, waiting = await self.repo.ready_to_push(
                access.agency_id, campaign.id, max(allowance * 5, 100)
            )
        if allowance <= 0:
            result.remaining = waiting
            return
        await self._push_ready(access, provider, steps, ref, rows, result, allowance)
        result.remaining = max(waiting - result.members_pushed, 0)

    async def _not_to_launch(
        self, access: Access, campaign_id: Any, ids: Sequence[Any]
    ) -> list[EmailLaunchFailure]:
        """Why selected leads are not launched: launched already, ended, or
        not this campaign's (another agency's is not found, the same)."""
        if not ids:
            return []
        rows = {row.member.id: row for row in await self.repo.member_rows(access.agency_id, ids)}
        out: list[EmailLaunchFailure] = []
        for member_id in ids:
            row = rows.get(member_id)
            if row is None or row.member.campaign_id != campaign_id:
                out.append(EmailLaunchFailure(member_id=member_id, code=SKIP_NOT_IN_CAMPAIGN))
                continue
            member = row.member
            if member.queued_at is not None:
                code, params = SKIP_ALREADY_LAUNCHED, {}
            else:
                code, params = SKIP_NOT_READY, {"status": member.status}
            out.append(
                EmailLaunchFailure(
                    member_id=member_id,
                    client_profile_id=row.profile.id,
                    full_name=_full_name(row),
                    code=code,
                    params=params,
                )
            )
        return out

    async def _push_ready(
        self,
        access: Access,
        provider: EmailProvider,
        steps: Sequence[EmailCampaignStep],
        ref: CampaignRef,
        rows: Sequence[MemberRow],
        result: EmailLaunchResult,
        allowance: int,
    ) -> None:
        """Hand leads over, up to `allowance`, never one the shared rules
        (readiness) refuse, checked here again at the moment of the
        hand-over: an address may have bounced or unsubscribed elsewhere, a
        template may lack its value. A refused lead stays READY with its
        reason and is tried last next time; its template is checked before
        any AI value is generated."""
        facts = await self.manager.client_facts(access, rows)
        renders = await self.repo.renders_for([row.member.id for row in rows])
        people = await self.manager.people(access, [row.profile for row in rows])
        pushable: list[MemberRow] = []
        for row in rows:
            if len(pushable) >= allowance:
                break
            member = row.member
            email = client_identity(row.profile, row.account).email
            blocking = [i for i in membership_issues(member, email, facts) if i.blocking]
            check = template_issues(
                steps,
                renders.get(member.id, {}),
                people.context_for(row.profile, row.account),
                ai_configured=self.manager.ai_configured(),
            )
            template_block = [i for i in check.issues if i.blocking and i.code != TEMPLATE]
            if blocking:
                failure = PushFailure(blocking[0].code, dict(blocking[0].params))
            elif check.errors:
                failure = PushFailure(TEMPLATE, dict(check.errors[0].params))
            elif template_block:
                # An AI passage to write while AI is not configured: refused
                # with the code the Launch page shows, before any model call.
                failure = PushFailure(template_block[0].code, dict(template_block[0].params))
            else:
                pushable.append(row)
                continue
            set_status_reason(member, failure.code, failure.params)
            self._failed(result, row, failure)
        for row in pushable:
            plan = await self._plan(access, people, steps, row, renders.get(row.member.id, {}))
            failure_or_none = await self._push_member(provider, ref, row, plan)
            if failure_or_none is None:
                result.members_pushed += 1
                result.pushed_today += 1
                result.launched_ids.append(row.member.id)
                continue
            self._failed(result, row, failure_or_none)
            if failure_or_none.stop:
                result.notes.append(EmailSyncNote(code="plan_refused"))
                result.stopped = True
                break

    @staticmethod
    def _failed(result: EmailLaunchResult, row: MemberRow, failure: PushFailure) -> None:
        result.members_failed += 1
        result.failures.append(
            EmailLaunchFailure(
                member_id=row.member.id,
                client_profile_id=row.profile.id,
                full_name=_full_name(row),
                code=failure.code,
                params=failure.params,
            )
        )

    async def _push_member(
        self, provider: EmailProvider, ref: CampaignRef, row: MemberRow, plan: MemberPlan
    ) -> PushFailure | None:
        """Hand one ready lead over: the lead added (or the one a previous
        launch added, taken up again), each step personalized with its
        render, then the lead launched. Returns why it was not, if it was
        not; its `status_reason` says the same."""
        member = row.member
        failure: PushFailure
        if plan.errors:
            failure = PushFailure(TEMPLATE, plan.errors[0].params)
            set_status_reason(member, failure.code, failure.params)
            return failure
        identity = client_identity(row.profile, row.account)
        email = (identity.email or "").strip()
        if not email:
            failure = PushFailure(NO_EMAIL)
            set_status_reason(member, failure.code)
            return failure
        try:
            if member.provider_lead_id:
                lead = lead_ref(member)
            else:
                # Data minimisation (decision A8): the address and the name.
                lead = await asyncio.to_thread(
                    provider.add_contact,
                    ref,
                    LeadSpec(
                        email=email, first_name=identity.first_name, last_name=identity.last_name
                    ),
                )
                member.provider_lead_id = lead.lead_id
                member.provider_contact_id = lead.contact_id
                await self.db.flush()
            now = datetime.now(UTC)
            for planned in plan.renders:
                if planned.already_pushed:
                    continue
                position = planned.step.position
                if not planned.step.provider_step_id:
                    raise ProviderError(
                        f"Step {position} is missing at the provider.",
                        code="STEP_MISSING",
                        params={"position": position},
                    )
                accepted = await asyncio.to_thread(
                    provider.set_step_content,
                    ref,
                    lead,
                    planned.step.provider_step_id,
                    StepContent(subject=planned.subject, html=body_to_html(planned.body)),
                )
                if not accepted:
                    # Lemlist already reached it: the render Nidria holds
                    # stays the email really sent, never overwritten.
                    raise ProviderError(
                        f"The provider already reached step {position}.",
                        code="STEP_REACHED",
                        params={"position": position},
                    )
                self._write(member, planned, now)
            await asyncio.to_thread(provider.launch_contact, lead)
        except ProviderError as exc:
            code = provider_error_code(exc)
            if exc.code == "EMAIL_UNSUBSCRIBED":
                # The address is on the provider's unsubscribe list: a fact,
                # kept, and on the agency's opposition list too.
                member.status = "EMAIL_UNSUBSCRIBED"
                await self._block(member, email)
            failure = PushFailure(
                code, _scalars(provider_params(exc)), stop=code == PLAN_REFUSED_CODE
            )
            # Refused by the plan: added and personalized, not launched; the
            # next Launch takes the same lead up again.
            set_status_reason(member, failure.code, failure.params)
            return failure
        member.status = "EMAIL_QUEUED"
        set_status_reason(member, None)
        member.queued_at = datetime.now(UTC)
        return None


__all__ = [
    "REASON_BOUNCED",
    "REASON_KEPT_OUT",
    "REASON_MISSING_AT_PROVIDER",
    "REASON_NEEDS_RENDER",
    "REASON_PAUSED_AT_PROVIDER",
    "REASON_SEND_FAILED",
    "REASON_UNSUBSCRIBED_AT_PROVIDER",
    "AgencyReconcile",
    "CampaignReconcile",
    "EmailSyncManager",
    "IngestStats",
    "activity_since",
    "campaign_spec",
    "describe",
    "gate_error",
    "is_outdated",
    "set_paused",
]
