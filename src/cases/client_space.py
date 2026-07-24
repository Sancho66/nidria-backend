"""Can this person actually reach their client space? ONE derivation,
consumed by the API (the badge on the case detail and the listing) AND by
the SYNC notification jobs — which must not chase a client who has never
come in. A neutral module on purpose: a job importing `cases_manager`
would drag the whole async manager graph into the scheduler.
"""

from datetime import UTC, datetime

from shared.models.expat_user import ExpatUser
from src.core.enums import ClientSpaceState


def client_space_state(
    expat: ExpatUser | None, pending_until: dict[str, datetime]
) -> tuple[ClientSpaceState | None, datetime | None]:
    """(state, invitation expiry) for one person — DERIVED, never stored.

    `pending_until` maps email → the furthest PENDING invitation expiry of
    the case (cancelled/accepted rows are already out). A person whose link
    is past that date is EXPIRED, exactly like one with no invitation at
    all: in both cases the only way back in is a resend.
    """
    if expat is None:
        return None, None
    if expat.activated_at is not None:
        return ClientSpaceState.ACTIVE, None
    expires_at = pending_until.get(expat.email)
    if expires_at is not None and expires_at > datetime.now(UTC):
        return ClientSpaceState.PENDING, expires_at
    return ClientSpaceState.EXPIRED, expires_at


def client_space_is_active(expat: ExpatUser | None) -> bool:
    """THE exclusion predicate of the notification jobs: may we send this
    person somewhere they can actually go?

    Reads the ACTIVE branch of `client_space_state`, which depends on
    `activated_at` alone — hence the empty mapping (no invitation lookup is
    needed to answer this question, and a job has no reason to run one).
    Going through the shared function rather than re-testing `activated_at`
    is the point: the day ACTIVE means more than "activated", the badge and
    the jobs move together instead of drifting apart.
    """
    return client_space_state(expat, {})[0] is ClientSpaceState.ACTIVE
