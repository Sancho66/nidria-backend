"""Superadmin "Gérer les agences" — projects the batched rows into the
table payload, deriving the status from the model (no status column)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.admin_repository import AdminRepository
from src.admin.admin_schema import AdminAgenciesResponse, AdminAgencyRow

# Seat caps live with the subscription logic — reuse, never duplicate,
# so the table can never drift from the invitation gate.
from src.agencies.agencies_manager import SEATS_MAX_BY_PLAN, TRIAL_SEAT_LIMIT


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
    ) -> AdminAgenciesResponse:
        rows, total = await AdminRepository(self.db).list_agencies_page(
            search=search, sort=sort, order=order, page=page, page_size=page_size
        )
        now = datetime.now(UTC)
        return AdminAgenciesResponse(
            items=[self._row(r, now) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def _row(self, r: Row[Any], now: datetime) -> AdminAgencyRow:
        status, days = _status(r.trial_ends_at, r.converted_at, now)
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
        )
