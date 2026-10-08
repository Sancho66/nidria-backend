"""Pure database access of the Email module (no business rule here).

Phase 1: the per-agency switch (`agency_email_module`) and the campaign facts
the platform routes decide on. Phase 2: what the readiness rules read beyond
a recipient's own row (`readiness.load_client_facts`). Phase 3: the agent
routes' queries, ported from Prism's `email_repository.py` (reference commit
629a2b0).

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
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Select,
    and_,
    case,
    delete,
    distinct,
    exists,
    func,
    not_,
    or_,
    select,
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
)
from shared.models.expat_user import ExpatUser
from src.client_profiles.client_profiles_repository import ClientProfilesRepository
from src.client_profiles.identity import effective_email_sql
from src.email_campaigns.providers.dry_run import DRY_RUN_PREFIX

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

    async def blocking_statuses_by_email(
        self, agency_id: uuid.UUID, emails: Iterable[str]
    ) -> list[tuple[str, uuid.UUID, str]]:
        """(lower-cased effective email, client profile id, status) of every
        UNSUBSCRIBED or BOUNCED membership of the agency whose profile's
        effective address (rule N2) is one of `emails`, removed memberships
        included (A1). Two records may share an address (risk R1)."""
        wanted = sorted({email.lower() for email in emails if email})
        if not wanted:
            return []
        address = func.lower(effective_email_sql())
        stmt = (
            select(address, EmailCampaignMember.client_profile_id, EmailCampaignMember.status)
            .select_from(EmailCampaignMember)
            .join(ClientProfile, ClientProfile.id == EmailCampaignMember.client_profile_id)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(
                EmailCampaignMember.agency_id == agency_id,
                EmailCampaignMember.status.in_(BLOCKING_MEMBER_STATUSES),
                address.in_(wanted),
            )
        )
        return [(row[0], row[1], row[2]) for row in (await self.db.execute(stmt)).all()]

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

    async def blocking_member_count(self, campaign_id: uuid.UUID) -> int:
        """Memberships of the campaign that unsubscribed or bounced, removed
        ones included (decision A1: the block lives on these rows)."""
        value = await self.db.scalar(
            select(func.count())
            .select_from(EmailCampaignMember)
            .where(
                EmailCampaignMember.campaign_id == campaign_id,
                EmailCampaignMember.status.in_(BLOCKING_MEMBER_STATUSES),
            )
        )
        return int(value or 0)

    async def delete_campaign(self, campaign_id: uuid.UUID) -> None:
        """The campaign and everything of it: steps, AI variables, memberships,
        renders and messages follow by cascade; provider events would only be
        detached (SET NULL), so they go explicitly."""
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
