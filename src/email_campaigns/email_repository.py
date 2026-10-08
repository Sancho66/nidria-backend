"""Pure database access of the Email module (no business rule here).

Phase 1: the per-agency switch (`agency_email_module`) and the campaign facts
the platform routes decide on. Phase 2: what the readiness rules read beyond
a recipient's own row (`readiness.load_client_facts`). Phase 3: the agent
routes' queries, ported from Prism's `email_repository.py` (reference commit
629a2b0), then (3B) the launch queue, the add wizard's lists, the
performance and outreach figures (all computed in SQL) and the deletion
hooks' reads. Phase 4: the provider's events (stored once,
`uq_email_provider_event`), the memberships and messages they reach, and
the Email Sync job's reads. The webhook and the job have no agent: they find
a campaign by its provider id, and the agency is the campaign's (rule N1);
an event about a whole contact, without a campaign Nidria runs, is the
agency's bound to its Lemlist team. The opposition list (`email_suppression`,
decision of 08/10/2026): written once per address, read by fingerprint.

The wizard reuses the Clients and Companies pages' own predicates
(`ClientProfilesRepository.filter_predicates`,
`CompanyProfilesRepository.filter_predicates`, decision A15), never a copy;
only their sort order is restated here (`list_page` keeps it inline).

Every read of the agent routes is bounded by the agency the manager already
resolved from the token: a campaign, a step, an AI variable or a membership
of another agency is simply not found here (rule N1). A recipient is a
client record; its name and address are the EFFECTIVE ones (rule N2): every
query that reads or searches them OUTER JOINs `ExpatUser` and coalesces
(`client_profiles.identity.effective_email_sql`).
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    Select,
    and_,
    case,
    cast,
    delete,
    distinct,
    exists,
    func,
    not_,
    or_,
    select,
    text,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from shared.models.agency import Agency
from shared.models.agency_email_module import AgencyEmailModule
from shared.models.client_profile import ClientProfile
from shared.models.company_profile import CompanyProfile, CompanyProfileRole
from shared.models.email_campaign import (
    BLOCKING_MEMBER_STATUSES,
    LIVE_MEMBER_STATUSES,
    EmailCampaign,
    EmailCampaignAiVariable,
    EmailCampaignMember,
    EmailCampaignStep,
    EmailMemberRender,
    EmailMessage,
    EmailProviderEvent,
    EmailSuppression,
)
from shared.models.expat_user import ExpatUser
from src.client_profiles.client_profiles_repository import ClientProfilesRepository
from src.client_profiles.client_profiles_schema import ProfileListFilter
from src.client_profiles.identity import effective_email_sql
from src.company_profiles.company_profiles_repository import CompanyProfilesRepository
from src.company_profiles.company_profiles_schema import CompanyListFilter
from src.core.client_lang import effective_lang_sql
from src.email_campaigns.providers.dry_run import DRY_RUN_PREFIX
from src.email_campaigns.suppression import (
    REASON_UNSUBSCRIBED,
    STATUS_OF_REASON,
    email_hash_sql,
)

_FUNNEL_KEYS = (
    "launched",
    "launched_dry_run",
    "reached",
    "opened",
    "clicked",
    "replied",
    "bounced",
    "unsubscribed",
)
_METRIC_KEYS = ("sent", "opened", "clicked", "replied", "bounced")
_STEP_KEYS = ("entered", "sent", "opened", "clicked", "replied", "bounced", "unsubscribed")
_DAY_KEYS = ("launched", "sent", "opened", "clicked", "replied", "bounced", "unsubscribed")
#: The provider events a day of the timeline counts, by its key.
_DAY_EVENTS = {
    "SENT": "sent",
    "OPENED": "opened",
    "CLICKED": "clicked",
    "REPLIED": "replied",
    "BOUNCED": "bounced",
    "UNSUBSCRIBED": "unsubscribed",
}
#: Ids per `IN (...)` of the deletion hooks: a bulk deletion by filter can
#: name more client records than one statement may carry parameters.
_IDS_PER_QUERY = 5000


def _metric_columns() -> list[ColumnElement[int]]:
    """Sent, opened, clicked (once each however many times), replies, bounces."""
    outbound = EmailMessage.direction == "OUTBOUND"
    return [
        func.count(case((and_(outbound, EmailMessage.sent_at.is_not(None)), 1))),
        func.count(case((and_(outbound, EmailMessage.open_count > 0), 1))),
        func.count(case((and_(outbound, EmailMessage.click_count > 0), 1))),
        func.count(case((EmailMessage.direction == "INBOUND", 1))),
        func.count(case((and_(outbound, EmailMessage.bounced_at.is_not(None)), 1))),
    ]


def _metrics(values: Sequence[Any]) -> dict[str, int]:
    return {key: int(value or 0) for key, value in zip(_METRIC_KEYS, values, strict=True)}


def _dry_run_id(column: Any) -> ColumnElement[bool]:
    """A dry-run identifier. `autoescape`: the `_` of the prefix is a LIKE
    wildcard otherwise."""
    result: ColumnElement[bool] = column.startswith(DRY_RUN_PREFIX, autoescape=True)
    return result


@dataclass(frozen=True)
class MemberRow:
    """One membership as the agent routes read it: the membership, its
    client record, the client-space account the record is linked to (None
    when it is not: rule N2) and its campaign (steps loaded)."""

    member: EmailCampaignMember
    profile: ClientProfile
    account: ExpatUser | None
    campaign: EmailCampaign


def real_provider_campaign() -> ColumnElement[bool]:
    """A campaign Lemlist knows: a provider id that is not a dry-run one.
    `autoescape`: the `_` of the prefix is a LIKE wildcard otherwise."""
    return and_(
        EmailCampaign.provider_campaign_id.is_not(None),
        EmailCampaign.provider_campaign_id != "",
        ~EmailCampaign.provider_campaign_id.startswith(DRY_RUN_PREFIX, autoescape=True),
    )


class EmailRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- the per-agency switch ----------------------------------------------------

    async def get_module(
        self, agency_id: uuid.UUID, *, for_update: bool = False
    ) -> AgencyEmailModule | None:
        stmt = select(AgencyEmailModule).where(AgencyEmailModule.agency_id == agency_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def lock_or_create_module(self, agency_id: uuid.UUID) -> AgencyEmailModule:
        """The agency's row, created (switched off) when missing, then locked
        for the rest of the transaction. ON CONFLICT: two concurrent first
        writes create one row, never an IntegrityError."""
        await self.db.execute(
            insert(AgencyEmailModule)
            .values(agency_id=agency_id)
            .on_conflict_do_nothing(index_elements=[AgencyEmailModule.agency_id])
        )
        row = await self.get_module(agency_id, for_update=True)
        assert row is not None  # inserted or already there, in this transaction
        return row

    async def enabled_agency_ids(self, agency_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
        """Among these agencies, those whose own switch is on (one query)."""
        ids = list(agency_ids)
        if not ids:
            return set()
        stmt = select(AgencyEmailModule.agency_id).where(
            AgencyEmailModule.agency_id.in_(ids), AgencyEmailModule.enabled.is_(True)
        )
        return set((await self.db.execute(stmt)).scalars())

    async def team_owner(self, provider: str, team_id: str) -> tuple[uuid.UUID, str] | None:
        """The agency a Lemlist team is bound to, with its name, or None."""
        stmt = (
            select(AgencyEmailModule.agency_id, Agency.name)
            .join(Agency, Agency.id == AgencyEmailModule.agency_id)
            .where(
                AgencyEmailModule.provider == provider,
                AgencyEmailModule.provider_team_id == team_id,
            )
        )
        row = (await self.db.execute(stmt)).one_or_none()
        return (row[0], row[1]) if row is not None else None

    # --- campaign facts the platform decides on -----------------------------------

    async def count_campaigns_by_status(self, agency_id: uuid.UUID) -> dict[str, int]:
        stmt = (
            select(EmailCampaign.status, func.count())
            .where(EmailCampaign.agency_id == agency_id)
            .group_by(EmailCampaign.status)
        )
        return {status: count for status, count in (await self.db.execute(stmt)).all()}

    async def campaign_ids(self, agency_id: uuid.UUID, status: str) -> list[uuid.UUID]:
        """The agency's campaigns in this status, oldest first."""
        rows = await self.db.execute(
            select(EmailCampaign.id)
            .where(EmailCampaign.agency_id == agency_id, EmailCampaign.status == status)
            .order_by(EmailCampaign.created_at, EmailCampaign.id)
        )
        return list(rows.scalars().all())

    async def count_live_provider_campaigns(self, agency_id: uuid.UUID) -> int:
        """ACTIVE campaigns that exist at Lemlist: sending may be under way."""
        stmt = select(func.count()).where(
            EmailCampaign.agency_id == agency_id,
            EmailCampaign.status == "ACTIVE",
            real_provider_campaign(),
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def count_provider_campaigns(self, agency_id: uuid.UUID) -> int:
        """Campaigns of any status that exist at Lemlist: the agency's history
        in its team."""
        stmt = select(func.count()).where(
            EmailCampaign.agency_id == agency_id, real_provider_campaign()
        )
        return int((await self.db.execute(stmt)).scalar_one())

    # --- what the readiness rules read (phase 2) ----------------------------------

    async def blocking_statuses(
        self, agency_id: uuid.UUID, profile_ids: Sequence[uuid.UUID]
    ) -> list[tuple[uuid.UUID, str]]:
        """(client profile id, status) of every UNSUBSCRIBED or BOUNCED
        membership of these profiles in the agency. Removed memberships
        INCLUDED: `removed_at` is deliberately not read (decision A1, the
        block outlives a removal)."""
        if not profile_ids:
            return []
        stmt = select(EmailCampaignMember.client_profile_id, EmailCampaignMember.status).where(
            EmailCampaignMember.agency_id == agency_id,
            EmailCampaignMember.client_profile_id.in_(list(profile_ids)),
            EmailCampaignMember.status.in_(BLOCKING_MEMBER_STATUSES),
        )
        return [(row[0], row[1]) for row in (await self.db.execute(stmt)).all()]

    async def live_members(
        self, agency_id: uuid.UUID, profile_ids: Sequence[uuid.UUID]
    ) -> list[EmailCampaignMember]:
        """The live memberships (READY, QUEUED, ACTIVE, PAUSED) of these
        profiles in the agency: one per profile at most
        (`uq_email_member_live`)."""
        if not profile_ids:
            return []
        stmt = select(EmailCampaignMember).where(
            EmailCampaignMember.agency_id == agency_id,
            EmailCampaignMember.client_profile_id.in_(list(profile_ids)),
            EmailCampaignMember.status.in_(LIVE_MEMBER_STATUSES),
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def suppressed(
        self, agency_id: uuid.UUID, digests: Iterable[str]
    ) -> dict[str, tuple[str, uuid.UUID | None]]:
        """The agency's opposition list for these address fingerprints:
        fingerprint -> (EMAIL_UNSUBSCRIBED or EMAIL_BOUNCED, the client
        record of the membership that listed it, None once it is gone)."""
        wanted = sorted({digest for digest in digests if digest})
        if not wanted:
            return {}
        stmt = (
            select(
                EmailSuppression.email_hash,
                EmailSuppression.reason,
                EmailCampaignMember.client_profile_id,
            )
            .outerjoin(
                EmailCampaignMember, EmailCampaignMember.id == EmailSuppression.source_member_id
            )
            .where(EmailSuppression.agency_id == agency_id, EmailSuppression.email_hash.in_(wanted))
        )
        return {
            digest: (STATUS_OF_REASON[reason], profile_id)
            for digest, reason, profile_id in (await self.db.execute(stmt)).all()
        }

    async def suppress(
        self,
        agency_id: uuid.UUID,
        digest: str,
        reason: str,
        source_member_id: uuid.UUID | None,
    ) -> None:
        """Put an address fingerprint on the agency's opposition list, or
        keep it there: an unsubscribe outranks a bounce, never the reverse,
        and the source follows the reason kept (a listed source gone since
        is filled again by the next one of the same reason). One statement:
        two writers at once never make two rows (`uq_email_suppression_address`)."""
        row = insert(EmailSuppression).values(
            id=uuid.uuid4(),
            agency_id=agency_id,
            email_hash=digest,
            reason=reason,
            source_member_id=source_member_id,
        )
        current = EmailSuppression.__table__.c
        upgraded = and_(
            current.reason != REASON_UNSUBSCRIBED, row.excluded.reason == REASON_UNSUBSCRIBED
        )
        await self.db.execute(
            row.on_conflict_do_update(
                constraint="uq_email_suppression_address",
                set_={
                    "reason": case(
                        (
                            or_(
                                current.reason == REASON_UNSUBSCRIBED,
                                row.excluded.reason == REASON_UNSUBSCRIBED,
                            ),
                            REASON_UNSUBSCRIBED,
                        ),
                        else_=current.reason,
                    ),
                    "source_member_id": case(
                        (upgraded, row.excluded.source_member_id),
                        else_=func.coalesce(
                            current.source_member_id, row.excluded.source_member_id
                        ),
                    ),
                },
            )
        )

    async def effective_email(self, profile_id: uuid.UUID) -> str | None:
        """The address Nidria writes to for this client record (rule N2)."""
        value = await self.db.scalar(
            select(effective_email_sql())
            .select_from(ClientProfile)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(ClientProfile.id == profile_id)
        )
        return str(value) if value else None

    async def live_members_by_email(
        self, agency_id: uuid.UUID, emails: Iterable[str]
    ) -> list[tuple[str, EmailCampaignMember]]:
        """(lower-cased effective email, membership) of every live
        membership of the agency whose profile's effective address is one of
        `emails`, whichever profile holds it (risk R1)."""
        wanted = sorted({email.lower() for email in emails if email})
        if not wanted:
            return []
        address = func.lower(effective_email_sql())
        stmt = (
            select(address, EmailCampaignMember)
            .select_from(EmailCampaignMember)
            .join(ClientProfile, ClientProfile.id == EmailCampaignMember.client_profile_id)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(
                EmailCampaignMember.agency_id == agency_id,
                EmailCampaignMember.status.in_(LIVE_MEMBER_STATUSES),
                address.in_(wanted),
            )
        )
        return [(row[0], row[1]) for row in (await self.db.execute(stmt)).all()]

    # --- campaigns (agent routes, phase 3) -----------------------------------------

    @staticmethod
    def _with_steps() -> Select[tuple[EmailCampaign]]:
        return select(EmailCampaign).options(
            selectinload(EmailCampaign.steps).selectinload(EmailCampaignStep.ai_variables)
        )

    async def campaigns(
        self, agency_id: uuid.UUID, statuses: Sequence[str] | None = None
    ) -> list[EmailCampaign]:
        """The agency's campaigns, last changed first, steps loaded."""
        stmt = (
            self._with_steps()
            .where(EmailCampaign.agency_id == agency_id)
            .order_by(EmailCampaign.updated_at.desc(), EmailCampaign.id.desc())
            .execution_options(populate_existing=True)
        )
        if statuses:
            stmt = stmt.where(EmailCampaign.status.in_(list(statuses)))
        return list((await self.db.execute(stmt)).scalars().all())

    async def campaign(
        self, agency_id: uuid.UUID, campaign_id: uuid.UUID, *, lock: bool = False
    ) -> EmailCampaign | None:
        """One campaign of the agency, steps and AI variables loaded; None for
        another agency's. `populate_existing`: a session keeps objects across
        commits, so a re-read refreshes the steps it already holds."""
        stmt = (
            self._with_steps()
            .where(EmailCampaign.id == campaign_id, EmailCampaign.agency_id == agency_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update(of=EmailCampaign)
        return (await self.db.execute(stmt)).scalars().first()

    async def step(self, agency_id: uuid.UUID, step_id: uuid.UUID) -> EmailCampaignStep | None:
        """One step of one of the agency's campaigns; None for another agency's."""
        stmt = (
            select(EmailCampaignStep)
            .join(EmailCampaign, EmailCampaign.id == EmailCampaignStep.campaign_id)
            .options(selectinload(EmailCampaignStep.ai_variables))
            .where(EmailCampaignStep.id == step_id, EmailCampaign.agency_id == agency_id)
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalars().first()

    async def ai_variable(
        self, agency_id: uuid.UUID, variable_id: uuid.UUID
    ) -> EmailCampaignAiVariable | None:
        """One AI variable of one of the agency's steps; None for another agency's."""
        stmt = (
            select(EmailCampaignAiVariable)
            .join(
                EmailCampaignStep, EmailCampaignStep.id == EmailCampaignAiVariable.campaign_step_id
            )
            .join(EmailCampaign, EmailCampaign.id == EmailCampaignStep.campaign_id)
            .where(EmailCampaignAiVariable.id == variable_id, EmailCampaign.agency_id == agency_id)
        )
        return (await self.db.execute(stmt)).scalars().first()

    async def status_counts(
        self, campaign_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, int]]:
        """Campaign -> membership status -> count."""
        if not campaign_ids:
            return {}
        rows = await self.db.execute(
            select(EmailCampaignMember.campaign_id, EmailCampaignMember.status, func.count())
            .where(EmailCampaignMember.campaign_id.in_(list(campaign_ids)))
            .group_by(EmailCampaignMember.campaign_id, EmailCampaignMember.status)
        )
        counts: dict[uuid.UUID, dict[str, int]] = {}
        for campaign_id, status, count in rows.all():
            counts.setdefault(campaign_id, {})[status] = int(count)
        return counts

    async def members_by_step(self, campaign_id: uuid.UUID) -> dict[int, int]:
        """Running recipients per step they were last sent."""
        rows = await self.db.execute(
            select(EmailCampaignMember.current_step, func.count())
            .where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.status.in_(("EMAIL_ACTIVE", "EMAIL_PAUSED")),
                EmailCampaignMember.current_step.is_not(None),
            )
            .group_by(EmailCampaignMember.current_step)
        )
        return {int(step): int(count) for step, count in rows.all()}

    async def step_counts(self, campaign_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Steps per campaign, without loading the templates."""
        counts = dict.fromkeys(campaign_ids, 0)
        if not counts:
            return counts
        rows = await self.db.execute(
            select(EmailCampaignStep.campaign_id, func.count())
            .where(EmailCampaignStep.campaign_id.in_(list(counts)))
            .group_by(EmailCampaignStep.campaign_id)
        )
        counts.update({campaign_id: int(count) for campaign_id, count in rows.all()})
        return counts

    async def message_metrics(
        self, campaign_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, int]]:
        """Per campaign, from Nidria's synced messages: emails sent, emails
        opened / clicked (once each, however many times), replies, bounces."""
        if not campaign_ids:
            return {}
        rows = await self.db.execute(
            select(EmailMessage.campaign_id, *_metric_columns())
            .where(EmailMessage.campaign_id.in_(list(campaign_ids)))
            .group_by(EmailMessage.campaign_id)
        )
        return {campaign_id: _metrics(values) for campaign_id, *values in rows.all()}

    async def message_metrics_by_step(self, campaign_id: uuid.UUID) -> dict[int, dict[str, int]]:
        """The same counts per step position: a reply counts for the step it
        answers. Messages without a step are left out."""
        rows = await self.db.execute(
            select(EmailMessage.step_position, *_metric_columns())
            .where(
                EmailMessage.campaign_id == campaign_id,
                EmailMessage.step_position.is_not(None),
            )
            .group_by(EmailMessage.step_position)
        )
        return {int(position): _metrics(values) for position, *values in rows.all()}

    @staticmethod
    def _cohort(
        campaign_ids: Sequence[uuid.UUID], since: datetime | None, until: datetime | None
    ) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [
            EmailCampaignMember.campaign_id.in_(list(campaign_ids))
        ]
        if since is not None:
            conditions.append(EmailCampaignMember.queued_at >= since)
        if until is not None:
            conditions.append(EmailCampaignMember.queued_at < until)
        return conditions

    async def lead_funnel(
        self,
        campaign_ids: Sequence[uuid.UUID],
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[uuid.UUID, dict[str, int]]:
        """Per campaign, leads (distinct memberships) launched for real (and in
        a dry run, apart), reached by an email, opened / clicked one, replied,
        bounced (a bounced email, or ended as bounced) and unsubscribed (the
        event, or ended as unsubscribed). With `since` / `until`: the cohort
        launched in that period (the Performance page, phase 3B)."""
        out: dict[uuid.UUID, dict[str, int]] = {
            cid: dict.fromkeys(_FUNNEL_KEYS, 0) for cid in campaign_ids
        }
        if not campaign_ids:
            return out
        lead = EmailCampaignMember
        cohort = self._cohort(campaign_ids, since, until)
        dry = _dry_run_id(lead.provider_lead_id)
        launched = await self.db.execute(
            select(
                lead.campaign_id,
                func.count(distinct(case((not_(dry), lead.id)))),
                func.count(distinct(case((dry, lead.id)))),
            )
            .where(*cohort, lead.queued_at.is_not(None))
            .group_by(lead.campaign_id)
        )
        for campaign_id, real, simulated in launched.all():
            out[campaign_id]["launched"] = int(real)
            out[campaign_id]["launched_dry_run"] = int(simulated)
        message = EmailMessage
        outbound = message.direction == "OUTBOUND"
        engaged = await self.db.execute(
            select(
                lead.campaign_id,
                func.count(distinct(case((and_(outbound, message.sent_at.is_not(None)), lead.id)))),
                func.count(distinct(case((and_(outbound, message.open_count > 0), lead.id)))),
                func.count(distinct(case((and_(outbound, message.click_count > 0), lead.id)))),
                func.count(distinct(case((message.direction == "INBOUND", lead.id)))),
            )
            .select_from(lead)
            .join(message, message.campaign_member_id == lead.id)
            .where(*cohort)
            .group_by(lead.campaign_id)
        )
        for campaign_id, reached, opened, clicked, replied in engaged.all():
            out[campaign_id].update(
                reached=int(reached), opened=int(opened), clicked=int(clicked), replied=int(replied)
            )
        bounced = union(
            select(lead.campaign_id, lead.id).where(*cohort, lead.status == "EMAIL_BOUNCED"),
            select(lead.campaign_id, lead.id)
            .join(message, message.campaign_member_id == lead.id)
            .where(*cohort, outbound, message.bounced_at.is_not(None)),
        ).subquery()
        for campaign_id, count in (
            await self.db.execute(
                select(bounced.c.campaign_id, func.count(distinct(bounced.c.id))).group_by(
                    bounced.c.campaign_id
                )
            )
        ).all():
            out[campaign_id]["bounced"] = int(count)
        event = EmailProviderEvent
        unsubscribed = union(
            select(lead.campaign_id, lead.id).where(*cohort, lead.status == "EMAIL_UNSUBSCRIBED"),
            select(lead.campaign_id, lead.id)
            .join(event, event.campaign_member_id == lead.id)
            .where(*cohort, event.type == "UNSUBSCRIBED"),
        ).subquery()
        for campaign_id, count in (
            await self.db.execute(
                select(
                    unsubscribed.c.campaign_id, func.count(distinct(unsubscribed.c.id))
                ).group_by(unsubscribed.c.campaign_id)
            )
        ).all():
            out[campaign_id]["unsubscribed"] = int(count)
        return out

    async def membership_progress(
        self, campaign_id: uuid.UUID
    ) -> list[tuple[str, int | None, bool, datetime | None]]:
        """Every membership of the campaign: its status, the last step sent to
        it, whether the provider has (or had) its lead, when it was handed over."""
        rows = await self.db.execute(
            select(
                EmailCampaignMember.status,
                EmailCampaignMember.current_step,
                EmailCampaignMember.provider_lead_id.is_not(None),
                EmailCampaignMember.queued_at,
            ).where(EmailCampaignMember.campaign_id == campaign_id)
        )
        return [(status, step, bool(held), queued) for status, step, held, queued in rows.all()]

    async def complete_waiting(self, campaign_id: uuid.UUID, last_position: int) -> int:
        """Running recipients sent the new last step: their sequence is over."""
        result = await self.db.execute(
            update(EmailCampaignMember)
            .where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.status.in_(("EMAIL_ACTIVE", "EMAIL_PAUSED")),
                EmailCampaignMember.current_step >= last_position,
            )
            .values(
                status="EMAIL_COMPLETED",
                status_reason=None,
                paused_at=None,
                paused_by_agent_id=None,
            )
            .execution_options(synchronize_session="fetch")
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_campaign(self, campaign_id: uuid.UUID) -> None:
        """The campaign and everything of it: steps, AI variables, memberships,
        renders and messages follow by cascade; provider events would only be
        detached (SET NULL), so they go explicitly. The opposition list stays:
        its rows only lose their source membership (SET NULL)."""
        await self.db.execute(
            delete(EmailProviderEvent).where(EmailProviderEvent.campaign_id == campaign_id)
        )
        await self.db.execute(delete(EmailCampaign).where(EmailCampaign.id == campaign_id))

    # --- memberships (agent routes, phase 3) ---------------------------------------

    @staticmethod
    def _in_agency(stmt: Select[Any], agency_id: uuid.UUID) -> Select[Any]:
        """`stmt` over memberships joined to their client record, account and
        campaign, all three in the agency."""
        return (
            stmt.select_from(EmailCampaignMember)
            .join(ClientProfile, ClientProfile.id == EmailCampaignMember.client_profile_id)
            .join(EmailCampaign, EmailCampaign.id == EmailCampaignMember.campaign_id)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(
                EmailCampaignMember.agency_id == agency_id,
                EmailCampaign.agency_id == agency_id,
                ClientProfile.agency_id == agency_id,
            )
        )

    def _member_rows(self, agency_id: uuid.UUID) -> Select[Any]:
        """Memberships with their client record, account and campaign."""
        return self._in_agency(
            select(EmailCampaignMember, ClientProfile, ExpatUser, EmailCampaign), agency_id
        ).execution_options(populate_existing=True)

    @staticmethod
    def _rows(result: Any) -> list[MemberRow]:
        return [MemberRow(m, p, a, c) for m, p, a, c in result.all()]

    async def member_rows(
        self, agency_id: uuid.UUID, member_ids: Sequence[uuid.UUID]
    ) -> list[MemberRow]:
        """These memberships, those of the agency only."""
        if not member_ids:
            return []
        result = await self.db.execute(
            self._member_rows(agency_id)
            .where(EmailCampaignMember.id.in_(list(member_ids)))
            .order_by(EmailCampaignMember.id)
        )
        return self._rows(result)

    async def member_row(self, agency_id: uuid.UUID, member_id: uuid.UUID) -> MemberRow | None:
        rows = await self.member_rows(agency_id, [member_id])
        return rows[0] if rows else None

    def _member_search(self, agency_id: uuid.UUID, search: str) -> ColumnElement[bool]:
        """A client's effective name or address, or the name of a company
        linked to it, contains `search`."""
        pattern = f"%{search}%"
        first = func.coalesce(ExpatUser.first_name, ClientProfile.first_name)
        last = func.coalesce(ExpatUser.last_name, ClientProfile.last_name)
        company = exists(
            select(CompanyProfileRole.id)
            .join(CompanyProfile, CompanyProfile.id == CompanyProfileRole.company_profile_id)
            .where(
                CompanyProfileRole.client_profile_id == ClientProfile.id,
                CompanyProfile.agency_id == agency_id,
                CompanyProfile.name.ilike(pattern),
            )
            .correlate(ClientProfile)
        )
        return or_(
            func.concat_ws(" ", first, last).ilike(pattern),
            effective_email_sql().ilike(pattern),
            company,
        )

    def _member_filters(
        self, agency_id: uuid.UUID, filters: dict[str, Any]
    ) -> list[ColumnElement[bool]]:
        """The Leads list's filters but the status one (see `members_page`)."""
        conditions: list[ColumnElement[bool]] = []
        if filters.get("campaign_id"):
            conditions.append(EmailCampaignMember.campaign_id == filters["campaign_id"])
        client_statuses = set(filters.get("client_status") or ())
        if len(client_statuses) == 1:
            # The client records' own projection of the derived status, reused
            # as is: the Leads list and the Clients page cannot disagree.
            [status] = client_statuses
            conditions.extend(
                ClientProfilesRepository(self.db).filter_predicates(agency_id, status=status)
            )
        if filters.get("step") is not None:
            conditions.append(EmailCampaignMember.current_step == filters["step"])
        address = func.coalesce(func.trim(effective_email_sql()), "")
        if filters.get("has_email") is True:
            conditions.append(address != "")
        elif filters.get("has_email") is False:
            conditions.append(address == "")
        if filters.get("added_after"):
            conditions.append(EmailCampaignMember.added_at >= filters["added_after"])
        if filters.get("added_before"):
            conditions.append(EmailCampaignMember.added_at <= filters["added_before"])
        if not filters.get("include_removed"):
            # Ended by a removal or a move: `removed_at`, whatever the status
            # (a removed unsubscribe keeps its status, decision A1).
            conditions.append(EmailCampaignMember.removed_at.is_(None))
        # Launched = handed to the provider (`queued_at`); to launch = not yet.
        if filters.get("launched") is True:
            conditions.append(EmailCampaignMember.queued_at.is_not(None))
        elif filters.get("launched") is False:
            conditions.append(EmailCampaignMember.queued_at.is_(None))
        search = (filters.get("search") or "").strip()
        if search:
            conditions.append(self._member_search(agency_id, search))
        return conditions

    async def members_page(
        self, agency_id: uuid.UUID, filters: dict[str, Any], page: int, page_size: int
    ) -> tuple[list[MemberRow], int, dict[str, int]]:
        """One page of the agency's memberships, last active first, the total
        and the counts per status (under every filter but the status one, for
        the filter chips)."""
        conditions = self._member_filters(agency_id, filters)
        counted = await self.db.execute(
            self._in_agency(select(EmailCampaignMember.status, func.count()), agency_id)
            .where(*conditions)
            .group_by(EmailCampaignMember.status)
        )
        status_counts = {status: int(count) for status, count in counted.all()}
        if filters.get("status"):
            conditions.append(EmailCampaignMember.status.in_(list(filters["status"])))
        total = await self.db.scalar(
            self._in_agency(select(func.count(EmailCampaignMember.id)), agency_id).where(
                *conditions
            )
        )
        result = await self.db.execute(
            self._member_rows(agency_id)
            .where(*conditions)
            .order_by(
                func.coalesce(
                    EmailCampaignMember.last_event_at, EmailCampaignMember.added_at
                ).desc(),
                EmailCampaignMember.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return self._rows(result), int(total or 0), status_counts

    async def open_in_campaign(
        self, campaign_id: uuid.UUID, profile_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Among these client records, those with an open (not ended)
        membership of the campaign."""
        if not profile_ids:
            return set()
        rows = await self.db.execute(
            select(EmailCampaignMember.client_profile_id).where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.client_profile_id.in_(list(profile_ids)),
                EmailCampaignMember.removed_at.is_(None),
            )
        )
        return set(rows.scalars().all())

    async def renders(self, member_id: uuid.UUID) -> dict[int, EmailMemberRender]:
        """Step position -> the membership's render."""
        return (await self.renders_for([member_id])).get(member_id, {})

    async def renders_for(
        self, member_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, dict[int, EmailMemberRender]]:
        """Membership -> step position -> render, in one query."""
        if not member_ids:
            return {}
        rows = await self.db.execute(
            select(EmailMemberRender)
            .where(EmailMemberRender.campaign_member_id.in_(list(member_ids)))
            .execution_options(populate_existing=True)
        )
        found: dict[uuid.UUID, dict[int, EmailMemberRender]] = {}
        for render in rows.scalars().all():
            found.setdefault(render.campaign_member_id, {})[render.step_position] = render
        return found

    async def events_of(self, member_id: uuid.UUID, limit: int = 200) -> list[EmailProviderEvent]:
        """The membership's provider events, newest first."""
        rows = await self.db.execute(
            select(EmailProviderEvent)
            .where(EmailProviderEvent.campaign_member_id == member_id)
            .order_by(EmailProviderEvent.occurred_at.desc(), EmailProviderEvent.id.desc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def messages_of(self, member_id: uuid.UUID) -> list[EmailMessage]:
        """The membership's conversation, oldest first."""
        rows = await self.db.execute(
            select(EmailMessage)
            .where(EmailMessage.campaign_member_id == member_id)
            .order_by(
                func.coalesce(
                    EmailMessage.sent_at, EmailMessage.received_at, EmailMessage.created_at
                ),
                EmailMessage.id,
            )
        )
        return list(rows.scalars().all())

    # --- what a template reads (agent routes, phase 3) -----------------------------

    async def fill_population(
        self, agency_id: uuid.UUID, campaign_id: uuid.UUID | None, cap: int
    ) -> tuple[int, list[tuple[ClientProfile, ExpatUser | None]]]:
        """The clients whose values a template will read, with the account
        they are linked to: a campaign's live recipients, or (no campaign)
        the agency's clients with an effective email. (total, rows): every
        row up to `cap`, else a random sample of `cap`."""
        condition: ColumnElement[bool]
        if campaign_id is not None:
            members = select(EmailCampaignMember.client_profile_id).where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.agency_id == agency_id,
                EmailCampaignMember.status.in_(LIVE_MEMBER_STATUSES),
            )
            condition = ClientProfile.id.in_(members)
        else:
            condition = func.coalesce(func.trim(effective_email_sql()), "") != ""
        base = (
            select(ClientProfile, ExpatUser)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(ClientProfile.agency_id == agency_id, condition)
        )
        total = int(await self.db.scalar(select(func.count()).select_from(base.subquery())) or 0)
        if total > cap:
            base = base.order_by(func.random()).limit(cap)
        # The values a count reads are the database's, never a session's copy.
        rows = await self.db.execute(base.execution_options(populate_existing=True))
        return total, [(profile, account) for profile, account in rows.all()]

    async def companies_of(
        self, agency_id: uuid.UUID, profile_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[CompanyProfile]]:
        """Client record -> the agency's companies linked to it by
        `company_profile_role` (one row per role: the variables count each
        company once, `variables.sole_company`)."""
        if not profile_ids:
            return {}
        rows = await self.db.execute(
            select(CompanyProfileRole.client_profile_id, CompanyProfile)
            .join(CompanyProfile, CompanyProfile.id == CompanyProfileRole.company_profile_id)
            .where(
                CompanyProfileRole.client_profile_id.in_(list(profile_ids)),
                CompanyProfile.agency_id == agency_id,
            )
            .order_by(CompanyProfile.name, CompanyProfile.id)
        )
        found: dict[uuid.UUID, list[CompanyProfile]] = {}
        for profile_id, company in rows.all():
            found.setdefault(profile_id, []).append(company)
        return found

    # --- the launch queue and the sync (phase 3B) ----------------------------------

    async def campaign_names(
        self, agency_id: uuid.UUID, campaign_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Campaign id -> name, the agency's campaigns only."""
        ids = sorted(set(campaign_ids))
        if not ids:
            return {}
        rows = await self.db.execute(
            select(EmailCampaign.id, EmailCampaign.name).where(
                EmailCampaign.id.in_(ids), EmailCampaign.agency_id == agency_id
            )
        )
        return {campaign_id: name for campaign_id, name in rows.all()}

    @staticmethod
    def _launch_order() -> tuple[Any, ...]:
        """The order a launch takes leads in: those refused last time come
        last, so they never hold back the others; then the oldest first."""
        return (
            EmailCampaignMember.status_reason.is_not(None),
            EmailCampaignMember.added_at,
            EmailCampaignMember.id,
        )

    async def ready_to_push(
        self,
        agency_id: uuid.UUID,
        campaign_id: uuid.UUID,
        limit: int,
        *,
        only: Sequence[uuid.UUID] | None = None,
    ) -> tuple[list[MemberRow], int]:
        """READY leads never handed over, in launch order, and how many wait
        in all; among `only` when a launch names them."""
        base: list[ColumnElement[bool]] = [
            EmailCampaignMember.campaign_id == campaign_id,
            EmailCampaignMember.status == "EMAIL_READY",
            EmailCampaignMember.queued_at.is_(None),
        ]
        if only is not None:
            base.append(EmailCampaignMember.id.in_(list(only)))
        total = await self.db.scalar(
            select(func.count())
            .select_from(EmailCampaignMember)
            .where(EmailCampaignMember.agency_id == agency_id, *base)
        )
        if limit <= 0:
            return [], int(total or 0)
        result = await self.db.execute(
            self._member_rows(agency_id).where(*base).order_by(*self._launch_order()).limit(limit)
        )
        return self._rows(result), int(total or 0)

    async def to_launch(
        self, agency_id: uuid.UUID, campaign_id: uuid.UUID, search: str | None
    ) -> list[MemberRow]:
        """Leads never handed over and still live (ready or paused), in the
        order a launch takes them."""
        conditions: list[ColumnElement[bool]] = [
            EmailCampaignMember.campaign_id == campaign_id,
            EmailCampaignMember.queued_at.is_(None),
            EmailCampaignMember.status.in_(("EMAIL_READY", "EMAIL_PAUSED")),
        ]
        if search and search.strip():
            conditions.append(self._member_search(agency_id, search.strip()))
        result = await self.db.execute(
            self._member_rows(agency_id).where(*conditions).order_by(*self._launch_order())
        )
        return self._rows(result)

    async def to_launch_count(self, campaign_id: uuid.UUID) -> int:
        """Leads never handed over and still live (ready or paused)."""
        value = await self.db.scalar(
            select(func.count())
            .select_from(EmailCampaignMember)
            .where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.queued_at.is_(None),
                EmailCampaignMember.status.in_(("EMAIL_READY", "EMAIL_PAUSED")),
            )
        )
        return int(value or 0)

    async def launched_count(self, campaign_id: uuid.UUID) -> int:
        """Leads ever handed to the provider (a dry run's hand-over included)."""
        value = await self.db.scalar(
            select(func.count())
            .select_from(EmailCampaignMember)
            .where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.queued_at.is_not(None),
            )
        )
        return int(value or 0)

    async def queued_since(self, campaign_id: uuid.UUID, since: datetime) -> int:
        """Leads handed to the provider since `since` (the daily limit)."""
        value = await self.db.scalar(
            select(func.count())
            .select_from(EmailCampaignMember)
            .where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.queued_at >= since,
            )
        )
        return int(value or 0)

    async def in_flight(self, agency_id: uuid.UUID, campaign_id: uuid.UUID) -> list[MemberRow]:
        """Leads the provider holds and may still email (queued, active,
        paused), with their client record and account."""
        result = await self.db.execute(
            self._member_rows(agency_id)
            .where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.status.in_(("EMAIL_QUEUED", "EMAIL_ACTIVE", "EMAIL_PAUSED")),
                EmailCampaignMember.provider_lead_id.is_not(None),
            )
            .order_by(EmailCampaignMember.id)
        )
        return self._rows(result)

    async def completed_with_leads(self, campaign_id: uuid.UUID) -> list[EmailCampaignMember]:
        """Leads that completed the sequence and whose lead the provider has."""
        rows = await self.db.execute(
            select(EmailCampaignMember)
            .where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.status == "EMAIL_COMPLETED",
                EmailCampaignMember.provider_lead_id.is_not(None),
            )
            .order_by(EmailCampaignMember.id)
        )
        return list(rows.scalars().all())

    async def with_provider_ids(self, campaign_id: uuid.UUID) -> list[EmailCampaignMember]:
        rows = await self.db.execute(
            select(EmailCampaignMember)
            .where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.provider_lead_id.is_not(None),
            )
            .order_by(EmailCampaignMember.id)
        )
        return list(rows.scalars().all())

    # --- a client record's memberships (route 33) ----------------------------------

    async def member_rows_of_profile(
        self, agency_id: uuid.UUID, profile_id: uuid.UUID
    ) -> list[MemberRow]:
        """Every membership of the client record in the agency's campaigns,
        ended and removed ones included, newest first."""
        result = await self.db.execute(
            self._member_rows(agency_id)
            .where(EmailCampaignMember.client_profile_id == profile_id)
            .order_by(EmailCampaignMember.added_at.desc(), EmailCampaignMember.id.desc())
        )
        return self._rows(result)

    # --- the add wizard (routes 34 to 37) ------------------------------------------

    @staticmethod
    def _live_profiles(agency_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
        """Client records with a live membership in the agency."""
        return select(EmailCampaignMember.client_profile_id).where(
            EmailCampaignMember.agency_id == agency_id,
            EmailCampaignMember.status.in_(LIVE_MEMBER_STATUSES),
        )

    async def candidate_clients(
        self,
        agency_id: uuid.UUID,
        criteria: ProfileListFilter,
        *,
        client_statuses: Sequence[str],
        has_email: bool | None,
        campaign_id: uuid.UUID | str | None,
        preferred_langs: Sequence[str] = (),
        agency_default_lang: str | None = None,
        sort_by: str,
        sort_order: str,
        page: int,
        page_size: int,
    ) -> tuple[list[tuple[ClientProfile, ExpatUser | None]], int]:
        """The Clients page's list for the wizard: its criteria applied by
        its own predicates (`ClientProfilesRepository.filter_predicates`,
        not copied), its sort and paging, then the module's own criteria.

        `client_statuses`: the derived status, through the same projection
        (one value filters, both or none do not); `has_email`: an effective
        address (rule N2) or none; `campaign_id`: live in that campaign, or
        `"none"` for no live membership at all; `preferred_langs`: the
        client's EFFECTIVE language is one of them (the record's, else its
        account's, else the agency's `agency_default_lang`: the chain every
        client send follows, `core.client_lang.effective_lang_sql`)."""
        profiles = ClientProfilesRepository(self.db)
        predicates: list[Any] = profiles.filter_predicates(
            agency_id, **criteria.model_dump(exclude_none=True)
        )
        if len(set(client_statuses)) == 1:
            [status] = set(client_statuses)
            predicates.extend(profiles.filter_predicates(agency_id, status=status))
        address = func.coalesce(func.trim(effective_email_sql()), "")
        if has_email is True:
            predicates.append(address != "")
        elif has_email is False:
            predicates.append(address == "")
        if preferred_langs:
            predicates.append(
                effective_lang_sql(agency_default_lang).in_(sorted(set(preferred_langs)))
            )
        live = self._live_profiles(agency_id)
        if campaign_id == "none":
            predicates.append(ClientProfile.id.not_in(live))
        elif campaign_id is not None:
            predicates.append(
                ClientProfile.id.in_(live.where(EmailCampaignMember.campaign_id == campaign_id))
            )
        # The OUTER join `filter_predicates` asks for (search,
        # client_space_activated), also where the identity and the
        # effective address are read.
        base = (
            select(ClientProfile, ExpatUser)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(*predicates)
        )
        total = await self.db.scalar(select(func.count()).select_from(base.subquery()))
        # The Clients page's order (`ClientProfilesRepository.list_page`).
        name_order = (
            func.coalesce(ExpatUser.last_name, ClientProfile.last_name),
            func.coalesce(ExpatUser.first_name, ClientProfile.first_name),
        )
        sort_exprs: dict[str, tuple[Any, ...]] = {
            "name": name_order,
            "created_at": (ClientProfile.created_at,),
            "last_activity": (ClientProfilesRepository._last_activity_expr(),),
        }
        exprs = sort_exprs.get(sort_by, name_order)
        ordered = [e.desc() if sort_order == "desc" else e.asc() for e in exprs]
        rows = await self.db.execute(
            base.order_by(*ordered, ClientProfile.created_at)
            .offset((page - 1) * page_size)
            .limit(page_size)
            .execution_options(populate_existing=True)
        )
        return [(profile, account) for profile, account in rows.all()], int(total or 0)

    async def candidate_companies(
        self,
        agency_id: uuid.UUID,
        criteria: CompanyListFilter,
        *,
        only_with_ready: bool,
        with_email: bool,
        sort_by: str,
        sort_order: str,
        page: int,
        page_size: int,
    ) -> tuple[list[tuple[CompanyProfile, int, int, int]], int]:
        """The Companies page's list for the wizard (its predicates, sort and
        paging), each company with its linked clients, those with an
        effective address and no block on record (eligible: none of their
        own memberships unsubscribed or bounced, and their address is not
        on the opposition list), and those also in no live campaign (ready).
        A client counts once per company, whatever its roles there.
        `only_with_ready`: a ready client at least; `with_email`: a client
        with an address at least."""
        has_email = func.coalesce(func.trim(effective_email_sql()), "") != ""
        blocked = select(EmailCampaignMember.client_profile_id).where(
            EmailCampaignMember.agency_id == agency_id,
            EmailCampaignMember.status.in_(BLOCKING_MEMBER_STATUSES),
        )
        listed = exists(
            select(EmailSuppression.id).where(
                EmailSuppression.agency_id == agency_id,
                EmailSuppression.email_hash == email_hash_sql(effective_email_sql()),
            )
        )
        eligible = and_(has_email, ClientProfile.id.not_in(blocked), not_(listed))
        ready = and_(eligible, ClientProfile.id.not_in(self._live_profiles(agency_id)))
        person = ClientProfile.id
        counts = (
            select(
                CompanyProfileRole.company_profile_id.label("company_id"),
                func.count(distinct(person)).label("clients"),
                func.count(distinct(case((has_email, person)))).label("with_email"),
                func.count(distinct(case((eligible, person)))).label("eligible"),
                func.count(distinct(case((ready, person)))).label("ready"),
            )
            .select_from(CompanyProfileRole)
            .join(ClientProfile, ClientProfile.id == CompanyProfileRole.client_profile_id)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(ClientProfile.agency_id == agency_id)
            .group_by(CompanyProfileRole.company_profile_id)
            .subquery()
        )
        clients = func.coalesce(counts.c.clients, 0)
        eligible_count = func.coalesce(counts.c.eligible, 0)
        ready_count = func.coalesce(counts.c.ready, 0)
        predicates = CompanyProfilesRepository(self.db).filter_predicates(
            agency_id, **criteria.model_dump(exclude_none=True)
        )
        base = (
            select(CompanyProfile, clients, eligible_count, ready_count)
            .outerjoin(counts, counts.c.company_id == CompanyProfile.id)
            .where(*predicates)
        )
        if only_with_ready:
            base = base.where(ready_count > 0)
        if with_email:
            base = base.where(func.coalesce(counts.c.with_email, 0) > 0)
        total = await self.db.scalar(select(func.count()).select_from(base.subquery()))
        # The Companies page's order (`CompanyProfilesRepository.list_page`).
        sort_exprs: dict[str, tuple[Any, ...]] = {
            "name": (CompanyProfile.name,),
            "created_at": (CompanyProfile.created_at,),
            "last_activity": (CompanyProfilesRepository._last_activity_expr(),),
        }
        exprs = sort_exprs.get(sort_by, (CompanyProfile.name,))
        ordered = [e.desc() if sort_order == "desc" else e.asc() for e in exprs]
        rows = await self.db.execute(
            base.order_by(*ordered, CompanyProfile.created_at)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return (
            [(company, int(n), int(e), int(r)) for company, n, e, r in rows.all()],
            int(total or 0),
        )

    async def company_statuses(
        self, agency_id: uuid.UUID, company_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[str]]:
        """Company -> the statuses of its linked clients' memberships."""
        if not company_ids:
            return {}
        rows = await self.db.execute(
            select(CompanyProfileRole.company_profile_id, EmailCampaignMember.status)
            .join(
                EmailCampaignMember,
                EmailCampaignMember.client_profile_id == CompanyProfileRole.client_profile_id,
            )
            .where(
                CompanyProfileRole.company_profile_id.in_(list(company_ids)),
                EmailCampaignMember.agency_id == agency_id,
            )
        )
        found: dict[uuid.UUID, list[str]] = {}
        for company_id, status in rows.all():
            found.setdefault(company_id, []).append(status)
        return found

    async def company_ids_in_agency(
        self, agency_id: uuid.UUID, company_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        if not company_ids:
            return set()
        rows = await self.db.execute(
            select(CompanyProfile.id).where(
                CompanyProfile.id.in_(list(company_ids)), CompanyProfile.agency_id == agency_id
            )
        )
        return set(rows.scalars().all())

    async def profiles_with_accounts(
        self, agency_id: uuid.UUID, profile_ids: Sequence[uuid.UUID]
    ) -> list[tuple[ClientProfile, ExpatUser | None]]:
        """These client records of the agency with their account, in the
        order given; another agency's are simply absent."""
        if not profile_ids:
            return []
        rows = await self.db.execute(
            select(ClientProfile, ExpatUser)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(ClientProfile.id.in_(list(profile_ids)), ClientProfile.agency_id == agency_id)
            .execution_options(populate_existing=True)
        )
        by_id = {profile.id: (profile, account) for profile, account in rows.all()}
        return [by_id[i] for i in profile_ids if i in by_id]

    async def profiles_of_companies(
        self, agency_id: uuid.UUID, company_ids: Sequence[uuid.UUID]
    ) -> list[uuid.UUID]:
        """A company brings every client linked to it by a role that has an
        effective address: once each, company by company, by name."""
        if not company_ids:
            return []
        rows = await self.db.execute(
            select(ClientProfile.id)
            .select_from(CompanyProfileRole)
            .join(ClientProfile, ClientProfile.id == CompanyProfileRole.client_profile_id)
            .join(CompanyProfile, CompanyProfile.id == CompanyProfileRole.company_profile_id)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(
                CompanyProfileRole.company_profile_id.in_(list(company_ids)),
                CompanyProfile.agency_id == agency_id,
                ClientProfile.agency_id == agency_id,
                func.coalesce(func.trim(effective_email_sql()), "") != "",
            )
            .order_by(
                CompanyProfile.name,
                CompanyProfile.id,
                func.coalesce(ExpatUser.last_name, ClientProfile.last_name),
                func.coalesce(ExpatUser.first_name, ClientProfile.first_name),
                ClientProfile.id,
            )
        )
        return list(dict.fromkeys(rows.scalars().all()))

    # --- performance (route 12): leads, never messages, unless said ---------------
    #
    # A lead is one client in one campaign (a membership). With a period, the
    # cohort is the leads launched in it (handed over at `queued_at`) and
    # every count follows them, whenever their emails went out; without one,
    # every lead of the campaign.

    async def message_totals(
        self, campaign_id: uuid.UUID, *, since: datetime | None, until: datetime | None
    ) -> dict[str, int]:
        """The cohort's emails (messages, not leads): sent, opened and clicked
        once at least, every open and click, replies received, bounced."""
        lead = EmailCampaignMember
        message = EmailMessage
        outbound = message.direction == "OUTBOUND"
        row = (
            await self.db.execute(
                select(
                    func.count(case((and_(outbound, message.sent_at.is_not(None)), 1))),
                    func.count(case((and_(outbound, message.open_count > 0), 1))),
                    func.coalesce(func.sum(case((outbound, message.open_count), else_=0)), 0),
                    func.count(case((and_(outbound, message.click_count > 0), 1))),
                    func.coalesce(func.sum(case((outbound, message.click_count), else_=0)), 0),
                    func.count(case((message.direction == "INBOUND", 1))),
                    func.count(case((and_(outbound, message.bounced_at.is_not(None)), 1))),
                )
                .select_from(lead)
                .join(message, message.campaign_member_id == lead.id)
                .where(*self._cohort([campaign_id], since, until))
            )
        ).one()
        keys = ("sent", "opened", "opens", "clicked", "clicks", "replies", "bounced")
        return {key: int(value or 0) for key, value in zip(keys, row, strict=True)}

    async def step_funnel(
        self, campaign_id: uuid.UUID, *, since: datetime | None, until: datetime | None
    ) -> dict[int | None, dict[str, int]]:
        """Per step position, the cohort's leads: entered (its email sent to
        them), opened / clicked / bounced that email, replied to it (a reply
        counts for the step it answers), unsubscribed from it; and the emails
        sent. `None`: replies and unsubscribes tied to no step."""
        lead = EmailCampaignMember
        message = EmailMessage
        cohort = self._cohort([campaign_id], since, until)
        steps: dict[int | None, dict[str, int]] = {}

        def bucket(position: int | None) -> dict[str, int]:
            return steps.setdefault(position, dict.fromkeys(_STEP_KEYS, 0))

        outbound = await self.db.execute(
            select(
                message.step_position,
                func.count(distinct(case((message.sent_at.is_not(None), lead.id)))),
                func.count(case((message.sent_at.is_not(None), 1))),
                func.count(distinct(case((message.open_count > 0, lead.id)))),
                func.count(distinct(case((message.click_count > 0, lead.id)))),
                func.count(distinct(case((message.bounced_at.is_not(None), lead.id)))),
            )
            .select_from(lead)
            .join(message, message.campaign_member_id == lead.id)
            .where(*cohort, message.direction == "OUTBOUND")
            .group_by(message.step_position)
        )
        for position, entered, sent, opened, clicked, bounced in outbound.all():
            bucket(position).update(
                entered=int(entered),
                sent=int(sent),
                opened=int(opened),
                clicked=int(clicked),
                bounced=int(bounced),
            )
        replies = await self.db.execute(
            select(message.step_position, func.count(distinct(lead.id)))
            .select_from(lead)
            .join(message, message.campaign_member_id == lead.id)
            .where(*cohort, message.direction == "INBOUND")
            .group_by(message.step_position)
        )
        for position, count in replies.all():
            bucket(position)["replied"] = int(count)
        event = EmailProviderEvent
        unsubscribes = await self.db.execute(
            select(event.step_position, func.count(distinct(lead.id)))
            .select_from(lead)
            .join(event, event.campaign_member_id == lead.id)
            .where(*cohort, event.type == "UNSUBSCRIBED")
            .group_by(event.step_position)
        )
        for position, count in unsubscribes.all():
            bucket(position)["unsubscribed"] = int(count)
        return steps

    async def performance_timeline(
        self,
        campaign_id: uuid.UUID,
        *,
        timezone: str,
        since: datetime | None,
        until: datetime | None,
    ) -> dict[date, dict[str, int]]:
        """Day by day, in the campaign's timezone, the cohort's leads:
        launched that day, and emailed / opening / clicking / replying /
        bouncing / unsubscribing that day (distinct leads, never messages,
        from the events stored once). Only days with activity, oldest first."""
        lead = EmailCampaignMember
        event = EmailProviderEvent
        cohort = self._cohort([campaign_id], since, until)
        days: dict[date, dict[str, int]] = {}

        def bucket(day: date) -> dict[str, int]:
            return days.setdefault(day, dict.fromkeys(_DAY_KEYS, 0))

        launched_day = cast(func.timezone(timezone, lead.queued_at), Date)
        launched = await self.db.execute(
            select(launched_day, func.count(distinct(lead.id)))
            .where(*cohort, lead.queued_at.is_not(None))
            .group_by(launched_day)
        )
        for day, count in launched.all():
            bucket(day)["launched"] = int(count)
        event_day = cast(func.timezone(timezone, event.occurred_at), Date)
        activity = await self.db.execute(
            select(event_day, event.type, func.count(distinct(lead.id)))
            .select_from(lead)
            .join(event, event.campaign_member_id == lead.id)
            .where(*cohort, event.type.in_(list(_DAY_EVENTS)))
            .group_by(event_day, event.type)
        )
        for day, kind, count in activity.all():
            bucket(day)[_DAY_EVENTS[kind]] = int(count)
        return dict(sorted(days.items()))

    async def cohort_states(
        self, campaign_id: uuid.UUID, *, since: datetime | None, until: datetime | None
    ) -> dict[str, int]:
        """The cohort's leads by their state now."""
        rows = await self.db.execute(
            select(EmailCampaignMember.status, func.count())
            .where(*self._cohort([campaign_id], since, until))
            .group_by(EmailCampaignMember.status)
        )
        return {status: int(count) for status, count in rows.all()}

    # --- outreach (route 11): the agency's activity, day by day -------------------

    def _outreach_scope(
        self, agency_id: uuid.UUID, filters: dict[str, Any]
    ) -> list[ColumnElement[bool]]:
        """The queue's filters, on top of the agency scope of `_in_agency`."""
        conditions: list[ColumnElement[bool]] = []
        if filters.get("campaign_id"):
            conditions.append(EmailCampaignMember.campaign_id == filters["campaign_id"])
        if filters.get("status"):
            conditions.append(EmailCampaignMember.status.in_(list(filters["status"])))
        if filters.get("sender"):
            conditions.append(
                EmailCampaign.settings["sender"]["email"].astext == str(filters["sender"])
            )
        search = (filters.get("search") or "").strip()
        if search:
            conditions.append(self._member_search(agency_id, search))
        return conditions

    async def outreach_past(
        self,
        agency_id: uuid.UUID,
        since: datetime,
        until: datetime,
        filters: dict[str, Any],
        limit: int,
    ) -> list[tuple[str, datetime, int | None, MemberRow]]:
        """What the provider reported in [since, until): emails sent,
        replies, bounces, unsubscribes; (kind, at, step, membership row)."""
        scope = self._outreach_scope(agency_id, filters)
        out: list[tuple[str, datetime, int | None, MemberRow]] = []
        message = EmailMessage
        outbound = message.direction == "OUTBOUND"
        for kind, at, extra in (
            ("SENT", message.sent_at, outbound),
            ("REPLIED", message.received_at, message.direction == "INBOUND"),
            ("BOUNCED", message.bounced_at, outbound),
        ):
            rows = await self.db.execute(
                self._in_agency(
                    select(
                        EmailCampaignMember,
                        ClientProfile,
                        ExpatUser,
                        EmailCampaign,
                        at,
                        message.step_position,
                    ),
                    agency_id,
                )
                .join(message, message.campaign_member_id == EmailCampaignMember.id)
                .where(*scope, extra, at >= since, at < until)
                .order_by(at.desc())
                .limit(limit)
            )
            for member, profile, account, campaign, when, step in rows.all():
                out.append((kind, when, step, MemberRow(member, profile, account, campaign)))
        event = EmailProviderEvent
        rows = await self.db.execute(
            self._in_agency(
                select(
                    EmailCampaignMember,
                    ClientProfile,
                    ExpatUser,
                    EmailCampaign,
                    event.occurred_at,
                    event.step_position,
                ),
                agency_id,
            )
            .join(event, event.campaign_member_id == EmailCampaignMember.id)
            .where(
                *scope,
                event.type == "UNSUBSCRIBED",
                event.occurred_at >= since,
                event.occurred_at < until,
            )
            .order_by(event.occurred_at.desc())
            .limit(limit)
        )
        for member, profile, account, campaign, when, step in rows.all():
            out.append(("UNSUBSCRIBED", when, step, MemberRow(member, profile, account, campaign)))
        return out

    async def outreach_in_flight(
        self, agency_id: uuid.UUID, filters: dict[str, Any]
    ) -> list[MemberRow]:
        """Launched leads that may still be sent something (queued or in
        sequence): the ones a date can be estimated for."""
        result = await self.db.execute(
            self._member_rows(agency_id)
            .where(
                *self._outreach_scope(agency_id, filters),
                EmailCampaignMember.status.in_(("EMAIL_QUEUED", "EMAIL_ACTIVE")),
                EmailCampaignMember.queued_at.is_not(None),
            )
            .order_by(EmailCampaignMember.id)
        )
        return self._rows(result)

    async def sent_days(
        self, member_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, dict[int, datetime]]:
        """Membership -> step position -> when its email was sent (the
        message, else the frozen render)."""
        sent: dict[uuid.UUID, dict[int, datetime]] = {}
        if not member_ids:
            return sent
        ids = list(member_ids)
        rows = await self.db.execute(
            select(
                EmailMessage.campaign_member_id, EmailMessage.step_position, EmailMessage.sent_at
            ).where(
                EmailMessage.campaign_member_id.in_(ids),
                EmailMessage.direction == "OUTBOUND",
                EmailMessage.sent_at.is_not(None),
                EmailMessage.step_position.is_not(None),
            )
        )
        for member_id, position, at in rows.all():
            sent.setdefault(member_id, {})[position] = at
        renders = await self.db.execute(
            select(
                EmailMemberRender.campaign_member_id,
                EmailMemberRender.step_position,
                EmailMemberRender.sent_at,
            ).where(
                EmailMemberRender.campaign_member_id.in_(ids),
                EmailMemberRender.sent_at.is_not(None),
            )
        )
        for member_id, position, at in renders.all():
            sent.setdefault(member_id, {}).setdefault(position, at)
        return sent

    async def outreach_held(
        self, agency_id: uuid.UUID, filters: dict[str, Any]
    ) -> tuple[int, int, int]:
        """(waiting launch, on hold, dry run): leads never launched yet;
        launched leads paused, or whose campaign is not active; leads
        launched in a dry run whose sequence has not ended. Each lead is
        counted once, under the reason `schedule.hold_of` gives it: a dry
        run's lead is never "on hold", whatever its pause or campaign."""
        base = self._in_agency(select(func.count(EmailCampaignMember.id)), agency_id).where(
            *self._outreach_scope(agency_id, filters)
        )
        simulated = _dry_run_id(EmailCampaignMember.provider_lead_id)
        waiting = await self.db.scalar(
            base.where(
                EmailCampaignMember.status == "EMAIL_READY",
                EmailCampaignMember.queued_at.is_(None),
            )
        )
        held = await self.db.scalar(
            base.where(
                EmailCampaignMember.queued_at.is_not(None),
                or_(EmailCampaignMember.provider_lead_id.is_(None), not_(simulated)),
                or_(
                    EmailCampaignMember.status == "EMAIL_PAUSED",
                    and_(
                        EmailCampaignMember.status.in_(("EMAIL_QUEUED", "EMAIL_ACTIVE")),
                        EmailCampaign.status != "ACTIVE",
                    ),
                ),
            )
        )
        dry_run = await self.db.scalar(
            base.where(
                EmailCampaignMember.queued_at.is_not(None),
                simulated,
                EmailCampaignMember.status.in_(LIVE_MEMBER_STATUSES),
            )
        )
        return int(waiting or 0), int(held or 0), int(dry_run or 0)

    # --- the deletion hooks (spec 8.11) --------------------------------------------

    async def real_leads_of_profiles(
        self, agency_id: uuid.UUID, profile_ids: Sequence[uuid.UUID]
    ) -> list[tuple[EmailCampaignMember, EmailCampaign]]:
        """The memberships of these client records whose lead is (still) at
        Lemlist: a real lead id (not a dry-run one) in a real campaign, and
        not taken out already (a removal or a move removed it there)."""
        found: list[tuple[EmailCampaignMember, EmailCampaign]] = []
        ids = list(dict.fromkeys(profile_ids))
        for start in range(0, len(ids), _IDS_PER_QUERY):
            rows = await self.db.execute(
                select(EmailCampaignMember, EmailCampaign)
                .join(EmailCampaign, EmailCampaign.id == EmailCampaignMember.campaign_id)
                .where(
                    EmailCampaignMember.agency_id == agency_id,
                    EmailCampaign.agency_id == agency_id,
                    EmailCampaignMember.client_profile_id.in_(ids[start : start + _IDS_PER_QUERY]),
                    EmailCampaignMember.provider_lead_id.is_not(None),
                    EmailCampaignMember.provider_lead_id != "",
                    not_(_dry_run_id(EmailCampaignMember.provider_lead_id)),
                    EmailCampaignMember.removed_at.is_(None),
                    real_provider_campaign(),
                )
                .order_by(EmailCampaignMember.client_profile_id, EmailCampaignMember.id)
            )
            found.extend((member, campaign) for member, campaign in rows.all())
        return found

    async def real_provider_campaigns(self, agency_id: uuid.UUID) -> list[EmailCampaign]:
        """The agency's campaigns that exist at Lemlist, any status."""
        rows = await self.db.execute(
            select(EmailCampaign)
            .where(EmailCampaign.agency_id == agency_id, real_provider_campaign())
            .order_by(EmailCampaign.created_at, EmailCampaign.id)
        )
        return list(rows.scalars().all())

    async def real_leads_of_campaign(self, campaign_id: uuid.UUID) -> list[EmailCampaignMember]:
        """A campaign's memberships whose lead is (still) at Lemlist."""
        rows = await self.db.execute(
            select(EmailCampaignMember)
            .where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.provider_lead_id.is_not(None),
                EmailCampaignMember.provider_lead_id != "",
                not_(_dry_run_id(EmailCampaignMember.provider_lead_id)),
                EmailCampaignMember.removed_at.is_(None),
            )
            .order_by(EmailCampaignMember.id)
        )
        return list(rows.scalars().all())

    # --- the provider's events (phase 4: one ingestion) ----------------------------

    async def store_event(self, values: dict[str, Any]) -> bool:
        """Insert a provider event once: a second delivery of the same event
        (webhook, job or Sync) is a no-op (`uq_email_provider_event`). True
        when this call stored it."""
        result = await self.db.execute(
            insert(EmailProviderEvent)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_email_provider_event")
            .returning(EmailProviderEvent.id)
        )
        return result.scalar() is not None

    async def campaign_by_provider_id(
        self, provider: str, provider_campaign_id: str
    ) -> EmailCampaign | None:
        """The campaign Lemlist knows by this id, whatever its agency (the
        webhook and the job have no agent: the agency is the campaign's,
        rule N1), steps loaded. A dry-run id never matches: such a campaign
        does not exist at the provider."""
        stmt = (
            self._with_steps()
            .where(
                EmailCampaign.provider == provider,
                EmailCampaign.provider_campaign_id == provider_campaign_id,
                real_provider_campaign(),
            )
            .order_by(EmailCampaign.created_at, EmailCampaign.id)
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalars().first()

    async def member_for_event(
        self,
        campaign_id: uuid.UUID,
        lead_id: str | None,
        email: str | None,
        contact_id: str | None = None,
    ) -> EmailCampaignMember | None:
        """The membership an event is about, locked (the webhook and the job
        may apply events to the same one at once): by its lead id, else by
        its provider contact id, else by the client's EFFECTIVE address
        (rule N2), an open membership first."""
        if lead_id:
            found = (
                (
                    await self.db.execute(
                        select(EmailCampaignMember)
                        .where(
                            EmailCampaignMember.campaign_id == campaign_id,
                            EmailCampaignMember.provider_lead_id == lead_id,
                        )
                        .order_by(EmailCampaignMember.added_at.desc(), EmailCampaignMember.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .first()
            )
            if found is not None:
                return found
        return await self._member_of(
            [EmailCampaignMember.campaign_id == campaign_id], contact_id, email, live_first=False
        )

    async def member_for_contact(
        self, agency_id: uuid.UUID, contact_id: str | None, email: str | None
    ) -> EmailCampaignMember | None:
        """The membership a provider event about a whole CONTACT is filed
        under, in the agency, locked: by the provider contact id, else by
        the client's effective address; a live membership first, then an
        open one, then the latest."""
        return await self._member_of(
            [EmailCampaignMember.agency_id == agency_id], contact_id, email, live_first=True
        )

    async def _member_of(
        self,
        scope: Sequence[ColumnElement[bool]],
        contact_id: str | None,
        email: str | None,
        *,
        live_first: bool,
    ) -> EmailCampaignMember | None:
        """The membership in `scope` holding this provider contact id, else
        one whose client's effective address is `email`, locked."""
        order: list[Any] = [
            EmailCampaignMember.removed_at.is_(None).desc(),
            EmailCampaignMember.added_at.desc(),
            EmailCampaignMember.id,
        ]
        if live_first:
            order.insert(0, EmailCampaignMember.status.in_(LIVE_MEMBER_STATUSES).desc())
        if contact_id:
            found = (
                (
                    await self.db.execute(
                        select(EmailCampaignMember)
                        .where(*scope, EmailCampaignMember.provider_contact_id == contact_id)
                        .order_by(*order)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .first()
            )
            if found is not None:
                return found
        address = (email or "").strip().lower()
        if not address:
            return None
        return (
            (
                await self.db.execute(
                    select(EmailCampaignMember)
                    .join(ClientProfile, ClientProfile.id == EmailCampaignMember.client_profile_id)
                    .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
                    .where(*scope, func.lower(effective_email_sql()) == address)
                    .order_by(*order)
                    .with_for_update(of=EmailCampaignMember)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )

    async def live_members_of_contact(
        self, agency_id: uuid.UUID, contact_id: str | None, digests: Iterable[str]
    ) -> list[EmailCampaignMember]:
        """The agency's live memberships of one provider contact: those
        holding its contact id, and those whose client's effective address
        has one of these fingerprints (`suppression.email_hash`, the
        opposition list's own comparison), locked."""
        wanted = sorted({digest for digest in digests if digest})
        reached: list[ColumnElement[bool]] = []
        if contact_id:
            reached.append(EmailCampaignMember.provider_contact_id == contact_id)
        if wanted:
            reached.append(email_hash_sql(effective_email_sql()).in_(wanted))
        if not reached:
            return []
        rows = await self.db.execute(
            select(EmailCampaignMember)
            .join(ClientProfile, ClientProfile.id == EmailCampaignMember.client_profile_id)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(
                EmailCampaignMember.agency_id == agency_id,
                EmailCampaignMember.status.in_(LIVE_MEMBER_STATUSES),
                or_(*reached),
            )
            .order_by(EmailCampaignMember.id)
            .with_for_update(of=EmailCampaignMember)
            .execution_options(populate_existing=True)
        )
        return list(rows.scalars().all())

    async def member_locked(self, member_id: uuid.UUID) -> EmailCampaignMember | None:
        """One membership, locked and fresh (a pause state re-read)."""
        return (
            await self.db.execute(
                select(EmailCampaignMember)
                .where(EmailCampaignMember.id == member_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    # --- the Email Sync job's reconciliation (phase 4) ------------------------------

    async def enabled_modules(self) -> list[AgencyEmailModule]:
        """Every agency whose own switch is on (the job then keeps those
        bound to the team the platform key opens)."""
        rows = await self.db.execute(
            select(AgencyEmailModule)
            .where(AgencyEmailModule.enabled.is_(True))
            .order_by(AgencyEmailModule.agency_id)
        )
        return list(rows.scalars().all())

    async def reconcilable_campaigns(self, agency_id: uuid.UUID) -> list[EmailCampaign]:
        """The agency's campaigns the job reads: at Lemlist for real, and
        still sending or able to receive replies (ACTIVE or PAUSED). An
        archived campaign is left to the webhook."""
        rows = await self.db.execute(
            select(EmailCampaign)
            .where(
                EmailCampaign.agency_id == agency_id,
                EmailCampaign.status.in_(("ACTIVE", "PAUSED")),
                real_provider_campaign(),
            )
            .order_by(EmailCampaign.created_at, EmailCampaign.id)
        )
        return list(rows.scalars().all())

    async def dry_run_campaign_count(self, agency_id: uuid.UUID) -> int:
        """ACTIVE or PAUSED campaigns of the agency that only a dry run made."""
        value = await self.db.scalar(
            select(func.count())
            .select_from(EmailCampaign)
            .where(
                EmailCampaign.agency_id == agency_id,
                EmailCampaign.status.in_(("ACTIVE", "PAUSED")),
                _dry_run_id(EmailCampaign.provider_campaign_id),
            )
        )
        return int(value or 0)

    async def at_provider(self, campaign_id: uuid.UUID) -> list[EmailCampaignMember]:
        """The memberships whose lead Lemlist holds (dry-run leads and
        removed memberships excluded), locked."""
        rows = await self.db.execute(
            select(EmailCampaignMember)
            .where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.provider_lead_id.is_not(None),
                EmailCampaignMember.provider_lead_id != "",
                not_(_dry_run_id(EmailCampaignMember.provider_lead_id)),
                EmailCampaignMember.removed_at.is_(None),
            )
            .order_by(EmailCampaignMember.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list(rows.scalars().all())

    # --- messages (phase 4) ------------------------------------------------------------

    async def outbound_message(self, values: dict[str, Any]) -> EmailMessage:
        """The membership's email at one step, created the first time an
        event mentions it, then locked: counts are added to, never
        overwritten (`uq_email_message_outbound_step`)."""
        await self.db.execute(
            insert(EmailMessage)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["campaign_member_id", "step_position"],
                # Literal: PostgreSQL infers a partial index from its very
                # predicate, never from bound parameters.
                index_where=text("direction = 'OUTBOUND' AND step_position IS NOT NULL"),
            )
        )
        return (
            await self.db.execute(
                select(EmailMessage)
                .where(
                    EmailMessage.campaign_member_id == values["campaign_member_id"],
                    EmailMessage.direction == "OUTBOUND",
                    EmailMessage.step_position == values["step_position"],
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    async def insert_message(self, values: dict[str, Any]) -> bool:
        """Insert a message once per provider message id
        (`uq_email_message_provider_id`); False when it exists."""
        result = await self.db.execute(
            insert(EmailMessage)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["provider", "provider_message_id"],
                index_where=text("provider_message_id IS NOT NULL"),
            )
            .returning(EmailMessage.id)
        )
        return result.scalar() is not None

    async def pending_reply_count(self, campaign_id: uuid.UUID) -> int:
        """Replies of the campaign whose content the job still has to read."""
        value = await self.db.scalar(
            select(func.count())
            .select_from(EmailMessage)
            .where(
                EmailMessage.campaign_id == campaign_id,
                EmailMessage.content_state == "PENDING",
            )
        )
        return int(value or 0)

    # --- the import of the Lemlist campaigns already running (lot C) -----------------

    async def lock_import(self, agency_id: uuid.UUID) -> None:
        """Hold the agency's import until the transaction ends: two imports
        of one agency never interleave (one campaign, its client records and
        memberships at a time), whoever runs them."""
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('email_import:' || :agency, 0))"),
            {"agency": str(agency_id)},
        )

    async def campaigns_by_provider_ids(
        self, provider: str, provider_campaign_ids: Iterable[str]
    ) -> list[EmailCampaign]:
        """The campaigns, of any agency, that carry these provider ids."""
        ids = sorted(set(provider_campaign_ids))
        if not ids:
            return []
        rows = await self.db.execute(
            select(EmailCampaign)
            .where(
                EmailCampaign.provider == provider,
                EmailCampaign.provider_campaign_id.in_(ids),
            )
            .order_by(EmailCampaign.created_at, EmailCampaign.id)
            .execution_options(populate_existing=True)
        )
        return list(rows.scalars().all())

    async def open_members(
        self, campaign_ids: Sequence[uuid.UUID], *, lock: bool = False
    ) -> list[EmailCampaignMember]:
        """The open (not ended by a removal or a move) memberships of these
        campaigns; locked when asked."""
        if not campaign_ids:
            return []
        stmt = (
            select(EmailCampaignMember)
            .where(
                EmailCampaignMember.campaign_id.in_(list(campaign_ids)),
                EmailCampaignMember.removed_at.is_(None),
            )
            .order_by(EmailCampaignMember.id)
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update()
        return list((await self.db.execute(stmt)).scalars().all())

    async def lead_ids_of_campaign(self, campaign_id: uuid.UUID) -> set[str]:
        """Every provider lead id the campaign's memberships hold, ended
        ones included."""
        rows = await self.db.execute(
            select(EmailCampaignMember.provider_lead_id).where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.provider_lead_id.is_not(None),
            )
        )
        return {str(lead_id) for lead_id in rows.scalars().all()}

    async def first_event_times(self, campaign_id: uuid.UUID) -> dict[uuid.UUID, datetime]:
        """Membership -> the time of its first provider event."""
        rows = await self.db.execute(
            select(EmailProviderEvent.campaign_member_id, func.min(EmailProviderEvent.occurred_at))
            .where(
                EmailProviderEvent.campaign_id == campaign_id,
                EmailProviderEvent.campaign_member_id.is_not(None),
            )
            .group_by(EmailProviderEvent.campaign_member_id)
        )
        return {member_id: at for member_id, at in rows.all() if member_id is not None}

    async def pending_replies(self, campaign_id: uuid.UUID, limit: int) -> list[EmailMessage]:
        """Replies of the campaign whose content was not read from the
        provider yet, newest first, locked."""
        rows = await self.db.execute(
            select(EmailMessage)
            .where(
                EmailMessage.campaign_id == campaign_id,
                EmailMessage.content_state == "PENDING",
            )
            .order_by(EmailMessage.received_at.desc().nulls_last(), EmailMessage.id)
            .limit(limit)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list(rows.scalars().all())
