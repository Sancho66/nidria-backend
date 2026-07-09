"""Superadmin "Gérer les agences" — projects the batched rows into the
table payload, deriving the status from the model (no status column)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.admin_repository import AdminRepository
from src.admin.admin_schema import AdminAgenciesResponse, AdminAgencyRow

# Seat caps + the SINGLE onboarding-gesture derivation live with the agency
# logic — reuse, never duplicate, so the table can never drift.
from src.agencies.agencies_manager import (
    SEATS_MAX_BY_PLAN,
    TRIAL_SEAT_LIMIT,
    onboarding_gestures,
)
from src.usage.usage_manager import classify_usage_state


def _status(
    trial_ends_at: datetime | None, converted_at: datetime | None, now: datetime
) -> tuple[str, int | None]:
    """active (converted, tested FIRST — it beats an unexpired trial) |
    trial (+ days remaining) | expired | unknown (neither set: a legacy /
    out-of-wizard anomaly, surfaced as-is, NEVER folded into expired)."""
    if converted_at is not None:
        return "active", None
    if trial_ends_at is not None:
        if trial_ends_at >= now:
            return "trial", (trial_ends_at - now).days
        return "expired", None
    return "unknown", None


class AdminManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_agencies(
        self,
        *,
        search: str | None,
        sort: str,
        order: str,
        page: int,
        page_size: int,
        trial_expiring_within_days: int | None = None,
        onboarding_incomplete: bool = False,
    ) -> AdminAgenciesResponse:
        now = datetime.now(UTC)
        repo = AdminRepository(self.db)
        rows, total = await repo.list_agencies_page(
            search=search,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
            now=now,
            trial_expiring_within_days=trial_expiring_within_days,
            onboarding_incomplete=onboarding_incomplete,
        )
        # ONE grouped batch for the page's agencies — never one query per row.
        adoption = await repo.adoption_batch([r.id for r in rows])
        return AdminAgenciesResponse(
            items=[self._row(r, now, adoption[r.id]) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def _row(self, r: Row[Any], now: datetime, adoption: dict[str, Any]) -> AdminAgencyRow:
        status, days = _status(r.trial_ends_at, r.converted_at, now)
        milestones = adoption["milestones"]
        # SAME derivation as GET /agencies/me/onboarding — journey_at resolves
        # to the milestone or the first non-demo template.
        journey_at = milestones.get("premier_parcours_cree") or adoption["journey_min"]
        return AdminAgencyRow(
            id=r.id,
            name=r.name,
            slug=r.slug,
            # The public (login-page) logo route; None when there is no logo.
            logo_url=f"/public/agencies/{r.slug}/logo" if r.logo_path else None,
            plan=r.plan,
            seats_used=r.seats_used,
            seats_limit=SEATS_MAX_BY_PLAN.get(r.plan or "", TRIAL_SEAT_LIMIT),
            is_founding=r.is_founding,
            status=status,
            trial_days_remaining=days,
            cases_count=r.cases_count,
            members_count=r.members_count,
            created_at=r.created_at,
            onboarding=onboarding_gestures(
                journey_at=journey_at,
                premier_dossier=milestones.get("premier_dossier_cree"),
                viewed=adoption["viewed_min"],
            ),
            usage_state=classify_usage_state(set(milestones)),
            last_login_at=r.last_login_at,
        )
