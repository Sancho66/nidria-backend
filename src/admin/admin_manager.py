"""Superadmin "Gérer les agences" — projects the batched rows into the
table payload, deriving the status from the model (no status column)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.admin_repository import AdminRepository
from src.admin.admin_schema import AdminAgenciesResponse, AdminAgencyRow

# Seat rule + the SINGLE onboarding-gesture derivation live with the agency
# logic — reuse, never duplicate, so the table can never drift.
from src.agencies.agencies_manager import (
    onboarding_gestures,
    seats_max_for,
)
from src.usage.usage_manager import classify_usage_state


def _full_name(first: str | None, last: str | None) -> str | None:
    """« Prénom Nom » à partir de ce qui existe — None si les deux manquent."""
    parts = [p.strip() for p in (first, last) if p and p.strip()]
    return " ".join(parts) or None


def _status(
    trial_ends_at: datetime | None,
    converted_at: datetime | None,
    now: datetime,
    *,
    lifetime_access: bool = False,
) -> tuple[str, int | None]:
    """lifetime (accès offert, testé EN PREMIER — c'est un état d'accès, il
    prime sur toute dérivation de calendrier) | active (converted) | trial
    (+ days remaining) | expired | unknown (neither set: a legacy /
    out-of-wizard anomaly, surfaced as-is, NEVER folded into expired).

    Sans le drapeau, une agence à vie tomberait précisément dans `unknown`
    — le seau des anomalies. Le cadeau serait rangé avec les accidents."""
    if lifetime_access:
        return "lifetime", None
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
        billing_status: str | None = None,
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
            billing_status=billing_status,
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
        status, days = _status(
            r.trial_ends_at, r.converted_at, now, lifetime_access=r.lifetime_access
        )
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
            # None = no ceiling (active subscription — décision 05/08); the
            # Row carries the same four billing columns as Agency.
            seats_limit=seats_max_for(r),
            is_founding=r.is_founding,
            is_internal=r.is_internal,
            lifetime_access=r.lifetime_access,
            billing_mode=r.billing_mode,
            billing_status=r.billing_status,
            status=status,
            trial_days_remaining=days,
            trial_ends_at=r.trial_ends_at,
            signature_credits_available=r.signature_credits_available or 0,
            cases_count=r.cases_count,
            members_count=r.members_count,
            created_at=r.created_at,
            referred_by=r.referred_by,
            # Le nom recomposé en PYTHON, pas en SQL : une concaténation SQL
            # rend le NULL contagieux (un nom de famille vide effacerait le
            # prénom). None quand l'agence n'a aucun agent interne.
            owner_name=_full_name(r.owner_first_name, r.owner_last_name),
            owner_email=r.owner_email,
            contact_phone=r.contact_phone,
            utm_source=r.utm_source,
            utm_medium=r.utm_medium,
            utm_campaign=r.utm_campaign,
            referrer=r.referrer,
            acquisition_captured_at=r.acquisition_captured_at,
            onboarding=onboarding_gestures(
                journey_at=journey_at,
                premier_dossier=milestones.get("premier_dossier_cree"),
                viewed=adoption["viewed_min"],
            ),
            usage_state=classify_usage_state(set(milestones)),
            last_login_at=r.last_login_at,
        )
