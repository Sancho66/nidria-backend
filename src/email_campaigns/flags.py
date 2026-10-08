"""The Email module's switches: is it on for an agency, does it talk to Lemlist.

Same pattern as `src/signatures/flags.py`: the env is the MASTER switch and
nothing bypasses it. Here the agency's own switch is NOT in `agency.settings`
(an agency admin rewrites that JSONB through PATCH /agencies/me) but in its
own table, `agency_email_module`, written by the platform only (N3).

Three conditions make a real Lemlist call possible (N5): real effects
(`EMAIL_PROVIDER_EXTERNAL_EFFECTS`), Lemlist not mocked (`MOCK_LEMLIST`, else
`MOCK_SERVICES`) and a key (`LEMLIST_API_KEY`). On top, an agency talks to
Lemlist only when it is bound to the team the key opens (N4); an unbound
agency runs in dry run.

`external_effects_on` has its one definition here, not in
`providers/registry.py` (Prism's home for it): the registry needs
`email_live_for` from this module, and `lemlist_team` needs
`external_effects_on`, so defining it in the registry would make a cycle. The
registry re-exports it, so Prism's `from ...providers.registry import
external_effects_on, provider_for` still reads.

The dry-run prefix has its one definition in `providers/dry_run.py`
(`DRY_RUN_PREFIX`, `is_dry_run_id`); this module reads it from there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import httpx

from shared.models.agency_email_module import AgencyEmailModule
from src.core.config import Settings, get_settings
from src.core.exceptions import ConflictError
from src.email_campaigns.providers.dry_run import is_dry_run_id

if TYPE_CHECKING:
    from src.email_campaigns.lemlist_team import LemlistTeam

#: What the module does for one agency right now. UNAVAILABLE: the agency is
#: bound, effects are on, and Lemlist does not say which team the key opens.
#: That is never turned into a silent dry run (decision A4).
ProviderMode = Literal["LIVE", "DRY_RUN", "UNAVAILABLE"]


def lemlist_mocked(settings: Settings) -> bool:
    """`MOCK_LEMLIST`, falling back on `MOCK_SERVICES` (the `mock_*` pattern)."""
    if settings.mock_lemlist is not None:
        return settings.mock_lemlist
    return settings.mock_services


def external_effects_on(settings: Settings) -> bool:
    """Real effects requested, Lemlist not mocked, and a key: all three."""
    return bool(
        settings.email_provider_external_effects
        and not lemlist_mocked(settings)
        and settings.lemlist_api_key
    )


def email_module_effectively_enabled(
    row: AgencyEmailModule | None, settings: Settings | None = None
) -> bool:
    """Both locks open: the env master switch AND the agency's row."""
    settings = settings or get_settings()
    return bool(settings.email_module_enabled and row is not None and row.enabled)


def require_email_module(
    row: AgencyEmailModule | None, settings: Settings | None = None
) -> AgencyEmailModule:
    """The guard every agent route of the module runs first: 409
    `email.module_disabled` while the module is off for the agency (same
    pattern as `kpi.disabled`). Returns the row when it is on."""
    if not email_module_effectively_enabled(row, settings):
        raise ConflictError(
            "The Email module is not enabled for this agency.", code="email.module_disabled"
        )
    assert row is not None  # effectively enabled implies a row
    return row


def is_real_provider_id(value: str | None) -> bool:
    """An identifier Lemlist handed out: present and not a dry-run one."""
    return bool(value) and not is_dry_run_id(value)


def _bound_key_team(
    row: AgencyEmailModule | None,
    settings: Settings,
    transport: httpx.BaseTransport | None,
) -> LemlistTeam | None:
    """The team the key opens, asked only when it decides something: module
    effective, effects on, agency bound. None otherwise, without any call.
    Raises `TeamUnknownError` when Lemlist does not say."""
    if not email_module_effectively_enabled(row, settings) or not external_effects_on(settings):
        return None
    if row is None or row.provider_team_id is None:
        return None  # an unbound agency runs in dry run (N4)
    # Imported here: lemlist_team reads external_effects_on from this module.
    from src.email_campaigns.lemlist_team import team_of_key

    return team_of_key(settings, transport=transport)


def email_live_for(
    row: AgencyEmailModule | None,
    settings: Settings | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """Whether this agency talks to Lemlist for real: the module is
    effectively on, effects are on, and the agency is bound to the very team
    the key opens. Raises `TeamUnknownError` when the agency is bound, effects
    are on and Lemlist does not answer: never a silent switch to dry run (A4).
    """
    settings = settings or get_settings()
    team = _bound_key_team(row, settings, transport)
    return team is not None and row is not None and team.id == row.provider_team_id


@dataclass(frozen=True)
class ProviderState:
    """The provider side of an agency, as the platform screen shows it."""

    mode: ProviderMode
    #: Bound, effects on, and the key now opens ANOTHER team: the agency's
    #: campaigns turn read-only (TEAM_MISMATCH) and new ones run dry.
    team_mismatch: bool


def provider_state_for(
    row: AgencyEmailModule | None,
    settings: Settings | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ProviderState:
    """`email_live_for`, readable without raising: an unknown team is
    UNAVAILABLE, a different team is a mismatch."""
    from src.email_campaigns.lemlist_team import TeamUnknownError

    settings = settings or get_settings()
    try:
        team = _bound_key_team(row, settings, transport)
    except TeamUnknownError:
        return ProviderState(mode="UNAVAILABLE", team_mismatch=False)
    if team is None or row is None:
        return ProviderState(mode="DRY_RUN", team_mismatch=False)
    if team.id == row.provider_team_id:
        return ProviderState(mode="LIVE", team_mismatch=False)
    return ProviderState(mode="DRY_RUN", team_mismatch=True)
