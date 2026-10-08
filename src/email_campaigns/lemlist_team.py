"""Which Lemlist team the platform key opens (`GET /team`), cached ten minutes.

Ported from Prism's `engine/shared/lemlist_teams.py`, `team_of_key` only: the
team rotation (`PREVIOUS_TEAMS`) becomes the team <-> agency binding (N4) and
the TEAM_MISMATCH gate. Lemlist ids belong to a team: a campaign or a lead of
one team does not exist in another, so a campaign carries the team it was
created in (`provider_metadata[CAMPAIGN_TEAM_KEY]`).

Two departures from Prism, both deliberate:
- no call at all while real effects are off or Lemlist is mocked: then there
  is no team, and the caller is in dry run;
- "Lemlist did not say" (network error, HTTP error, unreadable body) is NOT
  "no team": it raises `TeamUnknownError` (decision A4), so a passing outage
  can never switch a bound agency to dry run in silence. Failures are not
  cached: the next call asks again.

One contract for the team, here and in the Lemlist adapter (`team_lookup`,
`team_id`): a team id when Lemlist names it, `TeamUnknownError` (a
`ProviderError`, code `TEAM_UNKNOWN`) when it does not, and None only where
nothing is asked at all (real effects off: the dry run has no team).
`key_team_id` is the adapter's `team_lookup`: the cached team's id, or the
error.

The key is never logged, printed nor kept in clear: the cache is indexed by
its sha256, logs name it by `sha256[:12]`.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass

import httpx

from src.core.config import Settings, get_settings
from src.email_campaigns.flags import external_effects_on
from src.email_campaigns.providers.lemlist import LEMLIST_API_BASE_URL
from src.email_campaigns.providers.lemlist import TeamUnknownError as TeamUnknownError

logger = logging.getLogger(__name__)

TEAM_URL = f"{LEMLIST_API_BASE_URL}/team"
#: Where an Email campaign notes its team, in its provider metadata.
CAMPAIGN_TEAM_KEY = "team_id"
TEAM_TTL_SECONDS = 600.0
_TIMEOUT = httpx.Timeout(10.0)

#: Test seam (the `override` pattern of the signature provider): an
#: `httpx.MockTransport` that answers instead of Lemlist. Production leaves it
#: None and httpx uses its real transport.
transport_override: httpx.BaseTransport | None = None


@dataclass(frozen=True)
class LemlistTeam:
    id: str
    name: str | None


#: sha256(key) -> (monotonic time of the answer, team). Successes only.
_teams: dict[str, tuple[float, LemlistTeam]] = {}
_lock = threading.Lock()


def fingerprint(api_key: str) -> str:
    """How a key is designated anywhere outside the settings: sha256[:12]."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


def clear_cache() -> None:
    """Test hook: forget every cached team."""
    with _lock:
        _teams.clear()


def team_of_key(
    settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None
) -> LemlistTeam | None:
    """The team the platform key opens, cached ten minutes per key.

    None, without any network call, while real effects are off, Lemlist is
    mocked or there is no key. Raises `TeamUnknownError` when Lemlist is
    asked and does not say. Synchronous, like the provider it serves (the
    async routes call it through `asyncio.to_thread`)."""
    settings = settings or get_settings()
    if not external_effects_on(settings):
        return None
    api_key = settings.lemlist_api_key
    digest = hashlib.sha256(api_key.encode()).hexdigest()
    with _lock:
        hit = _teams.get(digest)
    if hit is not None and time.monotonic() - hit[0] < TEAM_TTL_SECONDS:
        return hit[1]
    team = _fetch_team(api_key, transport or transport_override)
    with _lock:
        _teams[digest] = (time.monotonic(), team)
    return team


def key_team_id(
    settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None
) -> str:
    """The id of the team the platform key opens, for a caller that talks to
    Lemlist for real (the adapter's `team_lookup`, wired by the registry).
    Raises `TeamUnknownError` when Lemlist does not say, and also when real
    effects are off: such a caller then has no team to stamp, which is never
    turned into a silent None."""
    team = team_of_key(settings, transport=transport)
    if team is None:
        raise TeamUnknownError("not_configured")
    return team.id


def _fetch_team(api_key: str, transport: httpx.BaseTransport | None) -> LemlistTeam:
    key_ref = fingerprint(api_key)
    try:
        with httpx.Client(
            timeout=_TIMEOUT, auth=httpx.BasicAuth("", api_key), transport=transport
        ) as client:
            response = client.get(TEAM_URL)
    except httpx.HTTPError as exc:
        # The class name only: an httpx message may carry the request.
        logger.warning("lemlist team lookup failed (key %s): %s", key_ref, type(exc).__name__)
        raise TeamUnknownError("network") from None
    if response.status_code != 200:
        logger.warning(
            "lemlist team lookup failed (key %s): HTTP %s", key_ref, response.status_code
        )
        raise TeamUnknownError(f"http_{response.status_code}")
    try:
        body = response.json()
    except ValueError:
        body = None
    team_id = body.get("_id") if isinstance(body, dict) else None
    if not isinstance(team_id, str) or not team_id:
        logger.warning("lemlist team lookup failed (key %s): unreadable body", key_ref)
        raise TeamUnknownError("payload")
    assert isinstance(body, dict)
    name = body.get("name")
    return LemlistTeam(id=team_id, name=name if isinstance(name, str) and name else None)
