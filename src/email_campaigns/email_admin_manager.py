"""The Email module's switch, seen from the platform (superadmin) and read by
every surface that needs to know whether an agency has the module.

Platform gestures (routes gated `agency.create`, superadmin only, on the
pattern of PATCH /agencies/{agency_id}/subscription):
- switch the module on or off for one agency;
- bind the agency to the Lemlist team the platform key opens now, or unbind.

The rules (spec 4.1):
- switching off is refused while an ACTIVE campaign exists at Lemlist
  (`email.campaigns_live`): sending must never run without a screen to stop
  it, archive the campaign first;
- binding needs real effects and a key, and an answer from Lemlist
  (`email.provider_unavailable`), and a team serves one agency
  (`email.team_bound_elsewhere`);
- unbinding, or rebinding to another team, is refused once a campaign of the
  agency exists at Lemlist (`email.team_has_history`): moving an agency that
  went live to another team is V2.
"""

import asyncio
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agency_email_module import AgencyEmailModule
from shared.models.agent import Agent
from shared.models.email_campaign import PROVIDER_LEMLIST
from src.agencies.agencies_repository import AgenciesRepository
from src.core.config import get_settings
from src.core.exceptions import ConflictError, NotFoundError
from src.email_campaigns.email_repository import EmailRepository
from src.email_campaigns.email_schema import (
    AgencyEmailModuleResponse,
    EmailCampaignCounts,
    EmailTeamInfo,
)
from src.email_campaigns.flags import (
    email_module_effectively_enabled,
    external_effects_on,
    provider_state_for,
)
from src.email_campaigns.lemlist_team import LemlistTeam, TeamUnknownError, team_of_key


class EmailAdminManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = EmailRepository(db)

    # --- reads shared with the agency surfaces ------------------------------------

    async def email_enabled_for(self, agency_id: uuid.UUID) -> bool:
        """The agency's EFFECTIVE switch (env master AND its row): the
        `email_enabled` of its fiche."""
        if not get_settings().email_module_enabled:
            return False  # the master switch: no query needed
        return email_module_effectively_enabled(await self.repo.get_module(agency_id))

    async def email_enabled_agency_ids(self, agency_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
        """`email_enabled_for` for many agencies, in one query."""
        if not get_settings().email_module_enabled:
            return set()
        return await self.repo.enabled_agency_ids(agency_ids)

    # --- platform routes ------------------------------------------------------------

    async def get_state(self, agency_id: uuid.UUID) -> AgencyEmailModuleResponse:
        await self._agency(agency_id)
        return await self._state(agency_id)

    async def set_enabled(
        self, superadmin: Agent, agency_id: uuid.UUID, enabled: bool
    ) -> AgencyEmailModuleResponse:
        await self._agency(agency_id)
        now = datetime.now(UTC)
        if enabled:
            row = await self.repo.lock_or_create_module(agency_id)
            if not row.enabled:
                row.enabled = True
                row.enabled_at = now
                row.changed_by_agent_id = superadmin.id
            await self.db.commit()
            return await self._state(agency_id)
        row_or_none = await self.repo.get_module(agency_id, for_update=True)
        if row_or_none is not None and row_or_none.enabled:
            live = await self.repo.count_live_provider_campaigns(agency_id)
            if live:
                await self.db.rollback()
                raise ConflictError(
                    "Campaigns of this agency are running at Lemlist; archive them "
                    "before switching the Email module off.",
                    code="email.campaigns_live",
                    params={"count": live},
                )
            row_or_none.enabled = False
            row_or_none.disabled_at = now
            row_or_none.changed_by_agent_id = superadmin.id
        await self.db.commit()
        return await self._state(agency_id)

    async def bind_team(self, superadmin: Agent, agency_id: uuid.UUID) -> AgencyEmailModuleResponse:
        """Bind the agency to the team the platform key opens NOW."""
        await self._agency(agency_id)
        team = await self._key_team()
        owner = await self.repo.team_owner(PROVIDER_LEMLIST, team.id)
        if owner is not None and owner[0] != agency_id:
            raise self._bound_elsewhere(*owner)
        row = await self.repo.lock_or_create_module(agency_id)
        if row.provider_team_id is not None and row.provider_team_id != team.id:
            await self._refuse_if_history(agency_id)
        if row.provider_team_id != team.id:
            row.changed_by_agent_id = superadmin.id
        row.provider = PROVIDER_LEMLIST
        row.provider_team_id = team.id
        row.provider_team_name = team.name
        try:
            await self.db.commit()
        except IntegrityError:
            # Another agency bound the same team between our check and commit
            # (uq_agency_email_module_team): the same refusal, named.
            await self.db.rollback()
            owner = await self.repo.team_owner(PROVIDER_LEMLIST, team.id)
            if owner is None or owner[0] == agency_id:
                raise
            raise self._bound_elsewhere(*owner) from None
        return await self._state(agency_id)

    async def unbind_team(
        self, superadmin: Agent, agency_id: uuid.UUID
    ) -> AgencyEmailModuleResponse:
        await self._agency(agency_id)
        row = await self.repo.get_module(agency_id, for_update=True)
        if row is not None and row.provider_team_id is not None:
            await self._refuse_if_history(agency_id)
            row.provider_team_id = None
            row.provider_team_name = None
            row.changed_by_agent_id = superadmin.id
        await self.db.commit()
        return await self._state(agency_id)

    # --- internals --------------------------------------------------------------------

    async def _agency(self, agency_id: uuid.UUID) -> Agency:
        agency = await AgenciesRepository(self.db).get_agency(agency_id)
        if agency is None:
            raise NotFoundError("Agency not found.", code="agency.not_found")
        return agency

    async def _key_team(self) -> LemlistTeam:
        settings = get_settings()
        if not external_effects_on(settings):
            raise ConflictError(
                "Lemlist is not reachable from this platform: real effects are off, "
                "Lemlist is mocked, or there is no key.",
                code="email.provider_unavailable",
                params={"reason": "not_configured"},
            )
        try:
            team = await asyncio.to_thread(team_of_key, settings)
        except TeamUnknownError:
            raise ConflictError(
                "Lemlist did not say which team the platform key opens.",
                code="email.provider_unavailable",
                params={"reason": "team_unknown"},
            ) from None
        assert team is not None  # effects on: a team, or TeamUnknownError
        return team

    async def _refuse_if_history(self, agency_id: uuid.UUID) -> None:
        count = await self.repo.count_provider_campaigns(agency_id)
        if count:
            await self.db.rollback()
            raise ConflictError(
                "Campaigns of this agency exist in its Lemlist team; its team cannot change.",
                code="email.team_has_history",
                params={"count": count},
            )

    @staticmethod
    def _bound_elsewhere(owner_id: uuid.UUID, owner_name: str) -> ConflictError:
        return ConflictError(
            "This Lemlist team already serves another agency.",
            code="email.team_bound_elsewhere",
            params={"agency_id": str(owner_id), "agency_name": owner_name},
        )

    async def _state(self, agency_id: uuid.UUID) -> AgencyEmailModuleResponse:
        settings = get_settings()
        row: AgencyEmailModule | None = await self.repo.get_module(agency_id)
        by_status = await self.repo.count_campaigns_by_status(agency_id)
        live = await self.repo.count_live_provider_campaigns(agency_id)
        # May ask Lemlist (cached ten minutes): only when the agency is bound,
        # the module effective and effects on.
        provider = await asyncio.to_thread(provider_state_for, row, settings)
        return AgencyEmailModuleResponse(
            agency_id=agency_id,
            enabled=bool(row and row.enabled),
            master_enabled=settings.email_module_enabled,
            effective=email_module_effectively_enabled(row, settings),
            external_effects=external_effects_on(settings),
            provider=row.provider if row is not None else PROVIDER_LEMLIST,
            team=(
                EmailTeamInfo(id=row.provider_team_id, name=row.provider_team_name)
                if row is not None and row.provider_team_id is not None
                else None
            ),
            provider_mode=provider.mode,
            team_mismatch=provider.team_mismatch,
            enabled_at=row.enabled_at if row is not None else None,
            disabled_at=row.disabled_at if row is not None else None,
            changed_by_agent_id=row.changed_by_agent_id if row is not None else None,
            campaigns=EmailCampaignCounts(
                draft=by_status.get("DRAFT", 0),
                active=by_status.get("ACTIVE", 0),
                paused=by_status.get("PAUSED", 0),
                archived=by_status.get("ARCHIVED", 0),
                live_at_provider=live,
            ),
        )
