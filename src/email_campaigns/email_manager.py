"""The Email module's business rules for the agent routes (`/email/*`): access,
meta, campaigns, steps, AI variables, previews, recipients (phase 3A); the
Launch page, the performance, the outreach page, a client record's email
state, the add wizard and the deletion hooks (phase 3B).

Ported from Prism's `email_manager.py` (reference commit 629a2b0). The Sync
and the Launch themselves live in `email_sync.py`, on top of the shared
helpers here (`access`, `provider`, `key_team_id`, `campaign_for`, `people`,
`client_facts`, `member_read`, `provider_refusal`).

Rules that matter, in one place:
- Access (rule N3, spec 4.2). Every call needs the module ON for the agent's
  agency (the env master switch AND its `agency_email_module` row), else 409
  `email.module_disabled`; then `email.view` AND `case.view` (the module
  lists clients' names and addresses), else 403 `permission.denied` with the
  missing key in params, the very format of the RBAC refusals. An action
  then needs its own key, checked here as well as by the route binding (a
  binding is data): per row for the bulk actions, whose rows always answer
  200 with a code each.
- Isolation (rule N1). The agency comes from the token, never from the
  request: a campaign, a step, an AI variable, a membership or a client
  record of another agency is not found (404, a specific `email.*_not_found`
  or `profile.not_found`; `email.not_found` on a bulk action's row).
- A client holds at most one live membership (ready, queued, active, paused)
  across the agency's campaigns. A move ends the old one (kept, pointing at
  the new one) and opens a new READY one: progress is never carried over.
- The durable block (decision A1, rule N6, the opposition list of
  08/10/2026): removing or moving an UNSUBSCRIBED or BOUNCED membership ends
  it but keeps its status; the person's address is on the agency's
  opposition list (`email_suppression`, written by `email_sync`), which
  outlives the campaign and the client record, so deleting a campaign that
  holds one is allowed. A person blocked for good is never moved to another
  campaign (`email.member_blocked`).
- The provider's sequence is positional: Nidria step N is the provider's Nth
  step. Until the provider holds a lead, every structural change remaps the
  provider steps by position. Once it does, Lemlist only adds steps after
  the last one (measured, spec 9.2): content and delays stay editable, a
  follow-up can be appended, nothing is inserted, reordered or removed; the
  last step, if nobody got it yet, is parked at the provider instead
  (`sequence_capabilities`). The first email needs a subject.
- An object that exists at Lemlist is only changed through it: with the
  agency in dry run, Nidria refuses rather than diverge (409
  `email.provider_unavailable`, params `{"reason": "external_effects_off"}`).
  Changing the state of a campaign live at Lemlist, deleting it or parking
  one of its steps also takes `email.sync` (spec 4.2).
- A campaign that lives in another Lemlist team than the one the platform
  key opens is read only (409 `email.team_mismatch`, rule N4).
- Every refusal is a code with named params (rule N9). A provider refusal
  (`ProviderError`) becomes an `email.provider_*` code (`provider_error_code`).
- No `activity_log` write (rule N7): the gestures are kept on the membership
  (`added_by_agent_id`, `paused_by_agent_id`, `removed_by_agent_id`).

`status_reason` format (decision A12). A membership's `status_reason` holds a
stable CODE, never a sentence; its params, when it has some, live in
`provider_metadata[STATUS_REASON_PARAMS_KEY]` as a JSON object of named,
JSON-serialisable values (the reads serve them as `status_reason_params`).
Write both through `set_status_reason`. Codes written here:
- `removed`: taken out of the campaign by an agent (no params);
- `moved`: ended by a move, params `{"campaign_id": <target id>,
  "campaign_name": <target name at the time>}`.
Codes the Launch and the Sync write (`email_sync.py`), each one already
translated by the frontend for another surface:
- a readiness issue code (`INVALID_EMAIL`, `DEMO_RECIPIENT`, `TEMPLATE`,
  ...) with the issue's params: the hand-over refused the lead, it stays
  READY and is tried last next time (the Launch page shows the same code);
- an `email.provider_*` error code with its params: Lemlist refused the
  lead (`email.provider_plan_required` stops the batch);
- `needs_render`: paused by the sync, its email for a step Lemlist holds is
  not there yet (params `position`, then `kind`, `token`, `error` for an
  email that cannot be rendered, `code` for a provider refusal, or
  `reason: next_sync`); the sync resumes it once the email is pushed;
- `kept_out`: completed before a follow-up was added, paused at Lemlist so
  it never gets it.
Codes the provider's events and the reconciliation write (phase 4,
`email_sync.py`):
- `paused_at_provider`: someone paused the lead in Lemlist's app (a pause
  or resume done there is mirrored);
- `missing_at_provider`: Lemlist's complete listing of the campaign no
  longer has the lead (removed in its app?): flagged, never ended; cleared
  when the lead is listed again;
- `unsubscribed_at_provider`: Lemlist's listing says the lead unsubscribed;
- `bounced` (params `reason`: the provider's own words, when it gave some):
  the email bounced;
- `send_failed` (params `reason`, when given): Lemlist failed to send a
  step, the lead stays as it is.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, cast, get_args

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agency_email_module import AgencyEmailModule
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.company_profile import CompanyProfile
from shared.models.email_campaign import (
    BLOCKING_MEMBER_STATUSES,
    LIVE_MEMBER_STATUSES,
    PROVIDER_LEMLIST,
    EmailCampaign,
    EmailCampaignAiVariable,
    EmailCampaignMember,
    EmailCampaignStep,
    EmailMemberRender,
    EmailMessage,
)
from shared.models.expat_user import ExpatUser
from src.agencies.agencies_repository import AgenciesRepository
from src.client_profiles.client_profiles_repository import ClientProfilesRepository
from src.client_profiles.client_profiles_schema import ProfileListFilter
from src.company_profiles.company_profiles_schema import CompanyListFilter
from src.core.config import Settings, get_settings
from src.core.email import normalize_email
from src.core.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from src.core.rbac.enforcement import effective_permissions
from src.core.rbac.permissions import Permission
from src.email_campaigns.email_repository import EmailRepository, MemberRow
from src.email_campaigns.email_schema import (
    CampaignProviderMode,
    CampaignStatus,
    ClientEmailStatus,
    ClientStatus,
    Eligibility,
    EmailActionResult,
    EmailActionResultRow,
    EmailAddMembersRequest,
    EmailAddMembersResponse,
    EmailAddPreviewResponse,
    EmailAddPreviewRow,
    EmailAddPreviewSummary,
    EmailAiTestRequest,
    EmailAiTestResponse,
    EmailAiVariableCreate,
    EmailAiVariableRead,
    EmailAiVariableUpdate,
    EmailCampaignCreate,
    EmailCampaignDeleteRead,
    EmailCampaignDetailRead,
    EmailCampaignLeadStats,
    EmailCampaignRead,
    EmailCampaignSettings,
    EmailCampaignUpdate,
    EmailCandidateClient,
    EmailCandidateClientList,
    EmailCandidateCompany,
    EmailCandidateCompanyList,
    EmailClientProfileRead,
    EmailConversationItem,
    EmailEventRead,
    EmailIssueRead,
    EmailLaunchReadiness,
    EmailLaunchReadinessRow,
    EmailLeadFunnelRead,
    EmailMemberDetail,
    EmailMemberListResponse,
    EmailMemberRead,
    EmailMessageTotalsRead,
    EmailMetaRead,
    EmailOutreachCampaign,
    EmailOutreachItem,
    EmailOutreachRead,
    EmailPerformanceDay,
    EmailPerformanceRates,
    EmailPerformanceRead,
    EmailPreviewClient,
    EmailPreviewRequest,
    EmailPreviewResponse,
    EmailRenderErrorRead,
    EmailReplyRead,
    EmailResolvedVariableRead,
    EmailScheduleHoldRead,
    EmailSenderOption,
    EmailSendersRead,
    EmailSequenceCapabilities,
    EmailStepCapabilities,
    EmailStepCreate,
    EmailStepImpactRead,
    EmailStepPerformanceRead,
    EmailStepRead,
    EmailStepReorder,
    EmailStepTimeline,
    EmailStepUpdate,
    EmailTeamInfo,
    EmailVariableFillRead,
    EmailVariableRead,
    LaunchReadinessFilter,
    MemberStatus,
    OutreachKind,
    SequenceReason,
    StepDeleteReason,
)
from src.email_campaigns.flags import (
    email_module_effectively_enabled,
    external_effects_on,
    is_real_provider_id,
    provider_state_for,
    require_email_module,
)
from src.email_campaigns.html_text import html_to_text
from src.email_campaigns.lemlist_team import TeamUnknownError, team_of_key
from src.email_campaigns.providers.base import (
    CampaignRef,
    EmailProvider,
    LeadRef,
    ProviderError,
    ProviderSender,
    StepSpec,
)
from src.email_campaigns.providers.dry_run import is_dry_run_id
from src.email_campaigns.providers.registry import provider_for
from src.email_campaigns.readiness import (
    ClientFacts,
    Issue,
    TemplateCheck,
    campaign_gates,
    client_issues,
    daily_limit_gate,
    day_start,
    durable_block,
    load_client_facts,
    membership_issues,
    not_launched,
    sender_gate,
    template_issues,
)
from src.email_campaigns.readiness import team_mismatch as on_other_team
from src.email_campaigns.renderer import (
    EmailTemplateRenderer,
    RenderResult,
    content_hash,
    is_valid_ai_key,
    tokens_in,
)
from src.email_campaigns.schedule import delay_days, schedule, today_in, zone_of
from src.email_campaigns.variables import (
    RenderContext,
    catalog_for,
    client_identity,
    sole_company,
    value_of,
)

LIVE = frozenset(LIVE_MEMBER_STATUSES)
BLOCKING = frozenset(BLOCKING_MEMBER_STATUSES)

#: Where a membership's `status_reason` params live, in its `provider_metadata`.
STATUS_REASON_PARAMS_KEY = "status_reason_params"
#: `status_reason` codes this module writes (see the module docstring).
REASON_REMOVED = "removed"
REASON_MOVED = "moved"

_SEQUENCE_KEYS = (
    "can_edit_step_content",
    "can_edit_step_delay",
    "can_add_step",
    "can_insert_step",
    "can_reorder_steps",
)
#: Campaign status -> the statuses it may become (spec 8.1).
_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"ACTIVE", "ARCHIVED"}),
    "ACTIVE": frozenset({"PAUSED", "ARCHIVED"}),
    "PAUSED": frozenset({"ACTIVE", "ARCHIVED"}),
    "ARCHIVED": frozenset({"PAUSED"}),
}
#: A ProviderError's code -> the `email.*` code the agent gets (rule N9).
_PROVIDER_CODES: dict[str, str] = {
    "PLAN_REQUIRED": "email.provider_plan_required",
    "ALREADY_IN_OTHER_CAMPAIGN": "email.provider_already_in_other_campaign",
    "ALREADY_IN_CAMPAIGN": "email.provider_already_in_campaign",
    "EMAIL_UNSUBSCRIBED": "email.provider_unsubscribed",
    "RETRY_LATER": "email.provider_retry_later",
    "RATE_LIMITED": "email.provider_retry_later",
    "STEP_IN_USE": "email.provider_step_in_use",
    "INSERT_IN_USE": "email.provider_insert_in_use",
    "SENDER_REQUIRED": "email.no_sender",
    "SENDER_MAILBOX": "email.provider_sender_mailbox",
    "TEAM_UNKNOWN": "email.provider_unavailable",
    "TIMEOUT": "email.provider_unreachable",
    "UNREACHABLE": "email.provider_unreachable",
    # Raised by the hand-over itself (email_sync): a step Lemlist does not
    # have yet, and one it already reached for this lead.
    "STEP_MISSING": "email.provider_step_missing",
    "STEP_REACHED": "email.provider_step_reached",
    # Raised by the reconciliation (email_sync): Lemlist no longer has the
    # campaign (deleted in its app); Nidria changes nothing of it.
    "CAMPAIGN_MISSING": "email.provider_campaign_missing",
}
#: Any other provider failure.
PROVIDER_ERROR = "email.provider_error"

#: Team id -> (expiry, senders): the senders route costs Lemlist one call per sender.
_SENDERS_CACHE: dict[str, tuple[datetime, list[ProviderSender]]] = {}
_SENDERS_TTL = timedelta(minutes=5)
#: (agency, campaign, population size) -> (expiry, fill counts). A new
#: recipient changes the size, so a campaign's counts follow it at once.
_FILL_CACHE: dict[
    tuple[uuid.UUID, uuid.UUID | None, int], tuple[datetime, EmailVariableFillRead]
] = {}
_FILL_TTL = timedelta(minutes=10)
#: Clients resolved at most per count; beyond, a random sample.
FILL_SAMPLE = 2000


def clear_caches() -> None:
    """Forget the cached senders and fill counts (tests)."""
    _SENDERS_CACHE.clear()
    _FILL_CACHE.clear()


# ── refusals ────────────────────────────────────────────────────────────────


def _denied(permission: Permission) -> ForbiddenError:
    """The RBAC refusal's own format (`enforcement.enforce`)."""
    return ForbiddenError(
        "Missing permission.", code="permission.denied", params={"permission": permission.value}
    )


def _effects_off() -> ConflictError:
    return ConflictError(
        "This campaign exists at Lemlist and the agency runs in dry run: nothing changes "
        "until it talks to Lemlist again.",
        code="email.provider_unavailable",
        params={"reason": "external_effects_off"},
    )


def provider_error_code(exc: ProviderError) -> str:
    """The `email.*` code of a provider refusal (rule N9)."""
    return _PROVIDER_CODES.get(exc.code, PROVIDER_ERROR)


def provider_params(exc: ProviderError) -> dict[str, Any]:
    if exc.code == "TEAM_UNKNOWN":
        # The same params as every other "Lemlist does not say" (decision A4).
        return {"reason": "team_unknown"}
    return dict(exc.params)


def provider_refusal(exc: ProviderError) -> ConflictError:
    """A provider refusal as the 409 the agent gets: a code and its params;
    Lemlist's own words stay in the English detail."""
    return ConflictError(
        f"Lemlist refused ({exc.code}): {exc}",
        code=provider_error_code(exc),
        params=provider_params(exc),
    )


# ── pure rules ──────────────────────────────────────────────────────────────


def campaign_provider_mode(provider_id: str | None) -> CampaignProviderMode:
    if not provider_id:
        return "NONE"
    return "DRY_RUN" if is_dry_run_id(provider_id) else "LIVE"


def _ai_prompts(step: EmailCampaignStep) -> dict[str, str]:
    return {v.key: f"{v.prompt_template}|{v.model or ''}" for v in step.ai_variables}


def prompt_hash(prompt: str, model: str | None) -> str:
    """Identifies the prompt an AI value was generated from (frozen with it)."""
    return hashlib.sha256(f"{prompt}|{model or ''}".encode()).hexdigest()[:16]


def refresh_hash(step: EmailCampaignStep) -> None:
    """Recompute the step's content hash; a new content is a new version."""
    new = content_hash(step.subject_template, step.body_template, _ai_prompts(step))
    if step.content_hash and new != step.content_hash:
        step.version += 1
    step.content_hash = new


def _remap_shells(ordered: list[EmailCampaignStep], shells: list[str]) -> list[str]:
    """Give each step the provider step at its position: the provider's
    sequence order never changes, only which Nidria step it carries. Returns
    the provider steps left over, to delete there."""
    for index, step in enumerate(ordered):
        shell = shells[index] if index < len(shells) else None
        if step.provider_step_id != shell:
            step.provider_step_id = shell
            step.provider_synced_hash = None
        step.position = index + 1
    return shells[len(ordered) :]


def _shells(campaign: EmailCampaign) -> list[str]:
    return [
        s.provider_step_id
        for s in sorted(campaign.steps, key=lambda s: s.position)
        if s.provider_step_id
    ]


def _require_subject(subject: str) -> None:
    """The first email of a sequence needs a subject (spec 8.2)."""
    if not subject.strip():
        raise BadRequestError(
            "The first email needs a subject.", code="email.first_step_subject_required"
        )


def _first_subject_required(ordered: Sequence[EmailCampaignStep]) -> None:
    """Refused before any change that would leave position 1 without a subject."""
    if ordered:
        _require_subject(ordered[0].subject_template)


@dataclass(frozen=True)
class SequenceFacts:
    """Where a campaign's recipients are, which decides what its sequence allows."""

    #: The provider has (or had) a lead of this campaign: its sequence is in use.
    in_use: bool
    #: When the first lead was handed to the provider.
    launched_at: datetime | None
    #: (status, last step sent) of every membership.
    progress: tuple[tuple[str, int | None], ...] = ()

    def reached(self, position: int) -> int:
        """Recipients sent this step or a later one."""
        return sum(1 for _, step in self.progress if (step or 0) >= position)


def sequence_capabilities(
    status: str, facts: SequenceFacts, *, team_mismatch: bool = False
) -> EmailSequenceCapabilities:
    """What a campaign's sequence allows now. Everything until the provider
    holds a lead; then content, delays and a step at the end (Lemlist refuses
    an insert and a removal, and has no reorder). Nothing for another team's
    campaign, nor an archived one."""
    if team_mismatch or status == "ARCHIVED":
        reason: SequenceReason = "team_mismatch" if team_mismatch else "archived"
        return EmailSequenceCapabilities(
            active_sequence=facts.in_use,
            **dict.fromkeys(_SEQUENCE_KEYS, False),
            reasons=dict.fromkeys(_SEQUENCE_KEYS, reason),
        )
    if not facts.in_use:
        return EmailSequenceCapabilities(
            active_sequence=False, **dict.fromkeys(_SEQUENCE_KEYS, True)
        )
    return EmailSequenceCapabilities(
        active_sequence=True,
        can_edit_step_content=True,
        can_edit_step_delay=True,
        can_add_step=True,
        can_insert_step=False,
        can_reorder_steps=False,
        reasons={"can_insert_step": "insert_in_use", "can_reorder_steps": "reorder_in_use"},
    )


def step_capabilities(
    step: EmailCampaignStep,
    steps: Sequence[EmailCampaignStep],
    facts: SequenceFacts,
    *,
    team_mismatch: bool = False,
) -> EmailStepCapabilities:
    """What can happen to one step. Once the sequence is in use, only a step
    not at the provider yet goes freely; the provider's last one is parked
    there if nobody got it (Lemlist removes nothing then), and skipping is
    Lemlist's app's."""
    reached = facts.reached(step.position)
    added = bool(
        facts.launched_at is not None
        and step.created_at is not None
        and step.created_at > facts.launched_at
    )
    pending = facts.in_use and not step.provider_step_id
    if team_mismatch:
        return EmailStepCapabilities(
            can_delete=False,
            delete_reason="team_mismatch",
            reached=reached,
            added_after_launch=added,
        )
    if not facts.in_use or pending:
        return EmailStepCapabilities(
            can_delete=True, reached=reached, added_after_launch=added, pending=pending
        )
    reason: StepDeleteReason | None
    if step.position == 1:
        reason = "first_step_in_use"
    elif any(s.provider_step_id and s.position > step.position for s in steps):
        reason = "not_last_at_provider"
    elif reached:
        reason = "already_sent"
    else:
        reason = None
    return EmailStepCapabilities(
        can_delete=reason is None,
        delete_reason=reason,
        can_skip=False,
        skip_reason="provider_app_only" if reason else None,
        reached=reached,
        added_after_launch=added,
    )


def step_impact(position: int, steps_count: int, facts: SequenceFacts) -> EmailStepImpactRead:
    """Who a step added at `position` reaches: running recipients not past it.
    Stopped, done, paused and removed ones are counted apart, never as
    receiving it retroactively."""
    counts = dict.fromkeys(
        ("will_receive", "already_past", "completed", "paused", "replied", "removed", "ended"), 0
    )
    for status, step in facts.progress:
        if status == "EMAIL_REMOVED":
            counts["removed"] += 1
        elif status == "EMAIL_REPLIED":
            counts["replied"] += 1
        elif status in BLOCKING:
            counts["ended"] += 1
        elif status == "EMAIL_COMPLETED":
            counts["completed"] += 1
        elif status == "EMAIL_PAUSED":
            counts["paused"] += 1
        elif (step or 0) >= position:
            counts["already_past"] += 1
        else:
            counts["will_receive"] += 1
    insert = position <= steps_count
    refused = insert and facts.in_use
    return EmailStepImpactRead(
        position=position,
        mode="INSERT" if insert else "APPEND",
        allowed=not refused,
        reason="insert_in_use" if refused else None,
        total=len(facts.progress),
        **counts,
    )


def chosen_mailbox(campaign: EmailCampaign) -> str | None:
    """The sending mailbox picked in the campaign's "Sent from", if any: the
    provider does not start the campaign until it sends from it."""
    sender = EmailCampaignSettings.model_validate(campaign.settings or {}).sender
    return sender.email if sender else None


def campaign_ref(campaign: EmailCampaign) -> CampaignRef | None:
    if not campaign.provider_campaign_id:
        return None
    return CampaignRef(
        campaign.provider_campaign_id,
        campaign.provider_sequence_id,
        campaign.provider_schedule_id,
    )


def lead_ref(member: EmailCampaignMember) -> LeadRef:
    return LeadRef(member.provider_lead_id or "", member.provider_contact_id)


def set_status_reason(
    member: EmailCampaignMember, code: str | None, params: Mapping[str, Any] | None = None
) -> None:
    """Write a membership's `status_reason` code and its params together
    (see the module docstring). The JSONB is reassigned, never mutated in
    place, so the change is tracked."""
    member.status_reason = code
    metadata = dict(member.provider_metadata or {})
    if code is not None and params:
        metadata[STATUS_REASON_PARAMS_KEY] = dict(params)
    else:
        metadata.pop(STATUS_REASON_PARAMS_KEY, None)
    member.provider_metadata = metadata


def status_reason_params(member: EmailCampaignMember) -> dict[str, Any]:
    found = (member.provider_metadata or {}).get(STATUS_REASON_PARAMS_KEY)
    return dict(found) if isinstance(found, dict) else {}


def _check_prompt(prompt: str) -> None:
    """An AI prompt reads client, company and agency values, never another
    AI variable (Prism's rule)."""
    for token in tokens_in(prompt):
        if token.startswith("ai."):
            raise BadRequestError(
                "An AI prompt can use client, company and agency variables, not AI variables.",
                code="email.ai_prompt_invalid",
                params={"token": token},
            )


def _check_ai_key(key: str) -> None:
    if not is_valid_ai_key(key):
        raise BadRequestError(
            "A key is lowercase letters, digits and underscores, starting with a letter.",
            code="email.ai_variable_key_invalid",
            params={"key": key},
        )


def _issue_read(issue: Issue) -> EmailIssueRead:
    return EmailIssueRead(code=issue.code, params=dict(issue.params), blocking=issue.blocking)


def _render_errors(result: RenderResult) -> list[EmailRenderErrorRead]:
    return [EmailRenderErrorRead(kind=e.kind, token=e.token) for e in result.errors]


def _full_name(first: str | None, last: str | None) -> str:
    return " ".join(part for part in (first, last) if part)


def _reply_read(message: EmailMessage) -> EmailReplyRead:
    """A reply as text: the text the sync kept, else the provider's HTML read
    as text (`html_text`), never the HTML itself."""
    text = message.body_text
    if text is None and message.body_html:
        text = html_to_text(message.body_html) or None
    return EmailReplyRead(
        id=message.id,
        step_position=message.step_position,
        received_at=message.received_at,
        subject=message.subject,
        body_text=text,
        content_state=message.content_state,
    )


def _event_read(event: Any) -> EmailEventRead:
    return EmailEventRead(
        id=event.id,
        type=event.type,
        provider_type=event.provider_type,
        step_position=event.step_position,
        occurred_at=event.occurred_at,
        campaign_member_id=event.campaign_member_id,
    )


def _row_result(
    member: EmailCampaignMember,
    *,
    ok: bool,
    code: str | None = None,
    params: Mapping[str, Any] | None = None,
    new_id: uuid.UUID | None = None,
) -> EmailActionResultRow:
    return EmailActionResultRow(
        id=member.id,
        ok=ok,
        status=member.status,
        code=code,
        params=dict(params or {}),
        new_id=new_id,
    )


#: Why one row of a bulk action failed: (code, params).
RowFailure = tuple[str, dict[str, Any]]


def _summary(ids: Sequence[uuid.UUID], rows: list[EmailActionResultRow]) -> EmailActionResult:
    """The rows in the order the ids were asked."""
    order = {member_id: index for index, member_id in enumerate(dict.fromkeys(ids))}
    rows.sort(key=lambda r: order.get(r.id, len(order)))
    return EmailActionResult(
        done=sum(r.ok for r in rows), failed=sum(not r.ok for r in rows), rows=rows
    )


#: A client's email state, highest precedence first (spec 8.4).
_EMAIL_PRECEDENCE: tuple[tuple[ClientEmailStatus, frozenset[str]], ...] = (
    ("EMAIL_REPLIED", frozenset({"EMAIL_REPLIED"})),
    ("EMAIL_IN_PROGRESS", frozenset({"EMAIL_QUEUED", "EMAIL_ACTIVE"})),
    ("EMAIL_PAUSED", frozenset({"EMAIL_PAUSED"})),
    ("EMAIL_READY", frozenset({"EMAIL_READY"})),
    ("EMAIL_COMPLETED", frozenset({"EMAIL_COMPLETED"})),
    ("EMAIL_BOUNCED", frozenset({"EMAIL_BOUNCED"})),
    ("EMAIL_UNSUBSCRIBED", frozenset({"EMAIL_UNSUBSCRIBED"})),
)
#: The outreach page covers at most this many days (spec 8.8).
OUTREACH_MAX_DAYS = 62
#: Items one outreach answer carries at most (Prism's cap): narrow the filters.
OUTREACH_LIMIT = 2000
#: Ready leads the Launch page lists by id at most (Prism's cap).
READY_IDS_CAP = 2000


def client_email_status(statuses: Iterable[str]) -> ClientEmailStatus | None:
    """A client's (or a company's clients') email state, derived from the
    memberships' statuses when read, never stored: a reply anywhere wins,
    then anything still sending, a pause, a lead waiting to start, then how
    finished sequences ended. A removed membership does not count; an
    unsubscribed or bounced one keeps its status when removed (decision A1),
    so the block still shows."""
    present = {s for s in statuses if s != "EMAIL_REMOVED"}
    for status, members in _EMAIL_PRECEDENCE:
        if present & members:
            return status
    return None


def _ratio(part: int, whole: int) -> float | None:
    """part / whole, or None when the whole is zero (no rate, never 0%)."""
    return round(part / whole, 4) if whole else None


def _invalid_period() -> BadRequestError:
    return BadRequestError("The period ends before it starts.", code="email.invalid_period")


# ── who asks, and what a template reads ─────────────────────────────────────


@dataclass(frozen=True)
class Access:
    """An agent inside the Email module of its agency: the module is on and
    the agent holds `email.view` and `case.view`. Plain values only, so a
    rollback (which expires the session's objects) never reaches them."""

    agent_id: uuid.UUID
    agency_id: uuid.UUID
    module: AgencyEmailModule
    permissions: frozenset[str]

    def can(self, permission: Permission) -> bool:
        return permission.value in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.can(permission):
            raise _denied(permission)


@dataclass(frozen=True)
class People:
    """What a template reads beyond a client's own row, loaded in batch for
    the clients at hand: the agency, their linked companies and their live
    cases (the derived status)."""

    agency: Agency | None
    #: client record id -> companies linked by `company_profile_role`.
    companies: Mapping[uuid.UUID, Sequence[CompanyProfile]] = field(default_factory=dict)
    #: client-space account id -> the agency's live cases of that account.
    cases: Mapping[uuid.UUID, Sequence[ClientCase]] = field(default_factory=dict)

    def companies_of(self, profile: ClientProfile) -> Sequence[CompanyProfile]:
        return self.companies.get(profile.id, ())

    def cases_of(self, profile: ClientProfile) -> Sequence[ClientCase]:
        if profile.expat_user_id is None:
            return ()
        return self.cases.get(profile.expat_user_id, ())

    def company(self, profile: ClientProfile) -> CompanyProfile | None:
        """What `company.*` reads: the linked company when there is one only."""
        return sole_company(self.companies_of(profile))

    def client_status(self, profile: ClientProfile) -> ClientStatus:
        # Imported here: the client records' manager will call this module
        # (deletion hooks, phase 3B); a module-level import would be a cycle.
        from src.client_profiles.client_profiles_manager import derived_client_status

        status = derived_client_status(list(self.cases_of(profile)), profile.status_override)
        return "client" if status == "client" else "prospect"

    def context(
        self,
        profile: ClientProfile,
        account: ExpatUser | None,
        ai_values: dict[str, str] | None = None,
        ai_keys: Sequence[str] | None = None,
    ) -> RenderContext:
        return RenderContext(
            client=profile,
            account=account,
            companies=self.companies_of(profile),
            agency=self.agency,
            cases=self.cases_of(profile),
            ai_values=dict(ai_values or {}),
            ai_keys=frozenset(ai_keys) if ai_keys is not None else None,
        )

    def context_for(
        self, profile: ClientProfile, account: ExpatUser | None
    ) -> Callable[[list[str]], RenderContext]:
        """The client's render context without AI values, for a step's AI
        keys: what a read checks a template against (readiness)."""

        def build(keys: list[str]) -> RenderContext:
            return self.context(profile, account, None, keys)

        return build


class EmailManager:
    """The Email module of the agent's agency.

    Every read and write stays in that agency: an object of another agency
    is not found, whatever the caller may see there. One instance serves one
    request: the agency's provider and the platform key's team are asked
    once and kept."""

    def __init__(self, db: AsyncSession, *, settings: Settings | None = None) -> None:
        self.db = db
        self.repo = EmailRepository(db)
        self.settings = settings or get_settings()
        self._provider: EmailProvider | None = None
        self._key_team: tuple[str | None] | None = None
        self._agency: tuple[Agency | None] | None = None

    # ── access ─────────────────────────────────────────────────────────────

    async def access(self, agent: Agent, permission: Permission | None = None) -> Access:
        """The agent inside its agency's Email module, holding `permission`.

        409 `email.module_disabled` while the module is off for the agency;
        403 `permission.denied` without `email.view` (the route binding's own
        key, checked again) or `case.view` (the module lists clients), or
        without `permission`."""
        module = require_email_module(await self.repo.get_module(agent.agency_id), self.settings)
        access = Access(
            agent_id=agent.id,
            agency_id=agent.agency_id,
            module=module,
            permissions=frozenset(effective_permissions(agent)),
        )
        access.require(Permission.EMAIL_VIEW)
        access.require(Permission.CASE_VIEW)
        if permission is not None:
            access.require(permission)
        return access

    async def provider(self, access: Access) -> EmailProvider:
        """The agency's provider (rules N4, N5): Lemlist only when the agency
        talks to it for real, else the dry run. 409
        `email.provider_unavailable` when the agency is bound, effects are on
        and Lemlist does not say which team the key opens (decision A4)."""
        if self._provider is None:
            self._provider = await asyncio.to_thread(provider_for, self.settings, access.module)
        return self._provider

    async def provider_of(self, module: AgencyEmailModule | None) -> EmailProvider:
        """The provider of the agency whose switch row is `module`, without an
        agent (the job, the webhook, the deletion hooks): the same choice as
        `provider` (rules N4, N5), never cached. Raises 409
        `email.provider_unavailable` when Lemlist does not say which team
        the key opens (decision A4)."""
        return await asyncio.to_thread(provider_for, self.settings, module)

    def through_provider(self, provider: EmailProvider, provider_id: str | None) -> bool:
        """Whether changing this provider object must go through the provider.
        A dry-run id exists only in Nidria (the dry run records it); a real
        one needs the agency to talk to Lemlist: in dry run, Nidria refuses
        rather than diverge from Lemlist (409 `email.provider_unavailable`)."""
        if not provider_id:
            return False
        if is_dry_run_id(provider_id):
            return not provider.external
        if not provider.external:
            raise _effects_off()
        return True

    async def key_team_id(self, access: Access) -> str | None:
        """The Lemlist team the platform key opens now, for the TEAM_MISMATCH
        gate (rule N4). The agency's provider says it when it is Lemlist;
        otherwise the key's team is asked only for a bound agency (cached ten
        minutes, no call while real effects are off). None when nothing says:
        a read never fails on it."""
        if self._key_team is not None:
            return self._key_team[0]
        team: str | None = None
        try:
            provider: EmailProvider | None = await self.provider(access)
        except ConflictError:
            provider = None
        if provider is not None and provider.external:
            try:
                team = await asyncio.to_thread(provider.team_id)
            except ProviderError:
                team = None
        elif access.module.provider_team_id is not None:
            try:
                found = await asyncio.to_thread(team_of_key, self.settings)
            except TeamUnknownError:
                found = None
            team = found.id if found is not None else None
        self._key_team = (team,)
        return team

    async def team_mismatch(self, access: Access, campaign: EmailCampaign) -> bool:
        """The campaign lives in another Lemlist team than the key's (read only)."""
        if not is_real_provider_id(campaign.provider_campaign_id):
            return False
        return on_other_team(campaign, await self.key_team_id(access))

    async def writable(self, access: Access, campaign: EmailCampaign) -> None:
        """Another team's campaign is history: nothing changes it."""
        if await self.team_mismatch(access, campaign):
            raise ConflictError(
                "This campaign lives in another Lemlist team: it is read only.",
                code="email.team_mismatch",
            )

    async def campaign_for(
        self, access: Access, campaign_id: uuid.UUID, *, lock: bool = False
    ) -> EmailCampaign:
        """A campaign of the agency (steps loaded), else 404."""
        campaign = await self.repo.campaign(access.agency_id, campaign_id, lock=lock)
        if campaign is None:
            raise NotFoundError("Campaign not found.", code="email.campaign_not_found")
        return campaign

    async def _step(
        self, access: Access, step_id: uuid.UUID
    ) -> tuple[EmailCampaignStep, EmailCampaign]:
        """A step of the agency's campaigns, its campaign locked and writable;
        the step is the campaign's own copy, so edits and reads see one object."""
        found = await self.repo.step(access.agency_id, step_id)
        if found is None:
            raise NotFoundError("Step not found.", code="email.step_not_found")
        campaign = await self.campaign_for(access, found.campaign_id, lock=True)
        await self.writable(access, campaign)
        step = next(s for s in campaign.steps if s.id == step_id)
        return step, campaign

    async def client_profile(self, access: Access, profile_id: uuid.UUID) -> ClientProfile:
        """A client record of the agency (its account loaded), else 404, with
        the client records' own code."""
        profile = await ClientProfilesRepository(self.db).get_for_agency(
            access.agency_id, profile_id
        )
        if profile is None:
            raise NotFoundError("Client profile not found.", code="profile.not_found")
        return profile

    async def agency(self, access: Access) -> Agency | None:
        if self._agency is None:
            self._agency = (await AgenciesRepository(self.db).get_agency(access.agency_id),)
        return self._agency[0]

    async def people(self, access: Access, profiles: Sequence[ClientProfile]) -> People:
        """The agency, companies and live cases of these clients: three
        queries whatever their number."""
        ids = list(dict.fromkeys(p.id for p in profiles))
        expat_ids = sorted({p.expat_user_id for p in profiles if p.expat_user_id is not None})
        cases: dict[uuid.UUID, list[ClientCase]] = {}
        if expat_ids:
            rows = await ClientProfilesRepository(self.db).cases_for_profiles(
                access.agency_id, expat_ids
            )
            for expat_id, case, _journey_name in rows:
                cases.setdefault(expat_id, []).append(case)
        return People(
            agency=await self.agency(access),
            companies=await self.repo.companies_of(access.agency_id, ids),
            cases=cases,
        )

    async def client_facts(self, access: Access, rows: Sequence[MemberRow]) -> ClientFacts:
        """The readiness facts of these memberships' clients (blocks, live
        memberships, by record and by effective address)."""
        return await load_client_facts(
            self.repo,
            access.agency_id,
            [(r.profile.id, client_identity(r.profile, r.account).email) for r in rows],
        )

    # ── meta ───────────────────────────────────────────────────────────────

    async def meta(self, agent: Agent) -> EmailMetaRead:
        access = await self.access(agent)
        module = access.module
        state = await asyncio.to_thread(provider_state_for, module, self.settings)
        return EmailMetaRead(
            enabled=True,
            provider=module.provider,
            provider_mode=state.mode,
            team=(
                EmailTeamInfo(id=module.provider_team_id, name=module.provider_team_name)
                if module.provider_team_id is not None
                else None
            ),
            team_mismatch=state.team_mismatch,
            can_manage=access.can(Permission.EMAIL_MANAGE),
            can_enroll=access.can(Permission.EMAIL_ENROLL),
            can_pause=access.can(Permission.EMAIL_PAUSE),
            can_remove=access.can(Permission.EMAIL_REMOVE),
            can_sync=access.can(Permission.EMAIL_SYNC),
            campaign_statuses=list(get_args(CampaignStatus)),
            member_statuses=list(get_args(MemberStatus)),
            client_statuses=list(get_args(ClientStatus)),
            variables=[
                EmailVariableRead(
                    token=v.token, source=v.source, type=v.value_type, example=v.example
                )
                for v in catalog_for()
            ],
        )

    async def variable_fill(
        self, agent: Agent, campaign_id: uuid.UUID | None = None
    ) -> EmailVariableFillRead:
        """How often each variable has a value, among the campaign's live
        recipients (else the agency's clients with an email), resolved
        exactly as an email is. Sampled beyond `FILL_SAMPLE`, cached ten
        minutes per agency, campaign and population size."""
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        population: Literal["campaign", "agency"] = "agency"
        total = 0
        rows: list[tuple[ClientProfile, ExpatUser | None]] = []
        if campaign_id is not None:
            await self.campaign_for(access, campaign_id)
            total, rows = await self.repo.fill_population(
                access.agency_id, campaign_id, FILL_SAMPLE
            )
            if total:
                population = "campaign"
        if population == "agency":
            campaign_id = None
            total, rows = await self.repo.fill_population(access.agency_id, None, FILL_SAMPLE)
        key = (access.agency_id, campaign_id, total)
        cached = _FILL_CACHE.get(key)
        if cached is not None and cached[0] > datetime.now(UTC):
            return cached[1]
        catalog = catalog_for()
        filled = dict.fromkeys((v.token for v in catalog), 0)
        people = await self.people(access, [profile for profile, _ in rows])
        for profile, account in rows:
            ctx = people.context(profile, account)
            for definition in catalog:
                if value_of(definition, ctx) is not None:
                    filled[definition.token] += 1
        result = EmailVariableFillRead(
            population=population,
            campaign_id=campaign_id,
            total=total,
            considered=len(rows),
            filled=filled,
        )
        _FILL_CACHE[key] = (datetime.now(UTC) + _FILL_TTL, result)
        return result

    async def senders(self, agent: Agent) -> EmailSendersRead:
        """Who a campaign of this agency can be sent from: Lemlist's senders
        of the agency's team (cached a few minutes per team). In a dry run,
        what the dry run answers (nobody: Lemlist is not asked)."""
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        provider = await self.provider(access)
        if not provider.external:
            found = await asyncio.to_thread(provider.list_senders)
            return EmailSendersRead(
                external=False, senders=[_sender_option(s) for s in found], note="dry_run"
            )
        team = await self.key_team_id(access)
        cached = _SENDERS_CACHE.get(team) if team else None
        if cached is not None and cached[0] > datetime.now(UTC):
            senders = cached[1]
        else:
            try:
                senders = await asyncio.to_thread(provider.list_senders)
            except ProviderError as exc:
                return EmailSendersRead(
                    external=True, note="provider_error", error_code=provider_error_code(exc)
                )
            if team:
                _SENDERS_CACHE[team] = (datetime.now(UTC) + _SENDERS_TTL, senders)
        return EmailSendersRead(
            external=True,
            senders=[_sender_option(s) for s in senders],
            note=None if senders else "no_sender",
        )

    # ── campaigns ──────────────────────────────────────────────────────────

    def step_read(
        self, step: EmailCampaignStep, capabilities: EmailStepCapabilities | None = None
    ) -> EmailStepRead:
        return EmailStepRead(
            id=step.id,
            campaign_id=step.campaign_id,
            position=step.position,
            name=step.name,
            type=step.type,
            delay_value=step.delay_value,
            delay_unit=step.delay_unit,
            subject_template=step.subject_template,
            body_template=step.body_template,
            provider_step_id=step.provider_step_id,
            version=step.version,
            content_hash=step.content_hash,
            unsynced=step.provider_step_id is None
            or step.provider_synced_hash != step.content_hash,
            variables_count=len(
                [
                    t
                    for t in tokens_in(step.subject_template, step.body_template)
                    if not t.startswith("ai.")
                ]
            ),
            ai_variables=[EmailAiVariableRead.model_validate(v) for v in step.ai_variables],
            capabilities=capabilities,
        )

    def campaign_read(
        self,
        campaign: EmailCampaign,
        counts: Mapping[str, int],
        metrics: Mapping[str, int] | None,
        *,
        team_mismatch: bool,
    ) -> EmailCampaignRead:
        steps = list(campaign.steps)
        return EmailCampaignRead(
            id=campaign.id,
            name=campaign.name,
            provider=campaign.provider,
            provider_campaign_id=campaign.provider_campaign_id,
            provider_mode=campaign_provider_mode(campaign.provider_campaign_id),
            status=campaign.status,
            settings=EmailCampaignSettings.model_validate(campaign.settings or {}),
            steps_count=len(steps),
            members_count=sum(n for s, n in counts.items() if s in LIVE),
            members_by_status=dict(counts),
            last_synced_at=campaign.last_synced_at,
            sync_error=campaign.sync_error,
            unsynced_changes=bool(steps)
            and any(
                s.provider_step_id is None or s.provider_synced_hash != s.content_hash
                for s in steps
            ),
            metrics={
                "sent": 0,
                "opened": 0,
                "clicked": 0,
                "replied": 0,
                "bounced": 0,
                **(metrics or {}),
            },
            provider_status=campaign.provider_status,
            last_provider_sync_at=campaign.last_provider_sync_at,
            last_provider_error=campaign.last_provider_error,
            team_mismatch=team_mismatch,
            created_by_agent_id=campaign.created_by_agent_id,
            created_at=campaign.created_at,
            updated_at=campaign.updated_at,
        )

    async def sequence_facts(self, campaign_id: uuid.UUID) -> SequenceFacts:
        rows = await self.repo.membership_progress(campaign_id)
        launched = [queued for _, _, held, queued in rows if held and queued is not None]
        return SequenceFacts(
            in_use=any(held for _, _, held, _ in rows),
            launched_at=min(launched) if launched else None,
            progress=tuple((status, step) for status, step, _, _ in rows),
        )

    async def detail(self, access: Access, campaign: EmailCampaign) -> EmailCampaignDetailRead:
        counts = (await self.repo.status_counts([campaign.id])).get(campaign.id, {})
        metrics = (await self.repo.message_metrics([campaign.id])).get(campaign.id)
        mismatch = await self.team_mismatch(access, campaign)
        base = self.campaign_read(campaign, counts, metrics, team_mismatch=mismatch)
        facts = await self.sequence_facts(campaign.id)
        ordered = sorted(campaign.steps, key=lambda s: s.position)
        return EmailCampaignDetailRead(
            **base.model_dump(),
            steps=[
                self.step_read(
                    step, step_capabilities(step, ordered, facts, team_mismatch=mismatch)
                )
                for step in ordered
            ],
            sequence=sequence_capabilities(campaign.status, facts, team_mismatch=mismatch),
            members_by_step=await self.repo.members_by_step(campaign.id),
            metrics_by_step=await self.repo.message_metrics_by_step(campaign.id),
        )

    async def _reloaded(self, access: Access, campaign_id: uuid.UUID) -> EmailCampaignDetailRead:
        """The campaign's detail read again from the database (after a commit)."""
        return await self.detail(access, await self.campaign_for(access, campaign_id))

    async def list_campaigns(
        self, agent: Agent, statuses: Sequence[str] | None = None
    ) -> list[EmailCampaignRead]:
        """The agency's campaigns, never another agency's, with their leads
        and replies."""
        access = await self.access(agent)
        campaigns = await self.repo.campaigns(access.agency_id, statuses)
        ids = [c.id for c in campaigns]
        counts = await self.repo.status_counts(ids)
        metrics = await self.repo.message_metrics(ids)
        funnels = await self.repo.lead_funnel(ids)
        reads = []
        for campaign in campaigns:
            read = self.campaign_read(
                campaign,
                counts.get(campaign.id, {}),
                metrics.get(campaign.id),
                team_mismatch=await self.team_mismatch(access, campaign),
            )
            funnel = funnels.get(campaign.id)
            if funnel is not None:
                read.lead_stats = EmailCampaignLeadStats(
                    launched=funnel["launched"],
                    reached=funnel["reached"],
                    replied=funnel["replied"],
                )
            reads.append(read)
        return reads

    async def get_campaign(self, agent: Agent, campaign_id: uuid.UUID) -> EmailCampaignDetailRead:
        access = await self.access(agent)
        return await self.detail(access, await self.campaign_for(access, campaign_id))

    async def create_campaign(
        self, agent: Agent, request: EmailCampaignCreate
    ) -> EmailCampaignDetailRead:
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        campaign = EmailCampaign(
            agency_id=access.agency_id,
            name=request.name.strip(),
            provider=PROVIDER_LEMLIST,
            status="DRAFT",
            settings=request.settings.model_dump(),
            created_by_agent_id=access.agent_id,
        )
        self.db.add(campaign)
        await self.db.commit()
        return await self._reloaded(access, campaign.id)

    async def update_campaign(
        self, agent: Agent, campaign_id: uuid.UUID, request: EmailCampaignUpdate
    ) -> EmailCampaignDetailRead:
        """Name, settings, status (spec 8.1). A status change reaches the
        provider when the campaign exists there; at Lemlist for real, it also
        takes `email.sync`."""
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        campaign = await self.campaign_for(access, campaign_id, lock=True)
        await self.writable(access, campaign)
        try:
            if request.name is not None:
                campaign.name = request.name.strip()
            if request.settings is not None:
                campaign.settings = request.settings.model_dump()
            if request.status is not None and request.status != campaign.status:
                await self._transition(access, campaign, request.status)
            await self.db.commit()
        except BaseException:
            await self.db.rollback()
            raise
        return await self._reloaded(access, campaign_id)

    async def _transition(self, access: Access, campaign: EmailCampaign, target: str) -> None:
        if target not in _TRANSITIONS.get(campaign.status, frozenset()):
            raise BadRequestError(
                f"A {campaign.status.lower()} campaign cannot become {target.lower()}.",
                code="email.invalid_transition",
                params={"from": campaign.status, "to": target},
            )
        if target == "ACTIVE" and not campaign.steps:
            raise BadRequestError(
                "Add at least one step before activating the campaign.", code="email.no_steps"
            )
        provider_id = campaign.provider_campaign_id
        if provider_id:
            provider = await self.provider(access)
            if self.through_provider(provider, provider_id):
                # Starting, pausing or archiving a campaign live at Lemlist
                # acts there: that takes email.sync. A dry run stays in Nidria.
                if is_real_provider_id(provider_id):
                    access.require(Permission.EMAIL_SYNC)
                ref = campaign_ref(campaign)
                assert ref is not None
                try:
                    if target == "ACTIVE":
                        mailbox = chosen_mailbox(campaign)
                        await asyncio.to_thread(provider.start_campaign, ref, mailbox=mailbox)
                    elif target == "PAUSED":
                        await asyncio.to_thread(provider.pause_campaign, ref)
                    elif target == "ARCHIVED":
                        await asyncio.to_thread(provider.archive_campaign, ref)
                except ProviderError as exc:
                    raise provider_refusal(exc) from exc
        campaign.status = target

    async def delete_campaign(
        self, agent: Agent, campaign_id: uuid.UUID
    ) -> EmailCampaignDeleteRead:
        """Delete a campaign for good: its steps, AI variables, memberships,
        renders, messages and provider events.

        Its unsubscribed and bounced recipients stay blocked: their
        addresses are on the agency's opposition list (`email_suppression`),
        which only loses the membership they came from. Lemlist's API cannot
        delete a campaign: one live in the current team is paused there
        first (an archived one already is), with `email.sync`, and stays
        there, to delete in Lemlist's app. Another team's campaign goes from
        Nidria only."""
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        campaign = await self.campaign_for(access, campaign_id, lock=True)
        name = campaign.name
        paused = kept = False
        if is_real_provider_id(campaign.provider_campaign_id) and not await self.team_mismatch(
            access, campaign
        ):
            kept = True
            if campaign.status != "ARCHIVED":
                # Pausing a live campaign acts at Lemlist: that takes email.sync.
                access.require(Permission.EMAIL_SYNC)
                provider = await self.provider(access)
                if not provider.external:
                    raise _effects_off()
                ref = campaign_ref(campaign)
                assert ref is not None
                try:
                    await asyncio.to_thread(provider.pause_campaign, ref)
                except ProviderError as exc:
                    raise provider_refusal(exc) from exc
                paused = True
        await self.repo.delete_campaign(campaign.id)
        await self.db.commit()
        return EmailCampaignDeleteRead(
            id=campaign_id, name=name, provider_paused=paused, provider_kept=kept
        )

    async def duplicate_campaign(
        self, agent: Agent, campaign_id: uuid.UUID, name: str | None = None
    ) -> EmailCampaignDetailRead:
        """A draft copy: steps and AI variables, no recipient, nothing at the
        provider. The way to change a sequence once it is in use. `name`: the
        copy's, written by the frontend (decision A13); else the original's."""
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        source = await self.campaign_for(access, campaign_id)
        copy = EmailCampaign(
            agency_id=access.agency_id,
            name=(name.strip() if name and name.strip() else source.name)[:255],
            provider=source.provider,
            status="DRAFT",
            settings=dict(source.settings or {}),
            created_by_agent_id=access.agent_id,
        )
        copy.steps = [_copy_step(step, position=step.position) for step in source.steps]
        self.db.add(copy)
        await self.db.commit()
        return await self._reloaded(access, copy.id)

    # ── steps ──────────────────────────────────────────────────────────────

    async def list_steps(self, agent: Agent, campaign_id: uuid.UUID) -> list[EmailStepRead]:
        access = await self.access(agent)
        campaign = await self.campaign_for(access, campaign_id)
        return [self.step_read(s) for s in sorted(campaign.steps, key=lambda s: s.position)]

    async def step_impact(
        self, agent: Agent, campaign_id: uuid.UUID, position: int | None
    ) -> EmailStepImpactRead:
        """Who a new step at `position` (the end when omitted) would reach."""
        access = await self.access(agent)
        campaign = await self.campaign_for(access, campaign_id)
        count = len(campaign.steps)
        at = count + 1 if position is None else min(position, count + 1)
        return step_impact(at, count, await self.sequence_facts(campaign.id))

    async def create_step(
        self, agent: Agent, campaign_id: uuid.UUID, request: EmailStepCreate
    ) -> EmailCampaignDetailRead:
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        campaign = await self.campaign_for(access, campaign_id, lock=True)
        await self.writable(access, campaign)
        in_use = (await self.sequence_facts(campaign.id)).in_use
        ordered = sorted(campaign.steps, key=lambda s: s.position)
        position = min(request.position or len(ordered) + 1, len(ordered) + 1)
        if in_use and position <= len(ordered):
            raise ConflictError(
                "Once leads are in the campaign, Lemlist only adds a step after the last one.",
                code="email.step_insert_in_use",
            )
        if request.delay_value is not None:
            delay = request.delay_value
        elif not ordered:
            delay = 0
        else:
            delay = EmailCampaignSettings.model_validate(campaign.settings or {}).default_delay_days
        step = EmailCampaignStep(
            campaign_id=campaign.id,
            position=position,
            name=request.name.strip(),
            type="EMAIL",
            delay_value=delay,
            delay_unit=request.delay_unit,
            subject_template=request.subject_template,
            body_template=request.body_template,
            version=1,
        )
        step.ai_variables = []
        step.content_hash = content_hash(step.subject_template, step.body_template, {})
        ordered.insert(position - 1, step)
        _first_subject_required(ordered)
        if not in_use:
            _remap_shells(ordered, _shells(campaign))
        # In use, it goes after the last step and every other step keeps its
        # provider step: the next Sync adds it there (phase 3B).
        campaign.steps.append(step)
        await self.db.commit()
        return await self._reloaded(access, campaign.id)

    async def update_step(
        self, agent: Agent, step_id: uuid.UUID, request: EmailStepUpdate
    ) -> EmailStepRead:
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        step, _campaign = await self._step(access, step_id)
        subject = (
            request.subject_template
            if request.subject_template is not None
            else step.subject_template
        )
        if step.position == 1:
            _require_subject(subject)
        for field_name in (
            "name",
            "delay_value",
            "delay_unit",
            "subject_template",
            "body_template",
        ):
            value = getattr(request, field_name)
            if value is not None:
                setattr(step, field_name, value.strip() if field_name == "name" else value)
        refresh_hash(step)
        await self.db.commit()
        refreshed = await self.repo.step(access.agency_id, step.id)
        assert refreshed is not None
        return self.step_read(refreshed)

    async def delete_step(self, agent: Agent, step_id: uuid.UUID) -> EmailCampaignDetailRead:
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        step, campaign = await self._step(access, step_id)
        campaign_id = campaign.id
        facts = await self.sequence_facts(campaign_id)
        try:
            if facts.in_use:
                await self._drop_in_use(access, step, campaign, facts)
            else:
                await self._drop_free(access, step, campaign)
            await self.db.commit()
        except BaseException:
            await self.db.rollback()
            raise
        return await self._reloaded(access, campaign_id)

    async def _drop_free(
        self, access: Access, step: EmailCampaignStep, campaign: EmailCampaign
    ) -> None:
        """Remove a step while no lead is at the provider: the provider steps
        are remapped by position and the one left over is deleted there."""
        remaining = [s for s in sorted(campaign.steps, key=lambda s: s.position) if s.id != step.id]
        _first_subject_required(remaining)
        surplus = _remap_shells(remaining, _shells(campaign))
        ref = campaign_ref(campaign)
        if ref is not None and surplus:
            provider = await self.provider(access)
            for shell in surplus:
                if self.through_provider(provider, shell):
                    try:
                        await asyncio.to_thread(provider.delete_step, ref, shell)
                    except ProviderError as exc:
                        raise provider_refusal(exc) from exc
        campaign.steps.remove(step)
        await self.db.delete(step)

    async def _drop_in_use(
        self,
        access: Access,
        step: EmailCampaignStep,
        campaign: EmailCampaign,
        facts: SequenceFacts,
    ) -> None:
        """Remove a step of a sequence in use: freely while the provider does
        not have it yet; otherwise only its last step nobody got, parked
        there (Lemlist removes no step once leads are in). The parked step
        stays at the provider, out of reach, and the next follow-up's Sync
        reuses it (phase 3B). Recipients waiting for it have finished."""
        steps = sorted(campaign.steps, key=lambda s: s.position)
        capabilities = step_capabilities(step, steps, facts)
        if not capabilities.can_delete:
            raise ConflictError(
                "Lemlist cannot remove this step now.",
                code="email.step_delete_in_use",
                params={"reason": capabilities.delete_reason or "not_last_at_provider"},
            )
        _first_subject_required([s for s in steps if s.id != step.id])
        if step.provider_step_id:
            ref = campaign_ref(campaign)
            if ref is not None:
                provider = await self.provider(access)
                if self.through_provider(provider, step.provider_step_id):
                    if is_real_provider_id(step.provider_step_id):
                        access.require(Permission.EMAIL_SYNC)
                    try:
                        await asyncio.to_thread(provider.park_step, ref, step.provider_step_id)
                    except ProviderError as exc:
                        raise provider_refusal(exc) from exc
            # Recipients waiting for it have finished: nothing comes after.
            await self.repo.complete_waiting(campaign.id, step.position - 1)
        campaign.steps.remove(step)
        await self.db.delete(step)
        for later in steps:
            if later.position > step.position:
                later.position -= 1

    async def duplicate_step(
        self, agent: Agent, step_id: uuid.UUID, name: str | None = None
    ) -> EmailCampaignDetailRead:
        """A copy of the step right after it; at the end once the sequence is
        in use (the provider adds nowhere else). `name`: the copy's
        (decision A13); else the original's."""
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        step, campaign = await self._step(access, step_id)
        in_use = (await self.sequence_facts(campaign.id)).in_use
        ordered = sorted(campaign.steps, key=lambda s: s.position)
        copy = _copy_step(
            step,
            position=len(ordered) + 1 if in_use else step.position + 1,
            name=name,
        )
        copy.campaign_id = campaign.id
        if in_use:
            ordered.append(copy)
        else:
            ordered.insert(step.position, copy)
            _remap_shells(ordered, _shells(campaign))
        campaign.steps.append(copy)
        await self.db.commit()
        return await self._reloaded(access, campaign.id)

    async def reorder_steps(
        self, agent: Agent, campaign_id: uuid.UUID, request: EmailStepReorder
    ) -> EmailCampaignDetailRead:
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        campaign = await self.campaign_for(access, campaign_id, lock=True)
        steps = {s.id: s for s in campaign.steps}
        if len(request.step_ids) != len(steps) or set(request.step_ids) != set(steps):
            raise BadRequestError(
                "The new order must list every step of the campaign exactly once.",
                code="email.invalid_step_order",
            )
        if [s.id for s in sorted(campaign.steps, key=lambda s: s.position)] == request.step_ids:
            return await self.detail(access, campaign)
        await self.writable(access, campaign)
        if (await self.sequence_facts(campaign.id)).in_use:
            raise ConflictError(
                "Steps cannot be reordered once leads are in the campaign.",
                code="email.sequence_in_use",
            )
        ordered = [steps[i] for i in request.step_ids]
        _first_subject_required(ordered)
        try:
            _remap_shells(ordered, _shells(campaign))
            ref = campaign_ref(campaign)
            if ref is not None:
                provider = await self.provider(access)
                if self.through_provider(provider, campaign.provider_campaign_id):
                    await asyncio.to_thread(
                        provider.reorder_steps,
                        ref,
                        [
                            (
                                s.provider_step_id,
                                StepSpec(
                                    position=s.position, name=s.name, delay_days=delay_days(s)
                                ),
                            )
                            for s in ordered
                            if s.provider_step_id
                        ],
                    )
            await self.db.commit()
        except ProviderError as exc:
            await self.db.rollback()
            raise provider_refusal(exc) from exc
        except BaseException:
            await self.db.rollback()
            raise
        return await self._reloaded(access, campaign_id)

    # ── AI variables ───────────────────────────────────────────────────────

    async def _ai_variable(
        self, access: Access, variable_id: uuid.UUID
    ) -> tuple[EmailCampaignAiVariable, EmailCampaignStep]:
        found = await self.repo.ai_variable(access.agency_id, variable_id)
        if found is None:
            raise NotFoundError("AI variable not found.", code="email.ai_variable_not_found")
        step, _campaign = await self._step(access, found.campaign_step_id)
        return next(v for v in step.ai_variables if v.id == variable_id), step

    async def _step_after(self, access: Access, step: EmailCampaignStep) -> EmailStepRead:
        refresh_hash(step)
        await self.db.commit()
        refreshed = await self.repo.step(access.agency_id, step.id)
        assert refreshed is not None
        return self.step_read(refreshed)

    async def create_ai_variable(
        self, agent: Agent, step_id: uuid.UUID, request: EmailAiVariableCreate
    ) -> EmailStepRead:
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        step, _campaign = await self._step(access, step_id)
        key = request.key.strip()
        _check_ai_key(key)
        if any(v.key == key for v in step.ai_variables):
            raise ConflictError(
                "This AI variable already exists on this step.",
                code="email.ai_variable_exists",
                params={"key": key},
            )
        _check_prompt(request.prompt_template)
        step.ai_variables.append(
            EmailCampaignAiVariable(
                key=key, prompt_template=request.prompt_template, model=request.model, config={}
            )
        )
        return await self._step_after(access, step)

    async def update_ai_variable(
        self, agent: Agent, variable_id: uuid.UUID, request: EmailAiVariableUpdate
    ) -> EmailStepRead:
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        target, step = await self._ai_variable(access, variable_id)
        key = request.key.strip() if request.key is not None else None
        if key is not None:
            _check_ai_key(key)
            if any(v.key == key and v.id != variable_id for v in step.ai_variables):
                raise ConflictError(
                    "This AI variable already exists on this step.",
                    code="email.ai_variable_exists",
                    params={"key": key},
                )
        if request.prompt_template is not None:
            _check_prompt(request.prompt_template)
        if key is not None:
            target.key = key
        if request.prompt_template is not None:
            target.prompt_template = request.prompt_template
        if request.model is not None:
            target.model = request.model or None
        return await self._step_after(access, step)

    async def delete_ai_variable(self, agent: Agent, variable_id: uuid.UUID) -> EmailStepRead:
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        _target, step = await self._ai_variable(access, variable_id)
        step.ai_variables = [v for v in step.ai_variables if v.id != variable_id]
        return await self._step_after(access, step)

    # ── previews and AI ────────────────────────────────────────────────────

    def ai_configured(self) -> bool:
        """Whether AI values can be generated now (phase 5's seam, decision
        A14, D3 open): not yet. While they cannot, an AI passage still to
        write blocks its client at the Launch page (readiness AI_AT_LAUNCH)
        exactly as the Launch refuses it."""
        return False

    async def generate_ai_values(
        self,
        access: Access,
        profile: ClientProfile,
        people: People,
        variables: Sequence[tuple[str, str, str | None]],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Generate the AI values (key, prompt, model) for one client:
        (values, key -> error code). Not configured in this phase (decision
        A14, D3 open): 409 `email.ai_not_configured`, never a fallback."""
        raise ConflictError(
            "AI generation is not configured for the Email module.",
            code="email.ai_not_configured",
        )

    async def ai_test(self, agent: Agent, request: EmailAiTestRequest) -> EmailAiTestResponse:
        """The prompt resolved for one client and the model's answer. The
        client is the agency's (404 otherwise) and the prompt reads no AI
        variable (400); then 409 `email.ai_not_configured` (decision A14)."""
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        profile = await self.client_profile(access, request.client_profile_id)
        key = request.key.strip()
        _check_ai_key(key)
        _check_prompt(request.prompt_template)
        people = await self.people(access, [profile])
        # The model sees the resolved prompt only; an address it does not need.
        resolved = EmailTemplateRenderer(require_ai=False).render(
            "", request.prompt_template, people.context(profile, profile.expat_user)
        )
        errors = [e for e in _render_errors(resolved) if e.kind != "no_email"]
        values, failures = await self.generate_ai_values(
            access, profile, people, [(key, request.prompt_template, request.model)]
        )
        return EmailAiTestResponse(
            input_resolved=resolved.body,
            output=values.get(key),
            model=request.model if key in values else None,
            errors=errors,
            error=failures.get(key),
        )

    async def preview(self, agent: Agent, request: EmailPreviewRequest) -> EmailPreviewResponse:
        """Render the editor's content for one real client: client, company
        and agency values always; AI values only as generated. An AI value
        not generated is the error `ai_not_generated`, never a fallback.

        A live recipient of the campaign, previewed on the saved step: the AI
        values its email will carry are the frozen ones, and generating here
        freezes them, so preview and send never differ (Prism's rule)."""
        access = await self.access(agent, Permission.EMAIL_MANAGE)
        profile = await self.client_profile(access, request.client_profile_id)
        account = profile.expat_user
        people = await self.people(access, [profile])
        ai_values: dict[str, str] = {}
        ai_errors: dict[str, str] = {}
        frozen: EmailMemberRender | None = None
        member: EmailCampaignMember | None = None
        step: EmailCampaignStep | None = None
        prompt_by_key = {v.key: v for v in request.ai_variables}
        if request.step_id is not None:
            step = await self.repo.step(access.agency_id, request.step_id)
            if step is None:
                raise NotFoundError("Step not found.", code="email.step_not_found")
            prompts = {v.key: f"{v.prompt_template}|{v.model or ''}" for v in request.ai_variables}
            same_content = (
                content_hash(request.subject_template, request.body_template, prompts)
                == step.content_hash
            )
            live = await self.repo.live_members(access.agency_id, [profile.id])
            member = next((m for m in live if m.campaign_id == step.campaign_id), None)
            if member is not None and same_content:
                frozen = (await self.repo.renders(member.id)).get(step.position)
            if not same_content:
                member = None
        if frozen is not None:
            for key, entry in (frozen.ai_values or {}).items():
                variable = prompt_by_key.get(key)
                if (
                    variable is not None
                    and isinstance(entry, dict)
                    and entry.get("prompt_hash")
                    == prompt_hash(variable.prompt_template, variable.model)
                ):
                    ai_values[key] = str(entry.get("value"))
        if request.generate_ai:
            missing = [
                (v.key, v.prompt_template, v.model)
                for v in request.ai_variables
                if v.key not in ai_values
            ]
            if missing:
                generated, ai_errors = await self.generate_ai_values(
                    access, profile, people, missing
                )
                ai_values.update(generated)
        else:
            # What this editor generated earlier, shown again while the agent
            # types; never frozen (only a generation here freezes).
            for variable in request.ai_variables:
                echoed = request.ai_values.get(variable.key)
                if variable.key not in ai_values and echoed and echoed.strip():
                    ai_values[variable.key] = echoed.strip()
        result = EmailTemplateRenderer(require_ai=True).render(
            request.subject_template,
            request.body_template,
            people.context(profile, account, ai_values, [v.key for v in request.ai_variables]),
        )
        if request.generate_ai and member is not None and step is not None and not ai_errors:
            await self._freeze_preview(member, step, ai_values, request, result)
        # The same checks the hand-over applies (readiness.render_errors).
        position = step.position if step is not None else request.position
        checks: list[EmailRenderErrorRead] = []
        if position == 1 and not result.subject.strip():
            checks.append(EmailRenderErrorRead(kind="first_subject_required"))
        if not result.body.strip():
            checks.append(EmailRenderErrorRead(kind="empty_body"))
        identity = client_identity(profile, account)
        company = people.company(profile)
        return EmailPreviewResponse(
            client=EmailPreviewClient(
                id=profile.id,
                first_name=identity.first_name,
                last_name=identity.last_name,
                full_name=_full_name(identity.first_name, identity.last_name),
                email=identity.email,
                company_id=company.id if company is not None else None,
                company_name=company.name if company is not None else None,
            ),
            subject=result.subject,
            body=result.body,
            variables=[
                EmailResolvedVariableRead(
                    token=v.token, value=v.value, source=v.source, fallback=v.fallback
                )
                for v in result.variables
            ],
            errors=_render_errors(result) + checks,
            ai_values=ai_values,
            ai_errors=ai_errors,
            sendable=result.ok and not ai_errors and not checks,
        )

    async def _freeze_preview(
        self,
        member: EmailCampaignMember,
        step: EmailCampaignStep,
        ai_values: dict[str, str],
        request: EmailPreviewRequest,
        result: RenderResult,
    ) -> None:
        """Keep the generated AI values in the recipient's render of the step,
        with the prompt they came from: the hand-over sends exactly them. A
        render already sent never changes (and the database refuses it)."""
        renders = await self.repo.renders(member.id)
        existing = renders.get(step.position)
        if existing is not None and existing.sent_at is not None:
            return
        stored = {
            v.key: {
                "value": ai_values[v.key],
                "prompt_hash": prompt_hash(v.prompt_template, v.model),
            }
            for v in request.ai_variables
            if v.key in ai_values
        }
        if existing is None:
            existing = EmailMemberRender(
                campaign_member_id=member.id,
                step_position=step.position,
                template_version=step.version,
            )
            self.db.add(existing)
        existing.campaign_step_id = step.id
        existing.subject = result.subject
        existing.body = result.body
        existing.resolved_variables = result.resolved_map()
        existing.ai_values = stored
        existing.template_version = step.version
        existing.template_hash = step.content_hash
        existing.rendered_at = datetime.now(UTC)
        await self.db.commit()

    # ── recipients ─────────────────────────────────────────────────────────

    def member_read(
        self,
        row: MemberRow,
        people: People,
        steps_count: Mapping[uuid.UUID, int],
        issues: Sequence[Issue] = (),
    ) -> EmailMemberRead:
        member, profile, campaign = row.member, row.profile, row.campaign
        identity = client_identity(profile, row.account)
        company = people.company(profile)
        return EmailMemberRead(
            id=member.id,
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            client_profile_id=profile.id,
            first_name=identity.first_name,
            last_name=identity.last_name,
            full_name=_full_name(identity.first_name, identity.last_name),
            email=identity.email,
            company_id=company.id if company is not None else None,
            company_name=company.name if company is not None else None,
            client_status=people.client_status(profile),
            status=member.status,
            status_reason=member.status_reason,
            status_reason_params=status_reason_params(member),
            current_step=member.current_step,
            steps_count=steps_count.get(campaign.id, 0),
            last_event_type=member.last_event_type,
            last_event_at=member.last_event_at,
            last_sent_at=member.last_sent_at,
            last_opened_at=member.last_opened_at,
            last_clicked_at=member.last_clicked_at,
            last_replied_at=member.last_replied_at,
            added_at=member.added_at,
            updated_at=member.updated_at,
            queued_at=member.queued_at,
            started_at=member.started_at,
            paused_at=member.paused_at,
            removed_at=member.removed_at,
            issues=[_issue_read(i) for i in issues],
            provider_lead_id=member.provider_lead_id,
            provider_status=member.provider_status,
            dry_run=is_dry_run_id(member.provider_lead_id),
            added_by_agent_id=member.added_by_agent_id,
            paused_by_agent_id=member.paused_by_agent_id,
            removed_by_agent_id=member.removed_by_agent_id,
            moved_to_id=member.moved_to_id,
        )

    async def list_members(
        self, agent: Agent, filters: dict[str, Any], page: int, page_size: int
    ) -> EmailMemberListResponse:
        """The agency's memberships, with the Leads list's filters. A lead
        still to launch carries what stands between it and a launch: the
        shared rules (readiness), the very ones the hand-over applies."""
        access = await self.access(agent)
        rows, total, status_counts = await self.repo.members_page(
            access.agency_id, filters, page, page_size
        )
        people = await self.people(access, [r.profile for r in rows])
        steps_count = await self.repo.step_counts(sorted({r.campaign.id for r in rows}))
        waiting = [r for r in rows if not_launched(r.member)]
        issues: dict[uuid.UUID, list[Issue]] = {}
        if waiting:
            facts = await self.client_facts(access, waiting)
            for r in waiting:
                email = client_identity(r.profile, r.account).email
                issues[r.member.id] = membership_issues(r.member, email, facts)
        return EmailMemberListResponse(
            items=[
                self.member_read(r, people, steps_count, issues.get(r.member.id, ())) for r in rows
            ],
            total=total,
            page=page,
            page_size=page_size,
            status_counts=status_counts,
        )

    async def member_detail(self, agent: Agent, member_id: uuid.UUID) -> EmailMemberDetail:
        """One membership in full, read on demand: per step, the exact email
        it was or will be sent (its frozen render, AI values included), its
        estimated day, what came of it and the replies it got; the same
        emails as one conversation; every provider event; and, still to
        launch, what stands in its way."""
        access = await self.access(agent)
        row = await self.repo.member_row(access.agency_id, member_id)
        if row is None:
            raise NotFoundError("Recipient not found.", code="email.member_not_found")
        member, profile = row.member, row.profile
        campaign = await self.campaign_for(access, member.campaign_id)
        people = await self.people(access, [profile])
        steps = {s.position: s for s in campaign.steps}
        renders = await self.repo.renders(member.id)
        messages = await self.repo.messages_of(member.id)
        events = [_event_read(e) for e in await self.repo.events_of(member.id)]
        outbound = {
            m.step_position: m
            for m in messages
            if m.direction == "OUTBOUND" and m.step_position is not None
        }
        replies = [_reply_read(m) for m in messages if m.direction == "INBOUND"]
        settings = EmailCampaignSettings.model_validate(campaign.settings or {})
        positions = set(steps) | set(renders) | set(outbound)
        sent: dict[int, datetime] = {}
        for position in positions:
            at = outbound[position].sent_at if position in outbound else None
            if at is None and position in renders:
                at = renders[position].sent_at
            if at is not None:
                sent[position] = at
        plan = schedule(
            member,
            list(steps.values()),
            sent,
            campaign_status=campaign.status,
            timezone=settings.timezone,
            sending_days=settings.sending_days,
            sending_end=settings.sending_end_time,
        )
        last_position = max(steps, default=0)
        timeline: list[EmailStepTimeline] = []
        for position in sorted(positions):
            render = renders.get(position)
            step = steps.get(position)
            message = outbound.get(position)
            status: str | None
            if message is not None:
                status = message.status
            elif render is not None and render.provider_synced_at is not None:
                status = "QUEUED"
            else:
                status = None
            planned = plan.steps.get(position)
            timeline.append(
                EmailStepTimeline(
                    position=position,
                    step_id=step.id if step else None,
                    step_name=step.name if step else None,
                    delay_days=delay_days(step) if step else None,
                    planned_on=planned.day if planned is not None and planned.estimated else None,
                    planned_overdue=bool(planned and planned.overdue),
                    status=status,
                    subject=render.subject if render else None,
                    body=render.body if render else None,
                    ai_values={
                        key: str(entry.get("value"))
                        for key, entry in (render.ai_values or {}).items()
                        if isinstance(entry, dict) and entry.get("value") is not None
                    }
                    if render
                    else {},
                    template_version=render.template_version if render else None,
                    outdated=bool(
                        render
                        and step
                        and render.sent_at is None
                        and (
                            render.template_hash != step.content_hash
                            or render.campaign_step_id != step.id
                        )
                    ),
                    rendered_at=render.rendered_at if render else None,
                    provider_synced_at=render.provider_synced_at if render else None,
                    sent_at=(message.sent_at if message else None)
                    or (render.sent_at if render else None),
                    open_count=message.open_count if message else 0,
                    click_count=message.click_count if message else 0,
                    first_opened_at=message.first_opened_at if message else None,
                    last_opened_at=message.last_opened_at if message else None,
                    last_clicked_at=message.last_clicked_at if message else None,
                    replied_at=message.replied_at if message else None,
                    bounced_at=message.bounced_at if message else None,
                    events=[e for e in events if e.step_position == position],
                    # A reply the provider tied to no step goes under the last
                    # email sent, where the conversation stood.
                    replies=[
                        r
                        for r in replies
                        if r.step_position == position
                        or (
                            r.step_position is None
                            and position == (member.current_step or last_position)
                        )
                    ],
                )
            )
        conversation: list[EmailConversationItem] = [
            EmailConversationItem(
                direction="OUTBOUND",
                at=item.sent_at,
                step_position=item.position,
                subject=item.subject,
                body=item.body,
                status=item.status,
                content_state="RENDERED",
                open_count=item.open_count,
                click_count=item.click_count,
            )
            for item in timeline
            if item.sent_at is not None
        ]
        conversation.extend(
            EmailConversationItem(
                direction="INBOUND",
                at=reply.received_at,
                step_position=reply.step_position,
                subject=reply.subject,
                body=reply.body_text,
                status="REPLIED",
                content_state=reply.content_state,
            )
            for reply in replies
            if reply.received_at is not None
        )
        conversation.sort(key=lambda c: c.at)
        issues: list[Issue] = []
        if not_launched(member):
            facts = await self.client_facts(access, [row])
            email = client_identity(profile, row.account).email
            ordered = sorted(campaign.steps, key=lambda s: s.position)
            issues = membership_issues(member, email, facts)
            issues += template_issues(
                ordered,
                renders,
                people.context_for(profile, row.account),
                ai_configured=self.ai_configured(),
            ).issues
        base = self.member_read(row, people, {campaign.id: len(campaign.steps)}, issues)
        return EmailMemberDetail(
            **base.model_dump(),
            steps=timeline,
            conversation=conversation,
            events=events,
            schedule_hold=(
                EmailScheduleHoldRead(code=plan.hold.code, params=dict(plan.hold.params))
                if plan.hold is not None
                else None
            ),
        )

    # ── bulk actions on recipients ─────────────────────────────────────────

    async def _owned_rows(
        self, access: Access, ids: Sequence[uuid.UUID], permission: Permission
    ) -> tuple[list[MemberRow], list[EmailActionResultRow]]:
        """The agency's memberships among `ids` the agent may act on, and a
        failed row for each other one: `email.not_found` (unknown, or another
        agency's) or `permission.denied` (checked row by row, whatever the
        route's binding says)."""
        unique = list(dict.fromkeys(ids))
        rows = await self.repo.member_rows(access.agency_id, unique)
        found = {r.member.id for r in rows}
        results = [
            EmailActionResultRow(id=member_id, ok=False, code="email.not_found")
            for member_id in unique
            if member_id not in found
        ]
        allowed: list[MemberRow] = []
        for row in rows:
            if not access.can(permission):
                results.append(
                    _row_result(
                        row.member,
                        ok=False,
                        code="permission.denied",
                        params={"permission": permission.value},
                    )
                )
            else:
                allowed.append(row)
        return allowed, results

    async def _lead_at_provider(
        self,
        access: Access,
        row: MemberRow,
        call: Callable[[EmailProvider, CampaignRef, LeadRef], None],
    ) -> RowFailure | None:
        """Apply `call` to the membership's lead at the provider, when it has
        one there; the failure as a row's (code, params), never raised."""
        ref = campaign_ref(row.campaign)
        if ref is None or not row.member.provider_lead_id:
            return None
        try:
            provider = await self.provider(access)
            if self.through_provider(provider, row.member.provider_lead_id):
                await asyncio.to_thread(call, provider, ref, lead_ref(row.member))
        except ProviderError as exc:
            return provider_error_code(exc), provider_params(exc)
        except ConflictError as exc:
            return exc.code, dict(exc.params)
        return None

    async def pause(self, agent: Agent, ids: Sequence[uuid.UUID]) -> EmailActionResult:
        """Pause each recipient, at Lemlist too. A manual pause names its
        agent (`paused_by_agent_id`): the sync never resumes it."""
        access = await self.access(agent)
        rows, results = await self._owned_rows(access, ids, Permission.EMAIL_PAUSE)
        now = datetime.now(UTC)
        for row in rows:
            member = row.member
            if await self.team_mismatch(access, row.campaign):
                results.append(_row_result(member, ok=False, code="email.team_mismatch"))
                continue
            if member.status == "EMAIL_PAUSED":
                results.append(_row_result(member, ok=True))
                continue
            if member.status not in ("EMAIL_READY", "EMAIL_QUEUED", "EMAIL_ACTIVE"):
                results.append(
                    _row_result(
                        member,
                        ok=False,
                        code="email.member_not_pausable",
                        params={"status": member.status},
                    )
                )
                continue
            failure = await self._lead_at_provider(
                access, row, lambda p, ref, lead: p.pause_contact(ref, lead)
            )
            if failure is not None:
                results.append(_row_result(member, ok=False, code=failure[0], params=failure[1]))
                continue
            member.status = "EMAIL_PAUSED"
            member.paused_at = now
            member.paused_by_agent_id = access.agent_id
            set_status_reason(member, None)
            results.append(_row_result(member, ok=True))
        await self.db.commit()
        return _summary(ids, results)

    async def _unrendered_step(
        self,
        access: Access,
        member: EmailCampaignMember,
        campaign_id: uuid.UUID,
        steps_of: dict[uuid.UUID, list[EmailCampaignStep]],
    ) -> int | None:
        """The first step at the provider this recipient can still reach
        without its own email there, if any."""
        if campaign_id not in steps_of:
            loaded = await self.repo.campaign(access.agency_id, campaign_id)
            steps_of[campaign_id] = sorted(
                loaded.steps if loaded is not None else [], key=lambda s: s.position
            )
        renders = await self.repo.renders(member.id)
        for step in steps_of[campaign_id]:
            if step.position <= (member.current_step or 0) or not step.provider_step_id:
                continue
            render = renders.get(step.position)
            if (
                render is None
                or render.provider_synced_at is None
                or render.campaign_step_id != step.id
            ):
                return step.position
        return None

    async def resume(self, agent: Agent, ids: Sequence[uuid.UUID]) -> EmailActionResult:
        """Resume each paused recipient, at Lemlist too. Refused while the
        provider could reach one of its steps without its own email there:
        it would send the step's placeholder (the sync renders it first)."""
        access = await self.access(agent)
        rows, results = await self._owned_rows(access, ids, Permission.EMAIL_PAUSE)
        steps_of: dict[uuid.UUID, list[EmailCampaignStep]] = {}
        for row in rows:
            member = row.member
            if await self.team_mismatch(access, row.campaign):
                results.append(_row_result(member, ok=False, code="email.team_mismatch"))
                continue
            if member.status in ("EMAIL_READY", "EMAIL_QUEUED", "EMAIL_ACTIVE"):
                results.append(_row_result(member, ok=True))
                continue
            if member.status != "EMAIL_PAUSED":
                results.append(
                    _row_result(
                        member,
                        ok=False,
                        code="email.member_not_resumable",
                        params={"status": member.status},
                    )
                )
                continue
            if member.provider_lead_id:
                missing = await self._unrendered_step(access, member, row.campaign.id, steps_of)
                if missing is not None:
                    results.append(
                        _row_result(
                            member,
                            ok=False,
                            code="email.member_needs_render",
                            params={"position": missing},
                        )
                    )
                    continue
            failure = await self._lead_at_provider(
                access, row, lambda p, ref, lead: p.resume_contact(ref, lead)
            )
            if failure is not None:
                results.append(_row_result(member, ok=False, code=failure[0], params=failure[1]))
                continue
            if member.provider_lead_id:
                member.status = "EMAIL_ACTIVE" if member.started_at else "EMAIL_QUEUED"
            else:
                member.status = "EMAIL_READY"
            member.paused_at = None
            member.paused_by_agent_id = None
            set_status_reason(member, None)
            results.append(_row_result(member, ok=True))
        await self.db.commit()
        return _summary(ids, results)

    async def _end(
        self,
        access: Access,
        row: MemberRow,
        reason: str,
        params: Mapping[str, Any] | None = None,
    ) -> RowFailure | None:
        """Take the recipient out of its campaign; the membership is kept.

        At Lemlist too, an ended lead included (replied, completed, bounced,
        unsubscribed): Lemlist refuses an address that is still a lead of
        another campaign, even an ended one, so a lead left there could never
        join another campaign (spec 8.4). Left alone: a membership already
        ended, and another team's lead (only Nidria's membership ends).
        An unsubscribed or bounced membership keeps its status (decision A1):
        the block outlives the removal."""
        member = row.member
        if member.removed_at is None and not await self.team_mismatch(access, row.campaign):
            failure = await self._lead_at_provider(
                access, row, lambda p, ref, lead: p.remove_contact(ref, lead)
            )
            if failure is not None:
                return failure
        if member.status not in BLOCKING:
            member.status = "EMAIL_REMOVED"
        set_status_reason(member, reason, params)
        member.removed_at = datetime.now(UTC)
        member.removed_by_agent_id = access.agent_id
        return None

    async def remove(self, agent: Agent, ids: Sequence[uuid.UUID]) -> EmailActionResult:
        """End each membership (kept, for the history); Lemlist removes the
        lead. Removing twice changes nothing."""
        access = await self.access(agent)
        rows, results = await self._owned_rows(access, ids, Permission.EMAIL_REMOVE)
        for row in rows:
            member = row.member
            if member.removed_at is not None:
                results.append(_row_result(member, ok=True))
                continue
            failure = await self._end(access, row, REASON_REMOVED)
            if failure is not None:
                results.append(_row_result(member, ok=False, code=failure[0], params=failure[1]))
                continue
            results.append(_row_result(member, ok=True))
        await self.db.commit()
        return _summary(ids, results)

    async def move(
        self, agent: Agent, ids: Sequence[uuid.UUID], target_campaign_id: uuid.UUID
    ) -> EmailActionResult:
        """End each membership and open a new, READY one in the target
        campaign: moving is enrolling elsewhere (`email.enroll`), on the
        target and on every row. Progress is not carried over."""
        access = await self.access(agent)
        target = await self.campaign_for(access, target_campaign_id)
        access.require(Permission.EMAIL_ENROLL)
        await self.writable(access, target)
        if target.status == "ARCHIVED":
            raise BadRequestError(
                "Recipients cannot move into an archived campaign.",
                code="email.campaign_archived",
            )
        rows, results = await self._owned_rows(access, ids, Permission.EMAIL_ENROLL)
        facts = await self.client_facts(access, rows)
        for row in rows:
            results.append(await self._move_one(access, row, target, facts))
        await self.db.commit()
        return _summary(ids, results)

    async def _move_one(
        self, access: Access, row: MemberRow, target: EmailCampaign, facts: ClientFacts
    ) -> EmailActionResultRow:
        member, profile = row.member, row.profile
        if row.campaign.id == target.id:
            return _row_result(member, ok=False, code="email.same_campaign")
        # Moving is enrolling elsewhere: never someone blocked for good (this
        # membership, another of the person's, or its address on the list).
        blocked = durable_block(profile.id, client_identity(profile, row.account).email, facts)
        if blocked is not None:
            return _row_result(
                member, ok=False, code="email.member_blocked", params={"status": blocked}
            )
        if await self.repo.open_in_campaign(target.id, [profile.id]):
            return _row_result(member, ok=False, code="email.already_in_campaign")
        if member.status not in LIVE:
            # An ended membership while the person lives in another campaign
            # (decision A11): one live membership per person, never two.
            live = [
                m
                for m in await self.repo.live_members(access.agency_id, [profile.id])
                if m.id != member.id
            ]
            if live:
                return _row_result(
                    member,
                    ok=False,
                    code="email.in_other_campaign",
                    params={"campaign_id": str(live[0].campaign_id)},
                )
        failure = await self._end(
            access,
            row,
            REASON_MOVED,
            {"campaign_id": str(target.id), "campaign_name": target.name},
        )
        if failure is not None:
            return _row_result(member, ok=False, code=failure[0], params=failure[1])
        await self.db.flush()
        new = EmailCampaignMember(
            campaign_id=target.id,
            client_profile_id=profile.id,
            agency_id=access.agency_id,
            status="EMAIL_READY",
            added_by_agent_id=access.agent_id,
        )
        self.db.add(new)
        await self.db.flush()
        member.moved_to_id = new.id
        return _row_result(member, ok=True, new_id=new.id)

    # ── the Launch page (route 13) ─────────────────────────────────────────

    async def launch_readiness(
        self,
        agent: Agent,
        campaign_id: uuid.UUID,
        *,
        search: str | None,
        readiness: LaunchReadinessFilter,
        page: int,
        page_size: int,
    ) -> EmailLaunchReadiness:
        """The Launch page: the campaign's gates, today's room under its
        daily limit, and every lead still to launch, READY or BLOCKED by the
        shared rules (readiness), the very ones the hand-over applies. A
        read: no lead is asked of the provider, no AI value is generated;
        renders already frozen (a member's preview) are counted, never made."""
        access = await self.access(agent)
        campaign = await self.campaign_for(access, campaign_id)
        steps = sorted(campaign.steps, key=lambda s: s.position)
        settings = EmailCampaignSettings.model_validate(campaign.settings or {})
        provider = await self.provider(access)
        gates = campaign_gates(
            campaign,
            steps,
            external=provider.external,
            key_team_id=await self.key_team_id(access),
            launching=True,
        )
        launched_today = await self.repo.queued_since(campaign.id, day_start(settings.timezone))
        remaining_today = max(settings.daily_limit - launched_today, 0)
        for extra in (
            daily_limit_gate(launched_today, settings.daily_limit),
            sender_gate(settings.sender),
        ):
            if extra is not None:
                gates.append(extra)
        rows = await self.repo.to_launch(access.agency_id, campaign.id, search)
        facts = await self.client_facts(access, rows)
        renders = await self.repo.renders_for([r.member.id for r in rows])
        people = await self.people(access, [r.profile for r in rows])
        checked: list[tuple[MemberRow, list[Issue], TemplateCheck]] = []
        for row in rows:
            email = client_identity(row.profile, row.account).email
            check = template_issues(
                steps,
                renders.get(row.member.id, {}),
                people.context_for(row.profile, row.account),
                ai_configured=self.ai_configured(),
            )
            issues = membership_issues(row.member, email, facts) + check.issues
            checked.append((row, issues, check))
        ready_rows = [entry for entry in checked if not any(i.blocking for i in entry[1])]
        shown = (
            checked
            if readiness == "all"
            else ready_rows
            if readiness == "ready"
            else [entry for entry in checked if any(i.blocking for i in entry[1])]
        )
        steps_count = {campaign.id: len(steps)}
        items = [
            EmailLaunchReadinessRow(
                lead=self.member_read(row, people, steps_count, issues),
                ready=not any(i.blocking for i in issues),
                issues=[_issue_read(i) for i in issues],
                steps_rendered=check.rendered,
                steps_total=check.total,
            )
            for row, issues, check in shown[(page - 1) * page_size : page * page_size]
        ]
        return EmailLaunchReadiness(
            campaign_id=campaign.id,
            gates=[_issue_read(g) for g in gates],
            can_launch=not any(g.blocking for g in gates),
            daily_limit=settings.daily_limit,
            launched_today=launched_today,
            remaining_today=remaining_today,
            to_launch=len(checked),
            ready=len(ready_rows),
            blocked=len(checked) - len(ready_rows),
            launched=await self.repo.launched_count(campaign.id),
            ready_ids=[
                row.member.id for row, _, _ in ready_rows if row.member.status == "EMAIL_READY"
            ][:READY_IDS_CAP],
            items=items,
            total=len(shown),
            page=page,
            page_size=page_size,
        )

    # ── performance (route 12) ─────────────────────────────────────────────

    async def performance(
        self,
        agent: Agent,
        campaign_id: uuid.UUID,
        launched_from: date | None = None,
        launched_to: date | None = None,
    ) -> EmailPerformanceRead:
        """The campaign's performance, computed in SQL from Nidria's records:
        leads (memberships) and messages never mixed; rates over the leads
        reached, None without any; with a period, the cohort of leads
        launched in it (days in the campaign's timezone). Nothing is read
        from the provider."""
        access = await self.access(agent)
        campaign = await self.campaign_for(access, campaign_id)
        settings = EmailCampaignSettings.model_validate(campaign.settings or {})
        zone = zone_of(settings.timezone)
        since = (
            datetime.combine(launched_from, time.min, tzinfo=zone).astimezone(UTC)
            if launched_from
            else None
        )
        until = (
            datetime.combine(launched_to + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
            if launched_to
            else None
        )
        if since is not None and until is not None and since >= until:
            raise _invalid_period()
        funnel = (await self.repo.lead_funnel([campaign.id], since=since, until=until))[campaign.id]
        counts = (await self.repo.status_counts([campaign.id])).get(campaign.id, {})
        messages = await self.repo.message_totals(campaign.id, since=since, until=until)
        raw_steps = await self.repo.step_funnel(campaign.id, since=since, until=until)
        names = {s.position: s.name for s in campaign.steps}
        positions: list[int | None] = list(sorted(names))
        positions += sorted(p for p in raw_steps if p is not None and p not in names)
        if None in raw_steps:
            positions.append(None)
        steps: list[EmailStepPerformanceRead] = []
        for position in positions:
            row = raw_steps.get(position) or dict.fromkeys(
                ("entered", "sent", "opened", "clicked", "replied", "bounced", "unsubscribed"), 0
            )
            entered = row["entered"]
            steps.append(
                EmailStepPerformanceRead(
                    position=position,
                    name=names.get(position) if position is not None else None,
                    **row,
                    open_rate=_ratio(row["opened"], entered),
                    click_rate=_ratio(row["clicked"], entered),
                    reply_rate=_ratio(row["replied"], entered),
                    bounce_rate=_ratio(row["bounced"], entered),
                    unsubscribe_rate=_ratio(row["unsubscribed"], entered),
                )
            )
        reached = funnel["reached"]
        timeline = await self.repo.performance_timeline(
            campaign.id, timezone=zone.key, since=since, until=until
        )
        return EmailPerformanceRead(
            campaign_id=campaign.id,
            timezone=zone.key,
            launched_from=launched_from,
            launched_to=launched_to,
            leads_in_campaign=sum(n for s, n in counts.items() if s != "EMAIL_REMOVED"),
            leads_to_launch=await self.repo.to_launch_count(campaign.id),
            funnel=EmailLeadFunnelRead(**funnel),
            rates=EmailPerformanceRates(
                open_rate=_ratio(funnel["opened"], reached),
                click_rate=_ratio(funnel["clicked"], reached),
                reply_rate=_ratio(funnel["replied"], reached),
                bounce_rate=_ratio(funnel["bounced"], reached),
                unsubscribe_rate=_ratio(funnel["unsubscribed"], reached),
            ),
            messages=EmailMessageTotalsRead(**messages),
            states=await self.repo.cohort_states(campaign.id, since=since, until=until),
            steps=steps,
            timeline=[EmailPerformanceDay(day=day, **row) for day, row in timeline.items()],
        )

    # ── outreach (route 11) ────────────────────────────────────────────────

    async def outreach(
        self,
        agent: Agent,
        start: date,
        end: date,
        timezone: str,
        filters: dict[str, Any],
    ) -> EmailOutreachRead:
        """The agency's outreach across its campaigns, day by day, from
        Nidria's records: what was sent, replied, bounced or unsubscribed (at
        its time, in the viewer's timezone), and each launched lead's next
        emails, an estimated day in its campaign's timezone, never an hour:
        `schedule`, the one estimate the member drawer shows too. Leads
        without a date (not launched, paused, campaign not active, launched
        in a dry run) are counted, each under its reason, never placed. At
        most 62 days."""
        access = await self.access(agent)
        if end < start:
            raise _invalid_period()
        if (end - start).days > OUTREACH_MAX_DAYS:
            raise BadRequestError(
                f"Show at most {OUTREACH_MAX_DAYS} days at a time.",
                code="email.period_too_long",
                params={"max_days": OUTREACH_MAX_DAYS},
            )
        viewer = zone_of(timezone)
        since = datetime.combine(start, time.min, tzinfo=viewer).astimezone(UTC)
        until = datetime.combine(end + timedelta(days=1), time.min, tzinfo=viewer).astimezone(UTC)
        past = await self.repo.outreach_past(
            access.agency_id, since, until, filters, OUTREACH_LIMIT
        )
        in_flight = await self.repo.outreach_in_flight(access.agency_id, filters)
        sent = await self.repo.sent_days([row.member.id for row in in_flight])
        # Read last, steps loaded: a membership query refreshes the campaign
        # rows it joins (`populate_existing`) and would unload their steps.
        campaigns = {c.id: c for c in await self.repo.campaigns(access.agency_id)}
        found: list[tuple[MemberRow, dict[str, Any]]] = []
        for kind, at, position, row in past:
            found.append(
                (
                    row,
                    {
                        "kind": kind,
                        "day": at.astimezone(viewer).date(),
                        "at": at,
                        "step_position": position,
                    },
                )
            )
        overdue = 0
        for row in in_flight:
            loaded = campaigns.get(row.campaign.id)
            if loaded is None:
                continue
            settings = EmailCampaignSettings.model_validate(loaded.settings or {})
            plan = schedule(
                row.member,
                loaded.steps,
                sent.get(row.member.id, {}),
                campaign_status=loaded.status,
                timezone=settings.timezone,
                sending_days=settings.sending_days,
                sending_end=settings.sending_end_time,
            )
            today = today_in(settings.timezone)
            for planned in sorted(plan.steps.values(), key=lambda p: p.position):
                if not planned.estimated or planned.day is None:
                    continue
                if planned.overdue:
                    overdue += 1
                    if start <= today <= end:
                        found.append(
                            (
                                row,
                                {
                                    "kind": "OVERDUE",
                                    "day": today,
                                    "estimated": True,
                                    "estimated_day": planned.day,
                                    "step_position": planned.position,
                                },
                            )
                        )
                elif start <= planned.day <= end:
                    found.append(
                        (
                            row,
                            {
                                "kind": "SCHEDULED",
                                "day": planned.day,
                                "estimated": True,
                                "step_position": planned.position,
                            },
                        )
                    )
        kinds = set(filters.get("kinds") or ())
        if kinds:
            found = [entry for entry in found if entry[1]["kind"] in kinds]
        people = await self.people(access, [row.profile for row, _ in found])
        items = [self._outreach_item(row, values, people, campaigns) for row, values in found]
        rank: dict[OutreachKind, int] = {
            "OVERDUE": 0,
            "SENT": 1,
            "REPLIED": 1,
            "BOUNCED": 1,
            "UNSUBSCRIBED": 1,
            "SCHEDULED": 2,
        }
        items.sort(
            key=lambda i: (
                i.day,
                rank[i.kind],
                i.at or datetime.min.replace(tzinfo=UTC),
                i.campaign_name,
                i.full_name,
            )
        )
        waiting, held, dry_run = await self.repo.outreach_held(access.agency_id, filters)
        return EmailOutreachRead(
            start=start,
            end=end,
            timezone=viewer.key,
            items=items[:OUTREACH_LIMIT],
            truncated=len(items) > OUTREACH_LIMIT,
            waiting_launch=waiting,
            on_hold=held,
            dry_run=dry_run,
            overdue=overdue,
            campaigns=[
                EmailOutreachCampaign(
                    id=c.id, name=c.name, status=c.status, sender=_sender_email(c)
                )
                for c in campaigns.values()
            ],
        )

    @staticmethod
    def _outreach_item(
        row: MemberRow,
        values: dict[str, Any],
        people: People,
        campaigns: Mapping[uuid.UUID, EmailCampaign],
    ) -> EmailOutreachItem:
        member, profile, campaign = row.member, row.profile, row.campaign
        identity = client_identity(profile, row.account)
        company = people.company(profile)
        loaded = campaigns.get(campaign.id)
        step_names = {s.position: s.name for s in loaded.steps} if loaded is not None else {}
        position = values.get("step_position")
        return EmailOutreachItem(
            member_id=member.id,
            client_profile_id=profile.id,
            full_name=_full_name(identity.first_name, identity.last_name),
            email=identity.email,
            company_id=company.id if company is not None else None,
            company_name=company.name if company is not None else None,
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            status=member.status,
            sender=_sender_email(campaign),
            step_name=step_names.get(position) if position is not None else None,
            **values,
        )

    # ── a client record's email state (route 33) ───────────────────────────

    async def client_profile_email(
        self, agent: Agent, client_profile_id: uuid.UUID
    ) -> EmailClientProfileRead:
        """A client record's email state (derived, never stored) and its
        memberships in the agency's campaigns, with what holds back the one
        still to launch. A record of another agency is not found."""
        access = await self.access(agent)
        profile = await self.client_profile(access, client_profile_id)
        rows = await self.repo.member_rows_of_profile(access.agency_id, profile.id)
        people = await self.people(access, [profile])
        steps_count = await self.repo.step_counts(sorted({r.campaign.id for r in rows}))
        waiting = [r for r in rows if not_launched(r.member)]
        issues: dict[uuid.UUID, list[Issue]] = {}
        if waiting:
            facts = await self.client_facts(access, waiting)
            for r in waiting:
                email = client_identity(r.profile, r.account).email
                issues[r.member.id] = membership_issues(r.member, email, facts)
        return EmailClientProfileRead(
            client_profile_id=profile.id,
            email_status=client_email_status(r.member.status for r in rows),
            client_status=people.client_status(profile),
            memberships=[
                self.member_read(r, people, steps_count, issues.get(r.member.id, ())) for r in rows
            ],
        )

    # ── the add wizard (routes 34 to 37) ───────────────────────────────────

    async def candidate_clients(
        self,
        agent: Agent,
        criteria: ProfileListFilter,
        *,
        client_statuses: Sequence[str],
        has_email: bool | None,
        campaign_id: uuid.UUID | str | None,
        preferred_langs: Sequence[str] = (),
        sort_by: str,
        sort_order: str,
        page: int,
        page_size: int,
    ) -> EmailCandidateClientList:
        """The Clients page's list (its criteria, sort and paging, decision
        A15), with where each client stands in the agency's campaigns.
        `preferred_langs`: the client's EFFECTIVE language (the record's,
        else its account's, else the agency's) is one of them."""
        access = await self.access(agent)
        agency = await self.agency(access) if preferred_langs else None
        rows, total = await self.repo.candidate_clients(
            access.agency_id,
            criteria,
            client_statuses=client_statuses,
            has_email=has_email,
            campaign_id=campaign_id,
            preferred_langs=preferred_langs,
            agency_default_lang=agency.default_language if agency is not None else None,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )
        profiles = [profile for profile, _ in rows]
        live = {
            m.client_profile_id: m
            for m in await self.repo.live_members(access.agency_id, [p.id for p in profiles])
        }
        names = await self.repo.campaign_names(
            access.agency_id, (m.campaign_id for m in live.values())
        )
        people = await self.people(access, profiles)
        items: list[EmailCandidateClient] = []
        for profile, account in rows:
            identity = client_identity(profile, account)
            company = people.company(profile)
            membership = live.get(profile.id)
            items.append(
                EmailCandidateClient(
                    id=profile.id,
                    first_name=identity.first_name,
                    last_name=identity.last_name,
                    full_name=_full_name(identity.first_name, identity.last_name),
                    email=identity.email,
                    has_email=bool(identity.email),
                    company_id=company.id if company is not None else None,
                    company_name=company.name if company is not None else None,
                    client_status=people.client_status(profile),
                    email_status=cast(MemberStatus, membership.status) if membership else None,
                    current_campaign_id=membership.campaign_id if membership else None,
                    current_campaign_name=names.get(membership.campaign_id) if membership else None,
                )
            )
        return EmailCandidateClientList(items=items, total=total, page=page, page_size=page_size)

    async def candidate_companies(
        self,
        agent: Agent,
        criteria: CompanyListFilter,
        *,
        only_with_ready: bool,
        with_email: bool,
        sort_by: str,
        sort_order: str,
        page: int,
        page_size: int,
    ) -> EmailCandidateCompanyList:
        """The Companies page's list (its criteria, sort and paging), each
        company with how many of its clients the wizard would add."""
        access = await self.access(agent)
        rows, total = await self.repo.candidate_companies(
            access.agency_id,
            criteria,
            only_with_ready=only_with_ready,
            with_email=with_email,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )
        statuses = await self.repo.company_statuses(
            access.agency_id, [company.id for company, *_ in rows]
        )
        return EmailCandidateCompanyList(
            items=[
                EmailCandidateCompany(
                    id=company.id,
                    name=company.name,
                    email_status=client_email_status(statuses.get(company.id, [])),
                    clients_count=clients,
                    eligible_clients_count=eligible,
                    ready_clients_count=ready,
                )
                for company, clients, eligible, ready in rows
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def preview_add(
        self, agent: Agent, campaign_id: uuid.UUID, request: EmailAddMembersRequest
    ) -> EmailAddPreviewResponse:
        """What adding this selection would do, row by row. Writes nothing."""
        access = await self.access(agent, Permission.EMAIL_ENROLL)
        campaign = await self.campaign_for(access, campaign_id)
        await self.writable(access, campaign)
        plan, _ = await self._add_plan(access, campaign, request)
        return EmailAddPreviewResponse(
            campaign_id=campaign.id, summary=_add_summary(plan), rows=plan
        )

    async def add_members(
        self, agent: Agent, campaign_id: uuid.UUID, request: EmailAddMembersRequest
    ) -> EmailAddMembersResponse:
        """Add the eligible clients as EMAIL_READY: adding sends nothing
        (spec 8.4), only a Launch hands a lead over. The others are left out
        with their reason, never moved (decision A10)."""
        access = await self.access(agent, Permission.EMAIL_ENROLL)
        # Locked: two adds into one campaign never race on the same client.
        campaign = await self.campaign_for(access, campaign_id, lock=True)
        await self.writable(access, campaign)
        if campaign.status == "ARCHIVED":
            raise BadRequestError(
                "Clients cannot be added to an archived campaign.",
                code="email.campaign_archived",
            )
        plan, _ = await self._add_plan(access, campaign, request)
        added = 0
        for row in plan:
            if row.action != "ADD":
                continue
            self.db.add(
                EmailCampaignMember(
                    campaign_id=campaign.id,
                    client_profile_id=row.client_profile_id,
                    agency_id=access.agency_id,
                    status="EMAIL_READY",
                    added_by_agent_id=access.agent_id,
                )
            )
            added += 1
        await self.db.commit()
        return EmailAddMembersResponse(added=added, skipped=len(plan) - added, rows=plan)

    async def _add_plan(
        self, access: Access, campaign: EmailCampaign, request: EmailAddMembersRequest
    ) -> tuple[list[EmailAddPreviewRow], list[tuple[ClientProfile, ExpatUser | None]]]:
        """The verdict on every client of the selection: the chosen records,
        then each chosen company's clients with an address. Another agency's
        record or company is refused outright (404), never skipped."""
        agency_id = access.agency_id
        company_ids = list(dict.fromkeys(request.company_ids))
        known = await self.repo.company_ids_in_agency(agency_id, company_ids)
        if len(known) != len(company_ids):
            raise NotFoundError("Company profile not found.", code="company_profile.not_found")
        excluded = set(request.exclude_client_profile_ids)
        picked = [i for i in dict.fromkeys(request.client_profile_ids) if i not in excluded]
        rows = await self.repo.profiles_with_accounts(agency_id, picked)
        if len(rows) != len(picked):
            raise NotFoundError("Client profile not found.", code="profile.not_found")
        chosen = set(picked)
        brought = [
            i
            for i in await self.repo.profiles_of_companies(agency_id, company_ids)
            if i not in excluded and i not in chosen
        ]
        rows += await self.repo.profiles_with_accounts(agency_id, brought)
        ids = [profile.id for profile, _ in rows]
        open_here = await self.repo.open_in_campaign(campaign.id, ids)
        identities = {profile.id: client_identity(profile, account) for profile, account in rows}
        facts = await load_client_facts(
            self.repo, agency_id, [(i, identities[i].email) for i in ids]
        )
        live_campaigns = {m.campaign_id for m in facts.live.values()}
        live_campaigns |= {m.campaign_id for ms in facts.live_addresses.values() for m in ms}
        names = await self.repo.campaign_names(agency_id, live_campaigns)
        people = await self.people(access, [profile for profile, _ in rows])
        # An address a READY row already carries: Lemlist holds one lead per
        # address in a team, so a second record with it is left out.
        addresses: dict[str, uuid.UUID] = {}
        plan: list[EmailAddPreviewRow] = []
        for profile, _account in rows:
            identity = identities[profile.id]
            own = facts.live.get(profile.id)
            current: uuid.UUID | None = own.campaign_id if own is not None else None
            via: uuid.UUID | None = None
            eligibility: Eligibility
            if profile.id in open_here:
                eligibility = "ALREADY_IN_CAMPAIGN"
            else:
                issues = client_issues(profile.id, identity.email, facts)
                blocking = [i for i in issues if i.blocking]
                if blocking:
                    issue = blocking[0]
                    eligibility = cast(Eligibility, issue.code)
                    if "via_client_profile_id" in issue.params:
                        via = uuid.UUID(str(issue.params["via_client_profile_id"]))
                    if "campaign_id" in issue.params:
                        current = uuid.UUID(str(issue.params["campaign_id"]))
                else:
                    address = normalize_email(identity.email or "")
                    if address in addresses:
                        eligibility, via = "DUPLICATE_EMAIL", addresses[address]
                    else:
                        eligibility = "READY"
                        addresses[address] = profile.id
            company = people.company(profile)
            plan.append(
                EmailAddPreviewRow(
                    client_profile_id=profile.id,
                    first_name=identity.first_name,
                    last_name=identity.last_name,
                    full_name=_full_name(identity.first_name, identity.last_name),
                    email=identity.email,
                    company_id=company.id if company is not None else None,
                    company_name=company.name if company is not None else None,
                    current_campaign_id=current,
                    current_campaign_name=names.get(current) if current is not None else None,
                    via_client_profile_id=via,
                    eligibility=eligibility,
                    action="ADD" if eligibility == "READY" else "SKIP",
                )
            )
        return plan, rows

    # ── the deletion hooks (spec 8.11, decision A2) ─────────────────────────

    async def _cleanup_provider(
        self, module: AgencyEmailModule | None, who: Mapping[str, Any]
    ) -> tuple[EmailProvider, str | None]:
        """The agency's REAL provider and the key's team, for leads that
        exist at Lemlist: removing them is mandatory, whatever the module's
        state (adjustment a). When Lemlist cannot be reached for real, 409
        `email.provider_cleanup_failed` with `who` and a `reason`:
        `module_disabled` (the module is off for the agency),
        `external_effects_off` (real effects off, Lemlist mocked or no key),
        `agency_unbound` (no Lemlist team bound), `team_mismatch` (the key
        opens another team than the bound one), `team_unknown` (Lemlist does
        not say which team the key opens, decision A4). Nothing is deleted
        then: the leads would stay at Lemlist for good."""

        def unavailable(reason: str) -> ConflictError:
            return _cleanup_failed({**who, "reason": reason})

        if not email_module_effectively_enabled(module, self.settings):
            raise unavailable("module_disabled")
        try:
            provider = await self.provider_of(module)
        except ConflictError as exc:
            raise unavailable(str(exc.params.get("reason") or "team_unknown")) from exc
        if not provider.external:
            if not external_effects_on(self.settings):
                raise unavailable("external_effects_off")
            if module is None or module.provider_team_id is None:
                raise unavailable("agency_unbound")
            raise unavailable("team_mismatch")
        try:
            team = await asyncio.to_thread(provider.team_id)
        except ProviderError as exc:
            raise unavailable("team_unknown") from exc
        return provider, team

    async def cleanup_client_profiles(
        self, agency_id: uuid.UUID, profile_ids: Sequence[uuid.UUID]
    ) -> int:
        """Before client records are deleted (one, in bulk, or merged away):
        remove at Lemlist every lead of theirs that carries a real provider
        id, ended ones included (Lemlist's deduplication sees them).

        Mandatory whenever such a lead exists, whatever the module's state
        (adjustment a): when Lemlist cannot be reached for real (module off
        for the agency, effects off, agency unbound, another team, team
        unknown), or a lead lives in another Lemlist team than the key's,
        or Lemlist refuses a removal, 409 `email.provider_cleanup_failed`
        (params `client_profile_id`, `reason`, and `code` for a refusal):
        the caller deletes nothing and the agent tries again once Lemlist is
        reachable. The teams are checked before any call. No call at all,
        and nothing blocks, without a real lead (a dry run's, or one a
        removal already took out). Returns how many leads were removed.
        Writes nothing."""
        if not profile_ids:
            return 0
        leads = await self.repo.real_leads_of_profiles(agency_id, profile_ids)
        if not leads:
            return 0
        first = {"client_profile_id": str(leads[0][0].client_profile_id)}
        provider, key_team = await self._cleanup_provider(
            await self.repo.get_module(agency_id), first
        )
        for member, campaign in leads:
            if on_other_team(campaign, key_team):
                raise _cleanup_failed(
                    {"client_profile_id": str(member.client_profile_id), "reason": "team_mismatch"}
                )
        removed = 0
        for member, campaign in leads:
            ref = campaign_ref(campaign)
            assert ref is not None  # a real provider campaign
            try:
                await asyncio.to_thread(provider.remove_contact, ref, lead_ref(member))
            except ProviderError as exc:
                raise _cleanup_failed(
                    {
                        "client_profile_id": str(member.client_profile_id),
                        "reason": "provider_refused",
                        "code": provider_error_code(exc),
                    }
                ) from exc
            removed += 1
        return removed

    async def cleanup_agency(self, agency_id: uuid.UUID) -> int:
        """Before an agency is deleted: pause at Lemlist each of its
        campaigns that carries a real provider id and remove their leads, so
        the team can serve another agency without exposing anything (spec
        8.11; Lemlist's own unsubscribe list stays, spec 18).

        Mandatory whenever such a campaign exists, whatever the module's
        state (adjustment a): Lemlist out of reach for real, a campaign of
        another team, or a refusal is a 409 `email.provider_cleanup_failed`
        (params `campaign_id`, `reason`, and `code` for a refusal) before
        any write. No call at all without a real campaign. Returns how many
        leads were removed."""
        campaigns = await self.repo.real_provider_campaigns(agency_id)
        if not campaigns:
            return 0
        first = {"campaign_id": str(campaigns[0].id)}
        provider, key_team = await self._cleanup_provider(
            await self.repo.get_module(agency_id), first
        )
        for campaign in campaigns:
            if on_other_team(campaign, key_team):
                raise _cleanup_failed({"campaign_id": str(campaign.id), "reason": "team_mismatch"})
        removed = 0
        for campaign in campaigns:
            ref = campaign_ref(campaign)
            assert ref is not None  # a real provider campaign
            try:
                await asyncio.to_thread(provider.pause_campaign, ref)
                for member in await self.repo.real_leads_of_campaign(campaign.id):
                    await asyncio.to_thread(provider.remove_contact, ref, lead_ref(member))
                    removed += 1
            except ProviderError as exc:
                raise _cleanup_failed(
                    {
                        "campaign_id": str(campaign.id),
                        "reason": "provider_refused",
                        "code": provider_error_code(exc),
                    }
                ) from exc
        return removed


def _sender_option(sender: ProviderSender) -> EmailSenderOption:
    return EmailSenderOption(user_id=sender.user_id, email=sender.email, campaigns=sender.campaigns)


def _sender_email(campaign: EmailCampaign) -> str | None:
    """The mailbox a campaign is sent from, as chosen in Nidria."""
    sender = (campaign.settings or {}).get("sender")
    if isinstance(sender, dict) and isinstance(sender.get("email"), str):
        return str(sender["email"])
    return None


def _cleanup_failed(params: Mapping[str, Any]) -> ConflictError:
    return ConflictError(
        "The leads at Lemlist could not be removed: nothing was deleted, try again "
        "once Lemlist is reachable.",
        code="email.provider_cleanup_failed",
        params=dict(params),
    )


def _add_summary(plan: Sequence[EmailAddPreviewRow]) -> EmailAddPreviewSummary:
    def count(kind: str) -> int:
        return sum(1 for r in plan if r.eligibility == kind)

    return EmailAddPreviewSummary(
        selected=len(plan),
        ready=count("READY"),
        no_email=count("NO_EMAIL"),
        invalid_email=count("INVALID_EMAIL"),
        demo_recipient=count("DEMO_RECIPIENT"),
        unsubscribed=count("UNSUBSCRIBED"),
        bounced=count("BOUNCED"),
        in_other_campaign=count("IN_OTHER_CAMPAIGN"),
        already_in_campaign=count("ALREADY_IN_CAMPAIGN"),
        duplicate_email=count("DUPLICATE_EMAIL"),
        will_add=sum(1 for r in plan if r.action == "ADD"),
        will_skip=sum(1 for r in plan if r.action == "SKIP"),
    )


def _copy_step(
    step: EmailCampaignStep, *, position: int, name: str | None = None
) -> EmailCampaignStep:
    """A new step with the same content and AI variables, nothing at the
    provider. `name`: the copy's (decision A13); else the original's."""
    copy = EmailCampaignStep(
        position=position,
        name=(name.strip() if name and name.strip() else step.name)[:255],
        type=step.type,
        delay_value=step.delay_value,
        delay_unit=step.delay_unit,
        subject_template=step.subject_template,
        body_template=step.body_template,
        version=1,
        content_hash=step.content_hash,
    )
    copy.ai_variables = [
        EmailCampaignAiVariable(
            key=v.key,
            prompt_template=v.prompt_template,
            model=v.model,
            config=dict(v.config or {}),
        )
        for v in step.ai_variables
    ]
    return copy


__all__ = [
    "Access",
    "EmailManager",
    "People",
    "SequenceFacts",
    "client_email_status",
    "provider_error_code",
    "provider_for",
    "provider_params",
    "provider_refusal",
    "set_status_reason",
]
