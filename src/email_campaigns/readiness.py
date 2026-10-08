"""What stands between a client and a campaign's emails: one set of rules.

Read by every place that decides or shows it: the add preview, the Leads
list, the Launch page and the hand-over to Lemlist (spec section 8.6,
Prism's D4). A screen never says "ready" for a client the hand-over would
refuse, and the hand-over never refuses for a reason no screen could show.

Ported from Prism's `readiness.py` (reference commit 629a2b0). Changes:
- no Apollo verification, no legacy workflow (`LEGACY_*`), no PREVIOUS_TEAM;
- a problem is an `Issue(code, params, level)`: a stable code with named
  params that the frontend translates (rule N9, decision A12), never a
  sentence;
- new codes: DEMO_RECIPIENT (rule N11) and TEAM_MISMATCH (rule N4, in place
  of Prism's PREVIOUS_TEAM);
- the recipient is a client record, its address the effective one (rule N2,
  `client_profiles.identity`), passed in by the caller;
- an unsubscribe is never overridable (rule N6: Prism's
  `include_unsubscribed` is gone);
- the durable block (A1, the opposition list) and the shared address (R1),
  below.

Codes, by level, with their params:
- person: NO_EMAIL, INVALID_EMAIL, DEMO_RECIPIENT; UNSUBSCRIBED and BOUNCED
  (`via_client_profile_id` when the address was listed through another
  record that still exists); IN_OTHER_CAMPAIGN (`campaign_id`, plus
  `via_client_profile_id` when another record with the same address holds
  the live membership);
- membership: PAUSED; LAST_ATTEMPT, a warning (`reason`: the code the last
  hand-over left in `status_reason`);
- campaign: TEAM_MISMATCH, ARCHIVED, NO_STEPS, EXTERNAL_EFFECTS_OFF,
  NOT_ACTIVE; DAILY_LIMIT (`launched_today`, `daily_limit`); NO_SENDER, a
  warning;
- template: TEMPLATE (`position`, `kind`: a renderer error kind,
  `first_subject_required` or `empty_body`, and `token` when there is one);
  AI_AT_LAUNCH: a warning while AI values can be generated, BLOCKING
  (`reason: ai_not_configured`, `position`) while they cannot (decision
  A14): the Launch would refuse that client, so the Launch page says so.

Every code is blocking but LAST_ATTEMPT, NO_SENDER and AI_AT_LAUNCH (when AI
generation is configured). As in
Prism, NO_SENDER only warns at the Launch page: Lemlist's only sender of the
team sends; with several, the sync stops and asks for a choice (the adapter's
`SENDER_REQUIRED`). TEMPLATE and AI_AT_LAUNCH are kept from Prism: TEMPLATE
carries each template problem in the same issue list as the person's, so the
Launch page shows one list; AI_AT_LAUNCH tells the AI passages are written at
launch, then frozen.

Durable block (decision A1, rule N6, and the opposition list of 08/10/2026).
A person is blocked for good when:
- one of ITS OWN memberships unsubscribed or bounced, removed ones included
  (`EmailRepository.blocking_statuses` deliberately ignores `removed_at`):
  the person stays blocked even once its address changed; or
- its effective ADDRESS is on the agency's opposition list
  (`email_suppression`, `suppression.email_hash`), the source of truth for
  addresses: whichever record carries the address now (a second record, or
  one created again after the first was deleted), whatever campaign or
  record was deleted since.
The list is written each time an unsubscribe or a bounce is applied to a
membership (`email_sync`); the memberships keep their history:
1. removing (or moving) an UNSUBSCRIBED or BOUNCED membership sets
   `removed_at` and `removed_by_agent_id` and KEEPS its status (never
   EMAIL_REMOVED);
2. an UNSUBSCRIBED or BOUNCED event applies to a removed membership too:
   its status changes, `removed_at` stays.
Deleting a campaign is allowed (it was refused before the list): its
memberships go, the list keeps a fingerprint of their addresses (never the
addresses). Lemlist's own unsubscribe
list holds the address too (spec section 18).

Shared address (risk R1). `client_profile.email` is not unique in an agency,
and a linked record reads its account's address: two records can carry the
same effective address. Lemlist deduplicates leads by address in its team,
so the rules read the address as well as the record: a listed address
blocks every record that carries it (an address that unsubscribed or
bounced is never written to again), and a live membership of another
record with the same address is IN_OTHER_CAMPAIGN, whichever campaign holds
it (this one included: `campaign_id` says which). An address is compared
trimmed and lower-cased (the list: its fingerprint); the add preview still
has to deduplicate the addresses of one batch among themselves (phase 3).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from shared.models.email_campaign import (
    EmailCampaign,
    EmailCampaignMember,
    EmailCampaignStep,
    EmailMemberRender,
)
from src.core.email import is_demo_recipient, normalize_email
from src.email_campaigns.email_repository import EmailRepository
from src.email_campaigns.flags import is_real_provider_id
from src.email_campaigns.lemlist_team import CAMPAIGN_TEAM_KEY
from src.email_campaigns.renderer import EmailTemplateRenderer, RenderResult, tokens_in
from src.email_campaigns.schedule import zone_of
from src.email_campaigns.suppression import email_hash
from src.email_campaigns.variables import RenderContext

_LOCAL = re.compile(r"[^\s@<>()\[\],;:\"]{1,64}")
_LABEL = re.compile(r"[^\s@<>()\[\],;:\"._-][^\s@<>()\[\],;:\".]{0,62}")
#: Letters only, or an internationalized (punycode) extension.
_TLD = re.compile(r"[^\W\d_]{2,63}|xn--[a-z0-9-]{1,59}", re.IGNORECASE)


def email_syntax_ok(value: str | None) -> bool:
    """Whether an address is at least shaped like one: a single @, nothing
    blank around it, no space, and a domain of dot-separated labels ending in
    a letters-only extension. A syntactic check, never a deliverability claim:
    a well-formed address can still bounce."""
    email = (value or "").strip()
    if email.count("@") != 1 or len(email) > 254:
        return False
    local, domain = email.split("@")
    if not _LOCAL.fullmatch(local) or local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    labels = domain.split(".")
    if len(labels) < 2 or not _TLD.fullmatch(labels[-1]):
        return False
    return all(_LABEL.fullmatch(label) and not label.endswith("-") for label in labels)


# ── issues: what stands between a client and its emails ──────────────────

IssueLevel = Literal["blocking", "warning"]
IssueParams = dict[str, str | int]


@dataclass(frozen=True)
class Issue:
    """One reason, as a code and its named params (the frontend writes the
    sentence). A blocking issue keeps the client from being added or handed
    over; a warning decides nothing."""

    code: str
    params: IssueParams = field(default_factory=dict)
    level: IssueLevel = "blocking"

    @property
    def blocking(self) -> bool:
        return self.level == "blocking"


# Person.
NO_EMAIL = "NO_EMAIL"
INVALID_EMAIL = "INVALID_EMAIL"
DEMO_RECIPIENT = "DEMO_RECIPIENT"
UNSUBSCRIBED = "UNSUBSCRIBED"
BOUNCED = "BOUNCED"
IN_OTHER_CAMPAIGN = "IN_OTHER_CAMPAIGN"
# Membership.
PAUSED = "PAUSED"
LAST_ATTEMPT = "LAST_ATTEMPT"
# Campaign-wide: nobody of the campaign launches while a blocking one holds.
TEAM_MISMATCH = "TEAM_MISMATCH"
ARCHIVED = "ARCHIVED"
NO_STEPS = "NO_STEPS"
EXTERNAL_EFFECTS_OFF = "EXTERNAL_EFFECTS_OFF"
NOT_ACTIVE = "NOT_ACTIVE"
DAILY_LIMIT = "DAILY_LIMIT"
NO_SENDER = "NO_SENDER"
# Template.
TEMPLATE = "TEMPLATE"
AI_AT_LAUNCH = "AI_AT_LAUNCH"

PERSON_CODES = (NO_EMAIL, INVALID_EMAIL, DEMO_RECIPIENT, UNSUBSCRIBED, BOUNCED, IN_OTHER_CAMPAIGN)
MEMBERSHIP_CODES = (PAUSED, LAST_ATTEMPT)
CAMPAIGN_CODES = (
    TEAM_MISMATCH,
    ARCHIVED,
    NO_STEPS,
    EXTERNAL_EFFECTS_OFF,
    NOT_ACTIVE,
    DAILY_LIMIT,
    NO_SENDER,
)
TEMPLATE_CODES = (TEMPLATE, AI_AT_LAUNCH)
#: Every code a readiness issue can carry (the frontend translates each one).
ISSUE_CODES = PERSON_CODES + MEMBERSHIP_CODES + CAMPAIGN_CODES + TEMPLATE_CODES

_UNSUBSCRIBED_STATUS = "EMAIL_UNSUBSCRIBED"


def _address(email: str | None) -> str | None:
    """How an address is compared: trimmed and lower-cased; None when blank."""
    if email is None or not email.strip():
        return None
    return normalize_email(email)


def _worst(first: str | None, second: str) -> str:
    """An unsubscribe outranks a bounce (Prism's order)."""
    return first if first == _UNSUBSCRIBED_STATUS else second


@dataclass(frozen=True)
class ClientFacts:
    """What the database knows of a batch of clients beyond their rows, in
    one agency: their own blocks, the opposition list's entries for their
    addresses, and live memberships, by record and by effective address
    (risk R1). Built by `load_client_facts`."""

    #: client profile id -> EMAIL_UNSUBSCRIBED or EMAIL_BOUNCED, from ANY of
    #: its memberships, removed ones included (A1); an unsubscribe wins.
    blocked: Mapping[uuid.UUID, str] = field(default_factory=dict)
    #: client profile id -> its live membership (one at most, any campaign).
    live: Mapping[uuid.UUID, EmailCampaignMember] = field(default_factory=dict)
    #: address fingerprint (`suppression.email_hash`) -> (EMAIL_UNSUBSCRIBED
    #: or EMAIL_BOUNCED, the client record the listed membership belonged
    #: to, None once it is gone): the agency's opposition list.
    suppressed: Mapping[str, tuple[str, uuid.UUID | None]] = field(default_factory=dict)
    #: address -> the live memberships of the records with that address.
    live_addresses: Mapping[str, Sequence[EmailCampaignMember]] = field(default_factory=dict)


async def load_client_facts(
    repo: EmailRepository,
    agency_id: uuid.UUID,
    clients: Iterable[tuple[uuid.UUID, str | None]],
) -> ClientFacts:
    """The facts the rules read for these clients, given as (client profile
    id, effective address): four queries for a whole batch, every one scoped
    to the agency."""
    people = list(clients)
    ids = list(dict.fromkeys(profile_id for profile_id, _ in people))
    addresses = sorted({a for a in (_address(email) for _, email in people) if a is not None})
    blocked: dict[uuid.UUID, str] = {}
    for profile_id, status in await repo.blocking_statuses(agency_id, ids):
        blocked[profile_id] = _worst(blocked.get(profile_id), status)
    live = {m.client_profile_id: m for m in await repo.live_members(agency_id, ids)}
    digests = sorted({d for d in (email_hash(email) for _, email in people) if d is not None})
    suppressed = await repo.suppressed(agency_id, digests)
    live_addresses: dict[str, list[EmailCampaignMember]] = {}
    for email, member in await repo.live_members_by_email(agency_id, addresses):
        live_addresses.setdefault(email, []).append(member)
    return ClientFacts(blocked, live, suppressed, live_addresses)


def _block_of(
    profile_id: uuid.UUID, email: str | None, facts: ClientFacts
) -> tuple[str, uuid.UUID | None] | None:
    """The block that applies: (status, None) for the record's own, or for
    its address listed through itself or a record gone since; (status,
    other record) for its address listed through another record. An
    unsubscribe outranks a bounce; the record's own outranks another's."""
    found: list[tuple[str, uuid.UUID | None]] = []
    own = facts.blocked.get(profile_id)
    if own is not None:
        found.append((own, None))
    digest = email_hash(email)
    listed = facts.suppressed.get(digest) if digest is not None else None
    if listed is not None:
        status, source = listed
        found.append((status, source if source != profile_id else None))
    if not found:
        return None
    found.sort(key=lambda b: (b[0] != _UNSUBSCRIBED_STATUS, b[1] is not None, str(b[1] or "")))
    return found[0]


def durable_block(profile_id: uuid.UUID, email: str | None, facts: ClientFacts) -> str | None:
    """EMAIL_UNSUBSCRIBED or EMAIL_BOUNCED when the person is blocked for
    good (its own memberships, or its address on the opposition list),
    else None: what refuses a move to another campaign."""
    block = _block_of(profile_id, email, facts)
    return block[0] if block is not None else None


def _live_elsewhere(
    profile_id: uuid.UUID,
    address: str | None,
    facts: ClientFacts,
    member_id: uuid.UUID | None,
) -> tuple[EmailCampaignMember, uuid.UUID | None] | None:
    """A live membership that is not the one being checked: the record's own
    first, else one of another record with the same address."""
    own = facts.live.get(profile_id)
    if own is not None and own.id != member_id:
        return own, None
    if address is None:
        return None
    others = sorted(
        (
            m
            for m in facts.live_addresses.get(address, ())
            if m.client_profile_id != profile_id and m.id != member_id
        ),
        key=lambda m: str(m.id),
    )
    return (others[0], others[0].client_profile_id) if others else None


def client_issues(
    profile_id: uuid.UUID,
    email: str | None,
    facts: ClientFacts,
    *,
    member_id: uuid.UUID | None = None,
) -> list[Issue]:
    """Every reason this client cannot receive a campaign's emails, in the
    order the add preview applies them: the address (none, malformed, a demo
    one), a block on record (unsubscribed, bounced), live elsewhere.
    `email`: the client's effective address (rule N2). `member_id`: the
    membership being checked (its own liveness is not "elsewhere")."""
    issues: list[Issue] = []
    shown = (email or "").strip()
    if not shown:
        issues.append(Issue(NO_EMAIL))
    elif not email_syntax_ok(shown):
        issues.append(Issue(INVALID_EMAIL))
    elif is_demo_recipient(shown):
        issues.append(Issue(DEMO_RECIPIENT))
    address = _address(email)
    block = _block_of(profile_id, email, facts)
    if block is not None:
        status, via = block
        code = UNSUBSCRIBED if status == _UNSUBSCRIBED_STATUS else BOUNCED
        issues.append(Issue(code, {"via_client_profile_id": str(via)} if via else {}))
    live = _live_elsewhere(profile_id, address, facts, member_id)
    if live is not None:
        member, via = live
        params: IssueParams = {"campaign_id": str(member.campaign_id)}
        if via is not None:
            params["via_client_profile_id"] = str(via)
        issues.append(Issue(IN_OTHER_CAMPAIGN, params))
    return issues


def membership_issues(
    member: EmailCampaignMember, email: str | None, facts: ClientFacts
) -> list[Issue]:
    """A lead not launched yet: its client's issues, and its own state. A
    pause keeps it out of a launch; the last hand-over's failure is told (it
    is tried again, last)."""
    issues = client_issues(member.client_profile_id, email, facts, member_id=member.id)
    if member.status == "EMAIL_PAUSED":
        issues.append(Issue(PAUSED))
    elif member.status == "EMAIL_READY" and member.status_reason:
        issues.append(Issue(LAST_ATTEMPT, {"reason": member.status_reason}, "warning"))
    return issues


def not_launched(member: EmailCampaignMember) -> bool:
    """Still to launch: never handed to the provider (a dry run's hand-over
    counts as one), and still live in its campaign."""
    return member.queued_at is None and member.status in ("EMAIL_READY", "EMAIL_PAUSED")


# ── the campaign: what holds every lead back ────────────────────────────


def team_mismatch(campaign: EmailCampaign, key_team_id: str | None) -> bool:
    """Whether a campaign that exists at Lemlist lives in another team than
    the one the platform key opens now (rule N4): it is then read-only.

    A campaign Lemlist does not know (never synced, or a dry-run one)
    belongs to no team. A real one carries the team it was created in
    (`provider_metadata[CAMPAIGN_TEAM_KEY]`); without that stamp its team is
    unknown, and unknown is never "the same team" (A4): a mismatch.
    `key_team_id`: the key's team, None when real effects are off (then
    EXTERNAL_EFFECTS_OFF holds the campaign, nothing is compared)."""
    if not is_real_provider_id(campaign.provider_campaign_id):
        return False
    stamped = (campaign.provider_metadata or {}).get(CAMPAIGN_TEAM_KEY)
    if not isinstance(stamped, str) or not stamped:
        return True
    return key_team_id is not None and stamped != key_team_id


def campaign_gates(
    campaign: EmailCampaign,
    steps: Sequence[EmailCampaignStep],
    *,
    external: bool,
    key_team_id: str | None,
    launching: bool,
) -> list[Issue]:
    """What stops the whole campaign, in the order the sync checks it. Only
    a launch needs the campaign active; a sync refreshes a paused one too.
    `external`: the agency's provider talks to Lemlist for real."""
    gates: list[Issue] = []
    if team_mismatch(campaign, key_team_id):
        gates.append(Issue(TEAM_MISMATCH))
    if campaign.status == "ARCHIVED":
        gates.append(Issue(ARCHIVED))
    if not steps:
        gates.append(Issue(NO_STEPS))
    if is_real_provider_id(campaign.provider_campaign_id) and not external:
        gates.append(Issue(EXTERNAL_EFFECTS_OFF))
    if launching and campaign.status not in ("ACTIVE", "ARCHIVED"):
        gates.append(Issue(NOT_ACTIVE))
    return gates


def daily_limit_gate(launched_today: int, daily_limit: int) -> Issue | None:
    """The campaign's daily limit is reached: the next leads launch tomorrow."""
    if launched_today < daily_limit:
        return None
    return Issue(DAILY_LIMIT, {"launched_today": launched_today, "daily_limit": daily_limit})


def sender_gate(sender: object | None) -> Issue | None:
    """No sending mailbox chosen in Nidria: a warning only, the team's sole
    sender sends (Prism's behaviour; the sync stops if there are several)."""
    return None if sender else Issue(NO_SENDER, level="warning")


def day_start(timezone: str, now: datetime | None = None) -> datetime:
    """Midnight today in the campaign's timezone (the daily limit's day)."""
    local = (now or datetime.now(UTC)).astimezone(zone_of(timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


# ── the emails: what a template needs from a client ──────────────────────

TemplateErrorKind = Literal[
    "missing_variable",
    "unknown_variable",
    "malformed_token",
    "ai_not_generated",
    "no_client",
    "no_company",
    "no_email",
    "first_subject_required",
    "empty_body",
]


@dataclass(frozen=True)
class TemplateError:
    """Why one step's email cannot be sent: the step's position and a code
    (the renderer's error kind, or the hand-over's own two rules)."""

    position: int
    kind: TemplateErrorKind
    token: str | None = None

    @property
    def params(self) -> IssueParams:
        params: IssueParams = {"position": self.position, "kind": self.kind}
        if self.token is not None:
            params["token"] = self.token
        return params


def render_errors(position: int, rendered: RenderResult) -> list[TemplateError]:
    """Why a rendered email cannot be sent: the hand-over's own rule, read
    by the Launch page on the same render without AI values."""
    errors = [TemplateError(position, e.kind, e.token) for e in rendered.errors]
    if position == 1 and not rendered.subject.strip():
        errors.append(TemplateError(position, "first_subject_required"))
    if not rendered.body.strip():
        errors.append(TemplateError(position, "empty_body"))
    return errors


def unsent_steps(
    steps: Iterable[EmailCampaignStep],
    renders: Mapping[int, EmailMemberRender],
    passed: int = 0,
) -> list[EmailCampaignStep]:
    """The steps a client can still be sent: after `passed`, not sent yet."""
    out = []
    for step in sorted(steps, key=lambda s: s.position):
        if step.position <= passed:
            continue
        render = renders.get(step.position)
        if render is not None and render.sent_at is not None:
            continue
        out.append(step)
    return out


def fresh_render(render: EmailMemberRender | None, step: EmailCampaignStep) -> bool:
    """A stored render that still matches its step (a preview freezes one)."""
    return (
        render is not None
        and render.campaign_step_id == step.id
        and render.template_hash == step.content_hash
    )


@dataclass(frozen=True)
class TemplateCheck:
    issues: list[Issue]
    #: The blocking problems, one per step and code (TEMPLATE issues' source).
    errors: list[TemplateError]
    #: Unsent steps already rendered for this client and still current (a
    #: member's preview freezes one): shown, never regenerated by a read.
    rendered: int
    total: int


def used_ai_keys(step: EmailCampaignStep) -> list[str]:
    """The step's AI variables its subject or body uses: the only ones the
    hand-over generates (one it does not use writes nothing of the email)."""
    used = {
        token.removeprefix("ai.")
        for token in tokens_in(step.subject_template, step.body_template)
        if token.startswith("ai.")
    }
    return [v.key for v in step.ai_variables if v.key in used]


def template_issues(
    steps: Sequence[EmailCampaignStep],
    stored: Mapping[int, EmailMemberRender],
    context_for: Callable[[list[str]], RenderContext],
    *,
    ai_configured: bool,
) -> TemplateCheck:
    """What the client's own emails lack, read without calling a model: each
    unsent step rendered with its client, company and agency values only (AI
    passages are written at launch, then frozen). A missing value without a
    fallback, an unknown variable, an empty first subject or body: blocking.
    An AI passage the subject or body uses and no frozen render holds yet
    is AI_AT_LAUNCH: a warning when AI generation is configured, BLOCKING
    when it is not (`ai_configured` False, decision A14), since the Launch
    would then refuse the client. `context_for(keys)`: the client's render
    context for a step whose AI variables are `keys`. Each step's
    `ai_variables` must be loaded."""
    issues: list[Issue] = []
    errors: list[TemplateError] = []
    rendered = 0
    ai_pending: int | None = None
    for step in unsent_steps(steps, stored):
        keys = [v.key for v in step.ai_variables]
        used = used_ai_keys(step)
        result = EmailTemplateRenderer(require_ai=False).render(
            step.subject_template, step.body_template, context_for(keys)
        )
        for error in render_errors(step.position, result):
            errors.append(error)
            issues.append(Issue(TEMPLATE, error.params))
        render = stored.get(step.position)
        pending = bool(used)
        if fresh_render(render, step):
            rendered += 1
            values = (
                render.ai_values
                if render is not None and isinstance(render.ai_values, dict)
                else {}
            )
            pending = any(
                not (isinstance(values.get(k), dict) and values[k].get("value")) for k in used
            )
        if pending and ai_pending is None:
            ai_pending = step.position
    if ai_pending is not None:
        if ai_configured:
            issues.append(Issue(AI_AT_LAUNCH, level="warning"))
        else:
            issues.append(
                Issue(AI_AT_LAUNCH, {"reason": "ai_not_configured", "position": ai_pending})
            )
    return TemplateCheck(issues, errors, rendered, len(steps))
