"""Superadmin agencies table — ONE main query with correlated scalar
subqueries (cases/members/seats/last_login become sortable/paginable COLUMNS)
+ ONE COUNT. The adoption signals that need timestamps (the 3 onboarding
gestures) are a SMALL, FIXED number of GROUPED queries keyed on the page's
agency ids — never one per agency. Constant query count whatever the page size
or the number of agencies, proven by a query-count test. No N+1."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, ScalarSelect, and_, asc, desc, exists, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.journey import JourneyTemplate
from shared.models.usage import AgencyUsageMilestone, UsageEvent
from src.agencies.demo_case_seed import DEMO_JOURNEY_NAME

# Whitelisted sort keys → (label used both in SELECT and ORDER BY).
_SORT_KEYS = ("created_at", "name", "cases_count")

_VIEWED = "case.viewed_as_client"
# The milestone keys the onboarding gestures AND the S0/S1/S2 state read.
_ONBOARDING_KEYS = ("premier_parcours_cree", "premier_dossier_cree", "premier_client_compte_active")


class AdminRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _cases_count(self) -> ScalarSelect[int]:
        return (
            select(func.count(ClientCase.id))
            .where(
                ClientCase.agency_id == Agency.id,
                ClientCase.deleted_at.is_(None),
                ClientCase.is_demo.is_(False),
            )
            .correlate(Agency)
            .scalar_subquery()
        )

    def _members_count(self) -> ScalarSelect[int]:
        return (
            select(func.count(Agent.id))
            .where(Agent.agency_id == Agency.id)
            .correlate(Agency)
            .scalar_subquery()
        )

    def _seats_used(self) -> ScalarSelect[int]:
        return (
            select(func.count(Agent.id))
            .where(Agent.agency_id == Agency.id, Agent.is_external.is_(False))
            .correlate(Agency)
            .scalar_subquery()
        )

    def _last_login(self) -> ScalarSelect[datetime | None]:
        # Folded into the main query (no extra round-trip): MAX of the agency's
        # agents' last_login_at — the agency-level heartbeat.
        return (
            select(func.max(Agent.last_login_at))
            .where(Agent.agency_id == Agency.id)
            .correlate(Agency)
            .scalar_subquery()
        )

    def _onboarding_complete(self) -> Any:
        """SQL predicate: the 3 gestures are done — built from the SAME
        definitions as onboarding_gestures, as correlated EXISTS so it filters
        BEFORE pagination (a post-page filter would break total/page counts)."""
        has_journey = or_(
            exists().where(
                AgencyUsageMilestone.agency_id == Agency.id,
                AgencyUsageMilestone.key == "premier_parcours_cree",
            ),
            exists().where(
                JourneyTemplate.agency_id == Agency.id, JourneyTemplate.name != DEMO_JOURNEY_NAME
            ),
        )
        has_viewed = exists().where(
            UsageEvent.agency_id == Agency.id, UsageEvent.event_type == _VIEWED
        )
        has_open = or_(
            exists().where(
                AgencyUsageMilestone.agency_id == Agency.id,
                AgencyUsageMilestone.key == "premier_dossier_cree",
            ),
            has_viewed,
        )
        return and_(has_journey, has_open, has_viewed)

    async def list_agencies_page(
        self,
        *,
        search: str | None,
        sort: str,
        order: str,
        page: int,
        page_size: int,
        now: datetime,
        trial_expiring_within_days: int | None = None,
        onboarding_incomplete: bool = False,
    ) -> tuple[Sequence[Row[Any]], int]:
        cases_count = self._cases_count().label("cases_count")
        members_count = self._members_count().label("members_count")
        seats_used = self._seats_used().label("seats_used")
        last_login_at = self._last_login().label("last_login_at")

        stmt = select(
            Agency.id,
            Agency.name,
            Agency.slug,
            Agency.logo_path,
            Agency.plan,
            Agency.is_founding,
            Agency.trial_ends_at,
            Agency.converted_at,
            Agency.created_at,
            cases_count,
            members_count,
            seats_used,
            last_login_at,
        )
        count_stmt = select(func.count()).select_from(Agency)

        predicates = []
        if search:
            like = f"%{search}%"
            predicates.append(or_(Agency.name.ilike(like), Agency.slug.ilike(like)))
        if trial_expiring_within_days is not None:
            # Eric's funnel: still a trial, ending within N days (or already
            # past — just as urgent). Converted agencies are excluded.
            predicates.append(
                and_(
                    Agency.converted_at.is_(None),
                    Agency.trial_ends_at.isnot(None),
                    Agency.trial_ends_at <= now + timedelta(days=trial_expiring_within_days),
                )
            )
        if onboarding_incomplete:
            predicates.append(not_(self._onboarding_complete()))
        if predicates:
            stmt = stmt.where(*predicates)
            count_stmt = count_stmt.where(*predicates)

        # `sort` is whitelisted to a key that is always present, so this is a
        # total lookup (never None) — index, don't `.get()`, to keep the type.
        sort_key = sort if sort in _SORT_KEYS else "created_at"
        sort_col = {
            "created_at": Agency.created_at,
            "name": Agency.name,
            "cases_count": cases_count,
        }[sort_key]
        direction = desc if order == "desc" else asc
        # A stable tiebreaker (id) so pagination never repeats/drops a row.
        stmt = (
            stmt.order_by(direction(sort_col), Agency.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        rows = (await self.db.execute(stmt)).all()
        total = (await self.db.execute(count_stmt)).scalar_one()
        return rows, total

    async def adoption_batch(self, agency_ids: Sequence[Any]) -> dict[Any, dict[str, Any]]:
        """The adoption signals needing timestamps, for the page's agencies, in
        a FIXED number of GROUPED queries (never one per agency). Returns per
        agency: {'milestones': {key: first_at}, 'journey_min', 'viewed_min'}."""
        if not agency_ids:
            return {}
        out: dict[Any, dict[str, Any]] = {
            aid: {"milestones": {}, "journey_min": None, "viewed_min": None} for aid in agency_ids
        }
        milestones = await self.db.execute(
            select(
                AgencyUsageMilestone.agency_id,
                AgencyUsageMilestone.key,
                AgencyUsageMilestone.first_at,
            ).where(
                AgencyUsageMilestone.agency_id.in_(agency_ids),
                AgencyUsageMilestone.key.in_(_ONBOARDING_KEYS),
            )
        )
        for agency_id, key, first_at in milestones.all():
            out[agency_id]["milestones"][key] = first_at
        journeys = await self.db.execute(
            select(JourneyTemplate.agency_id, func.min(JourneyTemplate.created_at))
            .where(
                JourneyTemplate.agency_id.in_(agency_ids),
                JourneyTemplate.name != DEMO_JOURNEY_NAME,
            )
            .group_by(JourneyTemplate.agency_id)
        )
        for agency_id, journey_min in journeys.all():
            out[agency_id]["journey_min"] = journey_min
        viewed = await self.db.execute(
            select(UsageEvent.agency_id, func.min(UsageEvent.created_at))
            .where(UsageEvent.agency_id.in_(agency_ids), UsageEvent.event_type == _VIEWED)
            .group_by(UsageEvent.agency_id)
        )
        for agency_id, viewed_min in viewed.all():
            out[agency_id]["viewed_min"] = viewed_min
        return out
