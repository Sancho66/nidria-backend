"""Pure database access of the Email module (no business rule here).

Phase 1: the per-agency switch (`agency_email_module`) and the campaign facts
the platform routes decide on. The agent routes add their queries later.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from shared.models.agency import Agency
from shared.models.agency_email_module import AgencyEmailModule
from shared.models.email_campaign import EmailCampaign
from src.email_campaigns.flags import DRY_RUN_PREFIX


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
