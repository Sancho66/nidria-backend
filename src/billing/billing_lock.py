"""The billing lock — READ-ONLY, never destructive (Alexandre's decision).

blocking_reason() is the ONE place the rule lives; everything else
(enforce()'s 4th stage, GET /agencies/me, the admin table tomorrow) calls
it. The discriminant is converted_at, NOT billing_mode: every trial agency
is billing_mode="manual" by default, so "manual is never blocked" really
means "a CONVERTED manual agency is never blocked" (Nicolas, Reside — the
relation is human, Eric decides).

Blocked = (not converted AND trial expired at J+0 — the read-only mode IS
the grace: nothing lost, everything visible, unlock is self-serve and
instant) OR (paddle AND past_due beyond the grace window) OR (paddle AND
canceled — Paddle poses it at the END of the paid period, the grace is
already in its definition).
"""

from datetime import datetime, timedelta

from shared.models.agency import Agency
from src.core.config import get_settings


def blocking_reason(agency: Agency, *, now: datetime) -> str | None:
    """The stable reason the agency is blocked, or None when it is not.
    Values: "trial_expired" | "past_due" | "canceled" — served to the front
    (banner wording) alongside the 403 code billing.subscription_required."""
    if agency.converted_at is None:
        # No conversion: the trial calendar decides. No calendar at all
        # (platform/demo agencies) = no deadline, never blocked.
        if agency.trial_ends_at is not None and agency.trial_ends_at <= now:
            return "trial_expired"
        return None
    if agency.billing_mode != "paddle":
        # Manually converted (Eric's PATCH): NEVER auto-blocked, whatever
        # the trial dates or the (absent) billing_status say.
        return None
    if agency.billing_status == "canceled":
        return "canceled"
    if agency.billing_status == "past_due" and agency.past_due_since is not None:
        grace = timedelta(days=get_settings().billing_past_due_grace_days)
        if now >= agency.past_due_since + grace:
            return "past_due"
    return None


def is_agency_blocked(agency: Agency, *, now: datetime) -> bool:
    return blocking_reason(agency, now=now) is not None
