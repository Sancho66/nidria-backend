"""The import of the Lemlist campaigns already running in the agency's team
(lot C, Alexandre's decision of 08/10/2026): two steps, idempotent, and read
only at Lemlist.

An imported campaign is a mirror (origin LEMLIST_IMPORT): its content
belongs to Lemlist, which keeps sending it with its own variables, and
Nidria never pushes a shell nor a personalization into it (each lead would
receive the placeholder, spec 9.2). The few gestures it still takes are in
`email_manager` (`editable`); the Sync, the job and the webhook only read it
(`email_sync`).

1. `plan` (GET /email/import/lemlist), reads only. The team's campaigns
   that run or are paused at Lemlist, in the order an import processes them
   (Lemlist's `createdAt`, oldest first), each with what importing it would
   do, in counts only (never an address nor a person's name): its steps by
   Lemlist type (only the main sequence is imported: conditional branches
   and A/B steps are counted, the text kept is variant A's); its leads
   (listed, identified, without an address, sharing one); the client records
   found by address (rule N2) or to create; the conflicts (a person live in
   another campaign of the agency, or in an importable campaign planned
   before); every combination of Lemlist's words met (`state` of the
   listing, and the lead's status word labelled with its source: the
   export's `status`, else its `stateSystem`, else the contacts lookup's
   `leadState`) with the Nidria status proposed; the Lemlist calls it took
   and an application will take. Drafts, ended, archived and erroneous
   campaigns are listed apart, so are the team's campaigns Nidria created
   itself (never imported).
2. `apply` (POST /email/import/lemlist {campaign_ids}): the plan computed
   again server side (the listing and every importable campaign read again,
   since earlier campaigns decide the conflicts of later ones), then each
   campaign asked for, in the plan's order, in its own transaction; a
   failure undoes that campaign only, its code in the answer, and the next
   one goes on. Per campaign: the campaign (its status mapped from Lemlist's,
   Lemlist's word in `provider_status`, its ids and team), its steps (keyed
   by Lemlist step id, the raw step in `provider_content`), the missing
   client records (names and address as Lemlist gives them, `source`
   "lemlist", through the client records' own creation core: no account,
   invitation, email nor notification), the memberships (status mapped,
   Lemlist's `state` in `provider_status`, the lead and contact ids;
   Lemlist's company kept in the membership's import metadata and shown when
   the record has none: no company record is made), the campaign's whole
   history through `ingest`, its replies' text through the existing PENDING
   mechanism (the Email Sync job reads it), and the opposition list for the
   unsubscribed and bounced. Running it again updates without a duplicate
   (keys: the provider campaign id, the lead id; events and messages are
   stored once): statuses refreshed, never a block lifted, new leads added,
   steps refreshed by id.

Lemlist is only read (`ImportReader`, GET only; each route sourced in
notes/lemlist-import-research.txt): the team's campaigns (§6), one campaign
for its main sequence id (§4), its sequences (§5), its leads (§1.1: pages of
100 by offset, a lead listed twice kept once), their identities from the
campaign's export (§1.2 a: one answer, no pagination documented, so every
listed lead is checked to have come back), completed for the missing ones
by the contacts lookup (§1.2 d: 100 contact ids per call), and, at
application only, the campaign's activities (§3: pages of 100).

A lead's status is decided by `propose` over the written mapping `RULES`
(Lemlist's words in notes §2); a combination it does not cover is not
imported, and counted with its raw values. The real export gives no
`status` (notes §17): its undocumented `stateSystem` is read as the
documented status it stands for (`STATE_SYSTEM_STATUS`), and a value not
known there is not covered.

History and present. The current state read at Lemlist decides each
membership's status. The history is then replayed by `ingest` in its
`history` mode: it fills the record (times, the steps sent, the first send)
and the messages, and moves no status, so it never contradicts that state;
a past bounce or unsubscribe still lists the address on the opposition
list, which nothing lifts. Running the import again never lifts a block
either: a membership UNSUBSCRIBED or BOUNCED keeps it, whatever Lemlist says.

Concurrency. Each campaign's transaction holds the agency's import lock
(`EmailRepository.lock_import`) and the campaign's row, and decides again,
from the database as it is then, every record and membership to create: two
imports never create a campaign, a client record nor a membership twice.
The database also keeps one campaign per real provider campaign id
(`uq_email_campaign_provider_campaign_real`).

Nothing here logs, counts or answers an address or a person's name: plans
and results carry counts, Lemlist campaign ids and campaign names only.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.email_campaign import (
    BLOCKING_MEMBER_STATUSES,
    LIVE_MEMBER_STATUSES,
    ORIGIN_LEMLIST_IMPORT,
    PROVIDER_LEMLIST,
    STEP_TYPE_EMAIL,
    STEP_TYPE_OTHER,
    EmailCampaign,
    EmailCampaignMember,
    EmailCampaignStep,
)
from src.client_profiles.client_profiles_repository import ClientProfilesRepository
from src.core.exceptions import ConflictError
from src.core.rbac.permissions import Permission
from src.email_campaigns.email_manager import (
    IMPORT_METADATA_KEY,
    STATUS_REASON_PARAMS_KEY,
    Access,
    EmailManager,
    import_metadata,
    provider_error_code,
    provider_refusal,
    set_status_reason,
)
from src.email_campaigns.email_schema import (
    EmailCampaignSettings,
    EmailImportCallsRead,
    EmailImportCampaignPlan,
    EmailImportCampaignRef,
    EmailImportCampaignResult,
    EmailImportLeadsRead,
    EmailImportPlanCallsRead,
    EmailImportPlanRead,
    EmailImportRequest,
    EmailImportResult,
    EmailImportRuleRead,
    EmailImportStateRead,
    EmailImportStepsRead,
    ImportRule,
    ImportSkipReason,
    ImportStatusSource,
    MemberStatus,
    OppositionReason,
)
from src.email_campaigns.email_sync import (
    REASON_BOUNCED,
    REASON_PAUSED_AT_PROVIDER,
    REASON_UNSUBSCRIBED_AT_PROVIDER,
    EmailSyncManager,
)
from src.email_campaigns.flags import external_effects_on, provider_state_for
from src.email_campaigns.lemlist_team import CAMPAIGN_TEAM_KEY
from src.email_campaigns.providers.base import (
    CampaignRef,
    ImportReader,
    ProviderActivityRead,
    ProviderCampaignListing,
    ProviderCampaignRow,
    ProviderContact,
    ProviderError,
    ProviderLeadIdentity,
    ProviderSequences,
)
from src.email_campaigns.renderer import content_hash
from src.email_campaigns.suppression import REASON_OF_STATUS, email_hash

logger = logging.getLogger(__name__)

#: The `source` of a client record the import creates (its CRM block).
SOURCE_LEMLIST = "lemlist"
#: The permission `POST /client-profiles` takes (its binding in
#: `client_profiles_router.BINDINGS`, a test keeps them equal): the import
#: creates client records.
CLIENT_PROFILE_CREATE = Permission.CASE_EDIT
#: Lemlist's campaign statuses an import takes, and the Nidria status each
#: becomes; the others (draft, ended, archived, errors) are listed apart.
_CAMPAIGN_STATUS = {"running": "ACTIVE", "paused": "PAUSED"}
#: A campaign the import does not take (params `status`, Lemlist's word).
NOT_IMPORTABLE = "email.import_not_importable"
#: Rows per page of a Lemlist listing, and contact ids per lookup.
_PAGE = 100
_LOOKUP = 100
#: What a client record keeps of a name, an address, a provider id.
_NAME_MAX = 100
_EMAIL_MAX = 254
_ID_MAX = 100
_COMPANY_MAX = 255
#: An imported step's own templates: empty (its content is Lemlist's).
_EMPTY_HASH = content_hash("", "", {})
_LIVE = frozenset(LIVE_MEMBER_STATUSES)
_BLOCKING = frozenset(BLOCKING_MEMBER_STATUSES)

# ── the mapping: Lemlist's words on a lead -> its Nidria status ──────────────
#
# `state`: the lead's last state (Lemlist's listing; else the export's
# `state`, or its documented `lastState`), an activity type or a global
# state, no published enum.
# `status`: documented, notInterested, interested, unsubscribed, review,
# scanning, running, paused, done (notes §2.1). The real export never gives
# it: its `stateSystem` stands for it (`STATE_SYSTEM_STATUS`).

#: Unsubscribed: the email's, the contact's (`entityUnsubscribed` since
#: 30/03/2026), by the API or by hand. `variableUnsubscribed` is not one:
#: Lemlist does not say which variable (lot B never applies it either).
UNSUBSCRIBED_STATES = frozenset(
    {"emailsUnsubscribed", "entityUnsubscribed", "apiUnsubscribed", "manualUnsubscribed"}
)
BOUNCED_STATES = frozenset({"emailsBounced"})
REPLIED_STATES = frozenset({"emailsReplied"})
#: Skipped during Lemlist's review.
SKIPPED_STATES = frozenset({"skipped"})
PAUSED_STATES = frozenset({"paused"})
#: Launched, not contacted yet: `reviewed` (launching a lead is reviewing
#: it, notes §2.2), and the states before any step (`scanned`, `imported`).
LAUNCHED_STATES = frozenset({"reviewed", "scanned", "imported"})
#: An email sent, opened or clicked: the detailed states and the global ones
#: grouping them.
CONTACTED_STATES = frozenset(
    {"emailsSent", "emailsOpened", "emailsClicked", "contacted", "hooked", "attracted"}
)
#: The export's `stateSystem` -> the documented status it is read as, when
#: the export gives no `status`. Observed on 08/10/2026 on the real team,
#: undocumented (notes §17): the real export carries `state` and
#: `stateSystem`, never `status` nor `lastState`. Each value, with the
#: states it came with there. Any other value is not covered.
STATE_SYSTEM_STATUS: dict[str, str] = {
    # emailsSent, emailsClicked: in the sequence.
    "inProgress": "running",
    # reviewed: launched, never contacted; running, so that rule `launched`
    # makes it EMAIL_QUEUED.
    "reviewed": "running",
    # Mostly emailsReplied (Lemlist pauses a lead at its reply): rule
    # `replied` comes first.
    "paused": "paused",
    # emailsBounced (2 of 2, 5 of 5): rule `bounced` comes first.
    "done": "done",
    # entityUnsubscribed.
    "unsubscribed": "unsubscribed",
}


@dataclass(frozen=True)
class Rule:
    """One line of the mapping. A lead matches when its `state` is in
    `states` or its `status` in `statuses` (`combine` either), or both;
    `proposed`: its Nidria status, or None and why it is not imported."""

    rule: ImportRule
    states: frozenset[str] = frozenset()
    statuses: frozenset[str] = frozenset()
    combine: Literal["either", "both"] = "either"
    proposed: MemberStatus | None = None
    not_imported: ImportSkipReason | None = None

    def matches(self, state: str | None, status: str | None) -> bool:
        in_state = state in self.states
        in_status = status in self.statuses
        return in_state and in_status if self.combine == "both" else in_state or in_status

    @property
    def opposition(self) -> OppositionReason | None:
        """Why the lead's address goes on the opposition list, if it does."""
        reason = REASON_OF_STATUS.get(self.proposed or "")
        return cast(OppositionReason, reason) if reason else None


#: The mapping, strongest first: the first rule matching decides. Blocks and
#: replies come from the state (a fact whatever the status); then what
#: Lemlist did not launch; then the status, read with the state for a
#: running lead (launched but not contacted, or in its sequence).
RULES: tuple[Rule, ...] = (
    Rule(
        "unsubscribed",
        UNSUBSCRIBED_STATES,
        frozenset({"unsubscribed"}),
        proposed="EMAIL_UNSUBSCRIBED",
    ),
    Rule("bounced", BOUNCED_STATES, proposed="EMAIL_BOUNCED"),
    Rule("replied", REPLIED_STATES, proposed="EMAIL_REPLIED"),
    Rule("skipped", SKIPPED_STATES, not_imported="skipped_at_provider"),
    Rule("not_launched", statuses=frozenset({"review", "scanning"}), not_imported="not_launched"),
    Rule("completed", statuses=frozenset({"done"}), proposed="EMAIL_COMPLETED"),
    Rule("paused", PAUSED_STATES, frozenset({"paused"}), proposed="EMAIL_PAUSED"),
    Rule("launched", LAUNCHED_STATES, frozenset({"running"}), "both", proposed="EMAIL_QUEUED"),
    Rule("in_sequence", CONTACTED_STATES, frozenset({"running"}), "both", proposed="EMAIL_ACTIVE"),
)
#: No rule matched: neither a status nor a `stateSystem` was read, or a
#: combination not covered (interested, notInterested, another channel's
#: state, a `stateSystem` value not known...).
STATUS_UNKNOWN = Rule("status_unknown", not_imported="status_unknown")
UNCOVERED = Rule("uncovered", not_imported="status_uncovered")


def read_status(status: str | None, state_system: str | None) -> str | None:
    """The status the mapping reads for a lead: Lemlist's `status` when it
    gives one, else its `stateSystem` read as the status it stands for
    (`STATE_SYSTEM_STATUS`); None for none, or a value not known there."""
    if status is not None:
        return status
    return STATE_SYSTEM_STATUS.get(state_system) if state_system is not None else None


def propose(state: str | None, status: str | None, state_system: str | None = None) -> Rule:
    """The rule that decides a lead's fate from Lemlist's words on it: its
    `state`, and its `status`, else its `stateSystem` (`read_status`). With
    neither word, `status_unknown`; a word no rule covers (a `stateSystem`
    value not known included), `uncovered`."""
    read_as = read_status(status, state_system)
    for rule in RULES:
        if rule.matches(state, read_as):
            return rule
    return STATUS_UNKNOWN if status is None and state_system is None else UNCOVERED


def rule_read(rule: Rule) -> EmailImportRuleRead:
    return EmailImportRuleRead(
        rule=rule.rule,
        states=sorted(rule.states),
        statuses=sorted(rule.statuses),
        combine=rule.combine,
        proposed=rule.proposed,
        opposition=rule.opposition,
        not_imported=rule.not_imported,
    )


# ── what Lemlist says, read in a worker thread (the adapter is synchronous) ───


@dataclass
class LeadRead:
    """One lead of a Lemlist campaign, as the import reads it."""

    lead_id: str
    contact_id: str | None
    #: The listing's `state`, else the export's (`state`, else `lastState`).
    state: str | None
    #: Its status as Lemlist words it: the export's `status`, else (none
    #: there, nor a `stateSystem`) the contacts lookup's `leadState`.
    status: str | None
    #: The export's `stateSystem`, as said (`STATE_SYSTEM_STATUS`).
    state_system: str | None = None
    #: Where the word its status is read from comes from: "status",
    #: "stateSystem", "leadState", or None (no word).
    status_source: ImportStatusSource | None = None
    #: Trimmed and lower-cased; None when Lemlist gave none usable.
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    company_name: str | None = None
    #: Where its identity came from: "export", "contacts", or None.
    identity: str | None = None

    @property
    def status_word(self) -> str | None:
        """The word its status is read from (`status_source`'s), as said."""
        return self.state_system if self.status_source == "stateSystem" else self.status

    @property
    def rule(self) -> Rule:
        return propose(self.state, self.status, self.state_system)


@dataclass
class CampaignRead:
    """One importable Lemlist campaign as the import read it. `error`: the
    `email.provider_*` code of the read that failed (not importable now).
    `reads`: the calls it took, by the plan's definition."""

    row: ProviderCampaignRow
    name: str | None = None
    status: str | None = None
    timezone: str | None = None
    error: str | None = None
    sequences: ProviderSequences | None = None
    leads: list[LeadRead] = field(default_factory=list)
    listing_complete: bool = True
    listing_duplicates: int = 0
    reads: int = 0

    @property
    def campaign_id(self) -> str:
        return self.row.campaign_id

    @property
    def ref(self) -> CampaignRef:
        main = self.sequences.main_sequence_id if self.sequences is not None else None
        return CampaignRef(self.row.campaign_id, main)


def _text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    cut = value.strip()[:limit].strip()
    return cut or None


def _address(value: str | None) -> str | None:
    """An address as the import keeps it: trimmed, lower-cased; None when
    empty, too long for a client record, or without its @."""
    if value is None:
        return None
    address = value.strip().lower()
    return address if address and "@" in address and len(address) <= _EMAIL_MAX else None


def _created(row: ProviderCampaignRow) -> tuple[bool, datetime, str]:
    """The import's order: Lemlist's creation time, oldest first (a campaign
    without a readable one last), then the id."""
    at: datetime | None = None
    if row.created_at:
        try:
            at = datetime.fromisoformat(row.created_at.replace("Z", "+00:00"))
        except ValueError:
            at = None
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return (at is None, at or datetime.max.replace(tzinfo=UTC), row.campaign_id)


#: A lead the export did not return.
_NO_IDENTITY = ProviderLeadIdentity(lead_id="", email=None)


def read_campaign(reader: ImportReader, row: ProviderCampaignRow) -> CampaignRead:
    """One campaign's reads: the campaign (its main sequence id), its
    sequences, its listing, its export and, for the leads the export did not
    identify, the contacts lookup. A failed read is kept as the campaign's
    `error`, never raised."""
    read = CampaignRead(row, name=row.name, status=row.status)
    try:
        read.reads += 1
        detail = reader.read_campaign(CampaignRef(row.campaign_id))
        if not detail.exists:
            read.error = "email.provider_campaign_missing"
            return read
        name = detail.metadata.get("name")
        read.name = name.strip() if isinstance(name, str) and name.strip() else row.name
        read.status = detail.status or row.status
        if (read.status or "") not in _CAMPAIGN_STATUS:
            # Ended, archived or back to a draft since it was listed.
            read.error = NOT_IMPORTABLE
            return read
        zone = detail.metadata.get("timezone")
        read.timezone = zone if isinstance(zone, str) else None
        ref = CampaignRef(row.campaign_id, detail.sequence_id)
        read.reads += 1
        read.sequences = reader.read_sequences(ref)
        read.reads += 1
        listing = reader.list_contacts(ref)
        # One page per 100 leads, the short (or empty) one that ends it
        # counted just above; at the page cap, no such last page.
        read.reads += len(listing.leads) // _PAGE - (0 if listing.complete else 1)
        read.listing_complete = listing.complete
        listed: dict[str, tuple[str | None, str | None]] = {}
        for lead in listing.leads:
            if lead.lead_id in listed:
                read.listing_duplicates += 1
            elif len(lead.lead_id) <= _ID_MAX:
                contact = (
                    lead.contact_id if lead.contact_id and len(lead.contact_id) <= _ID_MAX else None
                )
                listed[lead.lead_id] = (contact, lead.state)
        read.reads += 1
        identities: dict[str, ProviderLeadIdentity] = {
            identity.lead_id: identity for identity in reader.export_leads(ref)
        }
        missing = sorted(
            {
                contact
                for lead_id, (contact, _state) in listed.items()
                if contact and not _address((identities.get(lead_id) or _NO_IDENTITY).email)
            }
        )
        contacts: dict[str, ProviderContact] = {}
        if missing:
            read.reads += math.ceil(len(missing) / _LOOKUP)
            contacts = {c.contact_id: c for c in reader.lookup_contacts(missing)}
        read.leads = [
            _lead(row.campaign_id, lead_id, contact, state, identities.get(lead_id), contacts)
            for lead_id, (contact, state) in listed.items()
        ]
    except ProviderError as exc:
        read.error = provider_error_code(exc)
    return read


def _lead(
    campaign_id: str,
    lead_id: str,
    contact_id: str | None,
    state: str | None,
    identity: ProviderLeadIdentity | None,
    contacts: Mapping[str, ProviderContact],
) -> LeadRead:
    """A listed lead with its identity: the export's, else the contacts
    lookup's (which carries no company); its status word, the export's
    `status`, else its `stateSystem`, else the lead's status in this
    campaign by the lookup."""
    contact = contacts.get(contact_id) if contact_id else None
    found = identity or _NO_IDENTITY
    looked_up = contact.lead_statuses.get((campaign_id, lead_id)) if contact is not None else None
    status: str | None = None
    source: ImportStatusSource | None = None
    if found.status is not None:
        status, source = found.status, "status"
    elif found.state_system is not None:
        source = "stateSystem"
    elif looked_up is not None:
        status, source = looked_up, "leadState"
    lead = LeadRead(
        lead_id=lead_id,
        contact_id=contact_id,
        state=state or found.last_state,
        status=status,
        state_system=found.state_system,
        status_source=source,
        company_name=_text(found.company_name, _COMPANY_MAX),
    )
    if _address(found.email):
        lead.email, lead.identity = _address(found.email), "export"
        lead.first_name = _text(found.first_name, _NAME_MAX)
        lead.last_name = _text(found.last_name, _NAME_MAX)
    elif contact is not None and _address(contact.email):
        lead.email, lead.identity = _address(contact.email), "contacts"
        lead.first_name = _text(contact.first_name, _NAME_MAX)
        lead.last_name = _text(contact.last_name, _NAME_MAX)
    elif identity is not None:
        lead.identity = "export"
    return lead


def read_campaigns(reader: ImportReader, rows: Sequence[ProviderCampaignRow]) -> list[CampaignRead]:
    return [read_campaign(reader, row) for row in rows]


# ── what Nidria knows, and the plan ──────────────────────────────────────────


@dataclass
class Known:
    """The team's campaigns sorted by what Nidria knows of them."""

    #: Running or paused at Lemlist and not Nidria's own: in import order.
    importable: list[ProviderCampaignRow]
    not_importable: list[ProviderCampaignRow]
    created_by_nidria: list[ProviderCampaignRow]
    #: Lemlist campaign id -> the agency's campaign imported from it.
    imported: dict[str, EmailCampaign]


@dataclass
class Facts:
    """What the agency's database says of the people being planned."""

    #: Address -> the agency's client record holding it (rule N2).
    profiles: dict[str, uuid.UUID] = field(default_factory=dict)
    #: Live memberships of those records, and of any record by address.
    live_by_profile: dict[uuid.UUID, list[EmailCampaignMember]] = field(default_factory=dict)
    live_by_address: dict[str, list[EmailCampaignMember]] = field(default_factory=dict)
    #: An imported campaign's open memberships, by lead id and by record.
    open_by_lead: dict[uuid.UUID, dict[str, EmailCampaignMember]] = field(default_factory=dict)
    open_by_profile: dict[uuid.UUID, dict[uuid.UUID, EmailCampaignMember]] = field(
        default_factory=dict
    )


@dataclass
class Verdict:
    """What the import does with one lead: create its membership, refresh
    the one it has (`status`: the new one, None to keep it), or skip it."""

    lead: LeadRead
    rule: Rule
    action: Literal["create", "update", "skip"]
    reason: ImportSkipReason | None = None
    profile_id: uuid.UUID | None = None
    member: EmailCampaignMember | None = None
    status: MemberStatus | None = None
    #: An update whose new status would make the person live twice: kept.
    conflict: bool = False
    #: A record to create that an importable campaign planned before
    #: creates already (it exists by the time this one is imported).
    record_planned: bool = False


@dataclass
class CampaignPlan:
    read: CampaignRead
    campaign: EmailCampaign | None
    verdicts: list[Verdict] = field(default_factory=list)
    #: What the importable campaigns planned before it took live: an
    #: address or a record there is a `conflict_in_plan` here.
    taken_before: tuple[frozenset[str], frozenset[uuid.UUID]] = (frozenset(), frozenset())


def plan_campaign(
    read: CampaignRead,
    campaign: EmailCampaign | None,
    facts: Facts,
    taken_addresses: set[str],
    taken_profiles: set[uuid.UUID],
    planned_records: set[str] | None = None,
) -> CampaignPlan:
    """Each lead's verdict, in a fixed order (the leads already imported
    first, then by lead id), and what the campaign takes live (added to
    `taken_*` for the campaigns planned after it) or creates
    (`planned_records`, the addresses whose record a campaign planned before
    creates). Pure: reads `facts`."""
    planned_records = planned_records if planned_records is not None else set()
    plan = CampaignPlan(
        read, campaign, taken_before=(frozenset(taken_addresses), frozenset(taken_profiles))
    )
    own_by_lead = facts.open_by_lead.get(campaign.id, {}) if campaign is not None else {}
    own_by_profile = facts.open_by_profile.get(campaign.id, {}) if campaign is not None else {}
    seen: set[str] = set()
    for lead in sorted(
        read.leads, key=lambda lead: (lead.lead_id not in own_by_lead, lead.lead_id)
    ):
        rule = lead.rule
        address = lead.email
        member = own_by_lead.get(lead.lead_id)
        verdict: Verdict
        if member is not None:
            verdict = _refresh(lead, rule, member, facts, taken_addresses, taken_profiles)
        elif address is None:
            verdict = Verdict(lead, rule, "skip", "no_email")
        elif rule.proposed is None:
            verdict = Verdict(lead, rule, "skip", rule.not_imported)
        elif address in seen:
            verdict = Verdict(lead, rule, "skip", "duplicate_email")
        else:
            profile_id = facts.profiles.get(address)
            live = [
                *(facts.live_by_profile.get(profile_id, []) if profile_id else []),
                *facts.live_by_address.get(address, []),
            ]
            here = campaign.id if campaign is not None else None
            if any(m.campaign_id != here for m in live):
                verdict = Verdict(lead, rule, "skip", "conflict")
            elif (profile_id is not None and profile_id in own_by_profile) or live:
                verdict = Verdict(lead, rule, "skip", "already_in_campaign")
            elif address in taken_addresses or (
                profile_id is not None and profile_id in taken_profiles
            ):
                verdict = Verdict(lead, rule, "skip", "conflict_in_plan")
            else:
                verdict = Verdict(lead, rule, "create", profile_id=profile_id)
                if profile_id is None:
                    verdict.record_planned = address in planned_records
                    planned_records.add(address)
        if address is not None:
            seen.add(address)
        final = verdict.status or (verdict.member.status if verdict.member else rule.proposed)
        if verdict.action != "skip" and final in _LIVE:
            if address is not None:
                taken_addresses.add(address)
            if verdict.profile_id is not None:
                taken_profiles.add(verdict.profile_id)
        plan.verdicts.append(verdict)
    return plan


def _refresh(
    lead: LeadRead,
    rule: Rule,
    member: EmailCampaignMember,
    facts: Facts,
    taken_addresses: set[str],
    taken_profiles: set[uuid.UUID],
) -> Verdict:
    """An imported membership refreshed: its status becomes the one Lemlist's
    words now map to, except a block (never lifted), a status not covered
    (kept), and a live one while the person lives elsewhere (kept)."""
    verdict = Verdict(lead, rule, "update", profile_id=member.client_profile_id, member=member)
    current, proposed = member.status, rule.proposed
    if current in _BLOCKING or proposed is None or proposed == current:
        return verdict
    if proposed in _LIVE and current not in _LIVE:
        elsewhere = [
            m for m in facts.live_by_profile.get(member.client_profile_id, []) if m.id != member.id
        ]
        if (
            elsewhere
            or member.client_profile_id in taken_profiles
            or (lead.email is not None and lead.email in taken_addresses)
        ):
            verdict.conflict = True
            return verdict
    verdict.status = proposed
    return verdict


def plan_campaigns(
    reads: Sequence[CampaignRead], imported: Mapping[str, EmailCampaign], facts: Facts
) -> list[CampaignPlan]:
    """Every importable campaign planned in order, each knowing what the
    ones before it took live."""
    taken_addresses: set[str] = set()
    taken_profiles: set[uuid.UUID] = set()
    planned_records: set[str] = set()
    plans: list[CampaignPlan] = []
    for read in reads:
        campaign = imported.get(read.campaign_id)
        if read.error is not None:
            plans.append(
                CampaignPlan(
                    read,
                    campaign,
                    taken_before=(frozenset(taken_addresses), frozenset(taken_profiles)),
                )
            )
            continue
        plans.append(
            plan_campaign(read, campaign, facts, taken_addresses, taken_profiles, planned_records)
        )
    return plans


def _skipped(plan: CampaignPlan) -> Counter[str]:
    return Counter(v.reason for v in plan.verdicts if v.action == "skip" and v.reason)


def _leads_read(plan: CampaignPlan) -> EmailImportLeadsRead:
    leads = plan.read.leads
    creates = [v for v in plan.verdicts if v.action == "create"]
    skipped = _skipped(plan)
    return EmailImportLeadsRead(
        listed=len(leads),
        listing_complete=plan.read.listing_complete,
        listing_duplicates=plan.read.listing_duplicates,
        identities=sum(1 for lead in leads if lead.identity),
        from_export=sum(1 for lead in leads if lead.identity == "export"),
        from_contacts=sum(1 for lead in leads if lead.identity == "contacts"),
        without_email=sum(1 for lead in leads if lead.email is None),
        existing_profiles=sum(1 for v in creates if v.profile_id is not None),
        profiles_to_create=sum(1 for v in creates if v.profile_id is None and not v.record_planned),
        profiles_planned_before=sum(1 for v in creates if v.record_planned),
        conflicts=skipped["conflict"],
        conflicts_in_plan=skipped["conflict_in_plan"],
        duplicate_emails=skipped["duplicate_email"],
        to_create=len(creates),
        to_update=sum(1 for v in plan.verdicts if v.action == "update"),
        not_imported=dict(sorted(skipped.items())),
    )


def _states_read(plan: CampaignPlan) -> list[EmailImportStateRead]:
    """Every combination met: the state, the status word as said and its
    source, the status the mapping read it as, the rule."""
    counts = Counter((lead.state, lead.status_word, lead.status_source) for lead in plan.read.leads)
    out: list[EmailImportStateRead] = []
    for (state, word, source), count in sorted(
        counts.items(),
        key=lambda item: (-item[1], item[0][0] or "", item[0][1] or "", item[0][2] or ""),
    ):
        status, state_system = (None, word) if source == "stateSystem" else (word, None)
        rule = propose(state, status, state_system)
        out.append(
            EmailImportStateRead(
                state=state,
                status=word,
                status_source=source,
                status_read_as=read_status(status, state_system),
                count=count,
                rule=rule.rule,
                proposed=rule.proposed,
                opposition=rule.opposition,
            )
        )
    return out


def _steps_read(sequences: ProviderSequences) -> EmailImportStepsRead:
    return EmailImportStepsRead(
        by_type=dict(sorted(Counter(s.type or "unknown" for s in sequences.steps).items())),
        imported=len(sequences.steps),
        branches=sequences.branches,
        branch_steps=sequences.branch_steps,
        ab_tests=sum(1 for s in sequences.steps if s.ab_test),
    )


def _ref(row: ProviderCampaignRow) -> EmailImportCampaignRef:
    return EmailImportCampaignRef(
        provider_campaign_id=row.campaign_id, name=row.name, status=row.status
    )


def _settings(timezone: str | None) -> dict[str, Any]:
    """An imported campaign's settings: Nidria's defaults, in Lemlist's
    timezone when it names a valid one (its days are dated in it)."""
    try:
        return EmailCampaignSettings(**({"timezone": timezone} if timezone else {})).model_dump()
    except PydanticValidationError:
        return EmailCampaignSettings().model_dump()


def _member_metadata(
    lead: LeadRead, current: Mapping[str, Any] | None, imported_at: str
) -> dict[str, Any]:
    """A membership's provider metadata after an import: Lemlist's raw words
    on the lead, the reason params Nidria noted, and the import's own notes
    (Lemlist's company, its words when imported, when)."""
    noted = {
        **import_metadata(current),
        "imported_at": imported_at,
        "status": lead.status,
        "state_system": lead.state_system,
        "last_state": lead.state,
    }
    if lead.company_name:
        noted["company_name"] = lead.company_name
    raw = {
        key: value
        for key, value in (
            ("state", lead.state),
            ("contactId", lead.contact_id),
            ("status", lead.status),
            ("stateSystem", lead.state_system),
        )
        if value is not None
    }
    kept = {k: v for k, v in (current or {}).items() if k == STATUS_REASON_PARAMS_KEY}
    return {**raw, **kept, IMPORT_METADATA_KEY: noted}


def _set_status(member: EmailCampaignMember, status: MemberStatus, now: datetime) -> None:
    """A membership's status as the import maps it, with what goes along: a
    pause's time (Lemlist's own is not known) and the reason codes the
    Email Sync job writes for the same states."""
    member.status = status
    if status == "EMAIL_PAUSED":
        member.paused_at = member.paused_at or now
        member.paused_by_agent_id = None
        set_status_reason(member, REASON_PAUSED_AT_PROVIDER)
        return
    member.paused_at = None
    member.paused_by_agent_id = None
    if status == "EMAIL_UNSUBSCRIBED":
        set_status_reason(member, REASON_UNSUBSCRIBED_AT_PROVIDER)
    elif status == "EMAIL_BOUNCED":
        set_status_reason(member, REASON_BOUNCED)
    else:
        set_status_reason(member, None)


# ── the manager ──────────────────────────────────────────────────────────────


class EmailImportManager:
    """The import of the agency's running Lemlist campaigns (see the module
    docstring). One instance serves one request."""

    def __init__(self, db: AsyncSession, manager: EmailManager | None = None) -> None:
        self.db = db
        self.manager = manager or EmailManager(db)
        self.repo = self.manager.repo
        self.sync = EmailSyncManager(db, self.manager)

    # ── the two routes ──────────────────────────────────────────────────

    async def plan(self, agent: Agent) -> EmailImportPlanRead:
        """What importing each running campaign of the team would do (reads
        only, at Lemlist and in Nidria)."""
        access, reader, _team = await self._open(agent)
        try:
            listing = await self._list(reader)
            known = await self._known(access, listing)
            reads = await asyncio.to_thread(read_campaigns, reader, known.importable)
            facts = await self._facts(access.agency_id, reads, known.imported, lock=False)
            plans = plan_campaigns(reads, known.imported, facts)
            reads_total = sum(read.reads for read in reads)
            return EmailImportPlanRead(
                listing_complete=listing.complete,
                campaigns=[self._campaign_plan(plan) for plan in plans],
                not_importable=[_ref(row) for row in known.not_importable],
                created_by_nidria=[_ref(row) for row in known.created_by_nidria],
                rules=[rule_read(rule) for rule in (*RULES, STATUS_UNKNOWN, UNCOVERED)],
                calls=EmailImportPlanCallsRead(
                    listing_pages=listing.pages,
                    reads=reads_total,
                    total=listing.pages + reads_total,
                    apply_min=listing.pages
                    + reads_total
                    + sum(1 for read in reads if read.error is None),
                ),
            )
        finally:
            await self.db.rollback()

    async def apply(self, agent: Agent, request: EmailImportRequest) -> EmailImportResult:
        """Import the campaigns asked for, each in its own transaction, in
        the plan's order (computed again here)."""
        access, reader, team = await self._open(agent)
        listing = await self._list(reader)
        known = await self._known(access, listing)
        reads = await asyncio.to_thread(read_campaigns, reader, known.importable)
        facts = await self._facts(access.agency_id, reads, known.imported, lock=False)
        plans = plan_campaigns(reads, known.imported, facts)
        # The plan's reads end here: each campaign is its own transaction.
        await self.db.rollback()
        calls = listing.pages + sum(read.reads for read in reads)
        wanted = set(request.campaign_ids)
        results: dict[str, EmailImportCampaignResult] = {}
        for plan in plans:
            campaign_id = plan.read.campaign_id
            if campaign_id not in wanted:
                continue
            if plan.read.error is not None:
                results[campaign_id] = _refused(
                    campaign_id,
                    plan.read.name,
                    plan.read.error,
                    {"status": plan.read.status or "unknown"}
                    if plan.read.error == NOT_IMPORTABLE
                    else None,
                )
                continue
            result, pages = await self._import(access, reader, team, plan)
            results[campaign_id] = result
            calls += pages
        listed = {row.campaign_id: row for row in listing.campaigns}
        nidria = {row.campaign_id for row in known.created_by_nidria}
        for campaign_id in request.campaign_ids:
            if campaign_id in results:
                continue
            row = listed.get(campaign_id)
            if row is None:
                results[campaign_id] = _refused(
                    campaign_id, None, "email.import_campaign_not_found"
                )
            elif campaign_id in nidria:
                results[campaign_id] = _refused(
                    campaign_id, row.name, "email.import_created_by_nidria"
                )
            else:
                results[campaign_id] = _refused(
                    campaign_id, row.name, NOT_IMPORTABLE, {"status": row.status or "unknown"}
                )
        return EmailImportResult(campaigns=list(results.values()), calls=calls)

    # ── access and Lemlist ──────────────────────────────────────────────

    async def _open(self, agent: Agent) -> tuple[Access, ImportReader, str]:
        """The agent, its rights (email.sync, email.manage, and creating
        client records), and the agency's Lemlist read for real: the module
        on, the agency bound to the team the platform key opens, real
        effects on. Otherwise the module's 409s: `email.module_disabled`,
        `email.team_mismatch`, `email.provider_unavailable` {reason:
        external_effects_off, agency_unbound, team_unknown, not_configured}."""
        access = await self.manager.access(agent, Permission.EMAIL_SYNC)
        access.require(Permission.EMAIL_MANAGE)
        access.require(CLIENT_PROFILE_CREATE)
        provider = await self.manager.provider(access)
        if not provider.external or not isinstance(provider, ImportReader):
            raise await self._unavailable(access, provider.external)
        try:
            team = await asyncio.to_thread(provider.team_id)
        except ProviderError as exc:
            raise provider_refusal(exc) from exc
        if not team:
            raise ConflictError(
                "Lemlist did not say which team the platform key opens.",
                code="email.provider_unavailable",
                params={"reason": "team_unknown"},
            )
        return access, provider, team

    async def _unavailable(self, access: Access, external: bool) -> ConflictError:
        settings = self.manager.settings
        state = await asyncio.to_thread(provider_state_for, access.module, settings)
        if state.team_mismatch:
            return ConflictError(
                "The platform key opens another Lemlist team than the agency's.",
                code="email.team_mismatch",
            )
        if external:
            reason = "not_configured"
        elif not external_effects_on(settings):
            reason = "external_effects_off"
        elif access.module.provider_team_id is None:
            reason = "agency_unbound"
        else:
            reason = "team_unknown"
        return ConflictError(
            "An import reads the agency's Lemlist team for real, which is not possible now.",
            code="email.provider_unavailable",
            params={"reason": reason},
        )

    async def _list(self, reader: ImportReader) -> ProviderCampaignListing:
        try:
            return await asyncio.to_thread(reader.list_campaigns)
        except ProviderError as exc:
            raise provider_refusal(exc) from exc

    async def _known(self, access: Access, listing: ProviderCampaignListing) -> Known:
        found = await self.repo.campaigns_by_provider_ids(
            PROVIDER_LEMLIST, [row.campaign_id for row in listing.campaigns]
        )
        imported = {
            str(c.provider_campaign_id): c
            for c in found
            if c.agency_id == access.agency_id and c.origin == ORIGIN_LEMLIST_IMPORT
        }
        # Made by Nidria (here or, never in practice, in another agency).
        nidria = {str(c.provider_campaign_id) for c in found} - set(imported)
        known = Known([], [], [], imported)
        for row in sorted(listing.campaigns, key=_created):
            if row.campaign_id in nidria:
                known.created_by_nidria.append(row)
            elif (row.status or "") in _CAMPAIGN_STATUS:
                known.importable.append(row)
            else:
                known.not_importable.append(row)
        return known

    async def _facts(
        self,
        agency_id: uuid.UUID,
        reads: Iterable[CampaignRead],
        imported: Mapping[str, EmailCampaign],
        *,
        lock: bool,
    ) -> Facts:
        """Five queries whatever the number of leads: the records holding
        their addresses, the live memberships of those records and of those
        addresses, and the open memberships of the imported campaigns
        (locked when an import writes)."""
        reads = list(reads)
        addresses = {lead.email for read in reads for lead in read.leads if lead.email}
        facts = Facts(
            profiles=await ClientProfilesRepository(self.db).profile_ids_for_emails(
                agency_id, addresses
            )
        )
        campaign_ids = [imported[r.campaign_id].id for r in reads if r.campaign_id in imported]
        for member in await self.repo.open_members(campaign_ids, lock=lock):
            if member.provider_lead_id:
                facts.open_by_lead.setdefault(member.campaign_id, {})[member.provider_lead_id] = (
                    member
                )
            facts.open_by_profile.setdefault(member.campaign_id, {})[member.client_profile_id] = (
                member
            )
        # The records an address names, and those already imported (whose
        # address may have changed since).
        records = set(facts.profiles.values()) | {
            profile_id for by_profile in facts.open_by_profile.values() for profile_id in by_profile
        }
        for member in await self.repo.live_members(agency_id, sorted(records)):
            facts.live_by_profile.setdefault(member.client_profile_id, []).append(member)
        for address, member in await self.repo.live_members_by_email(agency_id, addresses):
            facts.live_by_address.setdefault(address, []).append(member)
        return facts

    def _campaign_plan(self, plan: CampaignPlan) -> EmailImportCampaignPlan:
        read = plan.read
        return EmailImportCampaignPlan(
            provider_campaign_id=read.campaign_id,
            name=read.name,
            status=read.status,
            already_imported=plan.campaign is not None,
            error=read.error,
            steps=_steps_read(read.sequences) if read.error is None and read.sequences else None,
            leads=_leads_read(plan) if read.error is None else None,
            states=_states_read(plan) if read.error is None else [],
            calls=EmailImportCallsRead(
                reads=read.reads, apply_min=read.reads + (1 if read.error is None else 0)
            ),
        )

    # ── one campaign's import, in its own transaction ───────────────────

    async def _import(
        self, access: Access, reader: ImportReader, team: str, plan: CampaignPlan
    ) -> tuple[EmailImportCampaignResult, int]:
        """(the campaign's result, the activity pages it read). Its history
        is read first, outside any transaction; then everything is written
        at once, or nothing."""
        read = plan.read
        started = datetime.now(UTC)
        try:
            history = await asyncio.to_thread(reader.read_activities, read.ref)
        except ProviderError as exc:
            return _refused(read.campaign_id, read.name, provider_error_code(exc)), 1
        result = EmailImportCampaignResult(
            provider_campaign_id=read.campaign_id, name=read.name, ok=True
        )
        try:
            await self._write(access, team, plan, history, started, result)
        except ConflictError as exc:
            await self.db.rollback()
            failed = _refused(read.campaign_id, read.name, exc.code, exc.params)
            return failed, history.pages
        except Exception as exc:  # noqa: BLE001 (one campaign never stops the others)
            # Classes only: a database error's message carries the
            # statement's values (addresses, names), which are never logged.
            cause = getattr(exc, "orig", None)
            logger.error(
                "email import of Lemlist campaign %s failed: %s%s",
                read.campaign_id,
                type(exc).__name__,
                f" ({type(cause).__name__})" if cause is not None else "",
            )
            await self.db.rollback()
            return _refused(read.campaign_id, read.name, "email.import_failed"), history.pages
        return result, history.pages

    async def _write(
        self,
        access: Access,
        team: str,
        plan: CampaignPlan,
        history: ProviderActivityRead,
        started: datetime,
        result: EmailImportCampaignResult,
    ) -> None:
        read = plan.read
        agency_id = access.agency_id
        await self.repo.lock_import(agency_id)
        campaign, created = await self._campaign(access, team, read)
        result.campaign_id, result.created = campaign.id, created
        # Decided again, under the locks, from the database as it is now.
        facts = await self._facts(
            agency_id, [read], {} if created else {read.campaign_id: campaign}, lock=True
        )
        taken_addresses, taken_profiles = plan.taken_before
        fresh = plan_campaign(
            read, None if created else campaign, facts, set(taken_addresses), set(taken_profiles)
        )
        assert read.sequences is not None  # a campaign read without error
        await self._steps(campaign, read.sequences, result)
        members = await self._members(access, campaign, fresh, result)
        stats = await self.sync.ingest(
            PROVIDER_LEMLIST,
            history.events,
            commit=False,
            campaign=campaign,
            history=True,
            members={m.provider_lead_id: m for _v, m in members if m.provider_lead_id},
        )
        result.events_stored, result.events_duplicate = stats.stored, stats.duplicates
        result.activities_read, result.activity_pages = history.rows, history.pages
        result.activities_complete = history.complete
        firsts = await self.repo.first_event_times(campaign.id)
        for _verdict, member in members:
            # When Nidria knows the lead was launched: its first activity.
            first = firsts.get(member.id)
            if first is not None and (member.queued_at is None or first < member.queued_at):
                member.queued_at = first
        await self._opposition(agency_id, fresh, members, result)
        set_aside = sorted(v.lead.lead_id for v in fresh.verdicts if v.action == "skip")
        known = await self.repo.lead_ids_of_campaign(campaign.id)
        unknown = (
            len({lead.lead_id for lead in read.leads} - known - set(set_aside))
            if read.listing_complete
            else None
        )
        now = datetime.now(UTC)
        noted = import_metadata(campaign.provider_metadata)
        noted.update(
            imported_at=now.isoformat(),
            imported_by_agent_id=str(access.agent_id),
            unknown_leads=unknown,
            unknown_leads_at=now.isoformat(),
            set_aside=set_aside,
        )
        campaign.provider_metadata = {
            **(campaign.provider_metadata or {}),
            IMPORT_METADATA_KEY: noted,
        }
        campaign.last_activity_sync_at = started
        campaign.last_provider_sync_at = now
        result.unknown_leads = unknown
        result.not_imported = dict(sorted(_skipped(fresh).items()))
        result.conflicts = (
            result.not_imported.get("conflict", 0)
            + result.not_imported.get("conflict_in_plan", 0)
            + sum(1 for v in fresh.verdicts if v.conflict)
        )
        result.replies_pending = await self.repo.pending_reply_count(campaign.id)
        await self.db.commit()

    async def _campaign(
        self, access: Access, team: str, read: CampaignRead
    ) -> tuple[EmailCampaign, bool]:
        """The agency's campaign for this Lemlist campaign, locked: the one
        imported before (refreshed: name, Lemlist's word, its status unless
        archived in Nidria), else a new one. One Nidria made is refused."""
        found = await self.repo.campaigns_by_provider_ids(PROVIDER_LEMLIST, [read.campaign_id])
        mine = next((c for c in found if c.agency_id == access.agency_id), None)
        if any(c.agency_id != access.agency_id for c in found) or (
            mine is not None and mine.origin != ORIGIN_LEMLIST_IMPORT
        ):
            raise ConflictError(
                "Nidria created this Lemlist campaign: it is never imported.",
                code="email.import_created_by_nidria",
            )
        name = (read.name or read.campaign_id)[:255]
        status = _CAMPAIGN_STATUS.get(read.status or "", "PAUSED")
        main = read.sequences.main_sequence_id if read.sequences is not None else None
        created = mine is None
        if mine is None:
            fresh = EmailCampaign(
                agency_id=access.agency_id,
                name=name,
                provider=PROVIDER_LEMLIST,
                origin=ORIGIN_LEMLIST_IMPORT,
                provider_campaign_id=read.campaign_id,
                provider_sequence_id=main,
                status=status,
                provider_status=(read.status or "")[:30] or None,
                settings=_settings(read.timezone),
                # Its team (rule N4): the one the key opens, the agency's.
                provider_metadata={CAMPAIGN_TEAM_KEY: team},
                created_by_agent_id=access.agent_id,
            )
            self.db.add(fresh)
            await self.db.flush()
            campaign_id = fresh.id
        else:
            campaign_id = mine.id
        campaign = await self.repo.campaign(access.agency_id, campaign_id, lock=True)
        assert campaign is not None  # the agency's, found or created just above
        if not created:
            campaign.name = name
            campaign.provider_sequence_id = main or campaign.provider_sequence_id
            campaign.provider_status = (read.status or "")[:30] or None
            if campaign.status != "ARCHIVED":
                campaign.status = status
        return campaign, created

    async def _steps(
        self,
        campaign: EmailCampaign,
        sequences: ProviderSequences,
        result: EmailImportCampaignResult,
    ) -> None:
        """The main sequence's steps, keyed by Lemlist step id, in Lemlist's
        order: an email is EMAIL, any other channel OTHER_CHANNEL (its
        Lemlist type in `provider_content`, the raw step). No Nidria
        template: the content is Lemlist's. A step Lemlist no longer has
        keeps its history, after the others."""
        by_id = {s.provider_step_id: s for s in campaign.steps if s.provider_step_id}
        listed: list[EmailCampaignStep] = []
        for provider_step in sequences.steps:
            kind = STEP_TYPE_EMAIL if provider_step.type == "email" else STEP_TYPE_OTHER
            step = by_id.get(provider_step.step_id)
            if step is None:
                step = EmailCampaignStep(
                    campaign_id=campaign.id,
                    position=provider_step.position,
                    name="",
                    type=kind,
                    delay_value=provider_step.delay_days,
                    delay_unit="DAYS",
                    subject_template="",
                    body_template="",
                    provider_step_id=provider_step.step_id,
                    version=1,
                    content_hash=_EMPTY_HASH,
                    provider_synced_hash=_EMPTY_HASH,
                    provider_content=dict(provider_step.raw),
                )
                step.ai_variables = []
                campaign.steps.append(step)
                result.steps_created += 1
            else:
                if (step.position, step.type, step.delay_value, step.provider_content) != (
                    provider_step.position,
                    kind,
                    provider_step.delay_days,
                    provider_step.raw,
                ):
                    result.steps_updated += 1
                step.position = provider_step.position
                step.type = kind
                step.delay_value = provider_step.delay_days
                step.delay_unit = "DAYS"
                step.provider_content = dict(provider_step.raw)
            listed.append(step)
        others = sorted(
            (s for s in campaign.steps if all(s is not kept for kept in listed)),
            key=lambda s: s.position,
        )
        for offset, step in enumerate(others, start=len(listed) + 1):
            step.position = offset
        await self.db.flush()

    async def _members(
        self,
        access: Access,
        campaign: EmailCampaign,
        plan: CampaignPlan,
        result: EmailImportCampaignResult,
    ) -> list[tuple[Verdict, EmailCampaignMember]]:
        """The missing client records, created by the client records' own
        core (`seed_client_profiles`: no account, invitation nor email),
        then the memberships created and refreshed."""
        # Imported here: the client records' manager reaches the Email module.
        from src.client_profiles.client_profiles_manager import ProfileSeed, seed_client_profiles

        agency_id = access.agency_id
        creates = [v for v in plan.verdicts if v.action == "create"]
        seeds = [
            ProfileSeed(v.lead.first_name, v.lead.last_name, v.lead.email, SOURCE_LEMLIST)
            for v in creates
            if v.profile_id is None
        ]
        existing, built = await seed_client_profiles(
            ClientProfilesRepository(self.db), agency_id, seeds
        )
        if built:
            self.db.add_all(built)
            await self.db.flush()
        result.profiles_created = len(built)
        by_address = {**existing, **{p.email: p.id for p in built if p.email}}
        now = datetime.now(UTC)
        imported_at = now.isoformat()
        out: list[tuple[Verdict, EmailCampaignMember]] = []
        for verdict in creates:
            lead = verdict.lead
            assert lead.email is not None and verdict.rule.proposed is not None
            member = EmailCampaignMember(
                campaign_id=campaign.id,
                client_profile_id=verdict.profile_id or by_address[lead.email],
                agency_id=agency_id,
                provider_lead_id=lead.lead_id,
                provider_contact_id=lead.contact_id,
                provider_status=(lead.state or "")[:60] or None,
                provider_metadata=_member_metadata(lead, None, imported_at),
                added_by_agent_id=access.agent_id,
                # Launched at Lemlist; refined to its first activity below.
                queued_at=now,
            )
            _set_status(member, verdict.rule.proposed, now)
            self.db.add(member)
            out.append((verdict, member))
        for verdict in plan.verdicts:
            if verdict.action != "update" or verdict.member is None:
                continue
            member, lead = verdict.member, verdict.lead
            member.provider_metadata = _member_metadata(lead, member.provider_metadata, imported_at)
            if verdict.status is not None:
                _set_status(member, verdict.status, now)
            member.provider_status = (lead.state or "")[:60] or None
            member.provider_contact_id = member.provider_contact_id or lead.contact_id
            out.append((verdict, member))
        result.members_created = len(creates)
        result.members_updated = len(out) - len(creates)
        await self.db.flush()
        return out

    async def _opposition(
        self,
        agency_id: uuid.UUID,
        plan: CampaignPlan,
        members: Sequence[tuple[Verdict, EmailCampaignMember]],
        result: EmailImportCampaignResult,
    ) -> None:
        """The addresses of the leads Lemlist says unsubscribed or bounced go
        on the agency's opposition list, imported or not (the person's
        effective address too, for a membership whose record changed it)."""
        listed: set[str] = set()
        for verdict, member in members:
            reason = REASON_OF_STATUS.get(member.status)
            if reason is None:
                continue
            addresses = [verdict.lead.email]
            if verdict.action == "update":
                addresses.append(await self.repo.effective_email(member.client_profile_id))
            for address in addresses:
                listed |= await self._suppress(agency_id, address, reason, member.id)
        for verdict in plan.verdicts:
            if verdict.action == "skip" and verdict.rule.opposition:
                listed |= await self._suppress(
                    agency_id, verdict.lead.email, verdict.rule.opposition, None
                )
        result.opposition_listed = len(listed)

    async def _suppress(
        self, agency_id: uuid.UUID, address: str | None, reason: str, member_id: uuid.UUID | None
    ) -> set[str]:
        digest = email_hash(address)
        if digest is None:
            return set()
        await self.repo.suppress(agency_id, digest, reason, member_id)
        return {digest}


def _refused(
    campaign_id: str,
    name: str | None,
    code: str,
    params: Mapping[str, Any] | None = None,
) -> EmailImportCampaignResult:
    """A campaign the import did not write: its code, nothing counted."""
    return EmailImportCampaignResult(
        provider_campaign_id=campaign_id,
        name=name,
        ok=False,
        code=code,
        params={
            key: value for key, value in (params or {}).items() if isinstance(value, int | str)
        },
    )


__all__ = [
    "CLIENT_PROFILE_CREATE",
    "RULES",
    "SOURCE_LEMLIST",
    "EmailImportManager",
    "propose",
]
