"""Which provider the Email module talks to for one agency: the real one only
when everything asks for it.

Ported from Prism's `providers/registry.py` (reference commit 629a2b0), where
real Lemlist calls need effects on, Lemlist not mocked and a key (Prism: "the
real one only when asked twice"). Nidria adds the agency (rules N4, N5): the
Lemlist adapter is chosen only when `email_live_for(row)`, that is the module
is effectively on for the agency, real effects are possible, and the agency
is bound to the very team the platform key opens. Anything less is the
dry-run provider, which sends nothing anywhere.

Decision A4: a bound agency, effects on, and Lemlist not saying which team
the key opens (an outage of `GET /team`) is refused with 409
`email.provider_unavailable` (params `{"reason": "team_unknown"}`, the same
refusal as binding a team), never answered with a silent dry run: the
agency's real campaigns would otherwise be read as dry and its new ones
created in no team.

`provider_for` is synchronous (it may ask Lemlist, cached ten minutes): the
async manager calls it through `asyncio.to_thread`. Tests replace it by
monkeypatch with a double of the same signature, `(settings, row)`.

`external_effects_on` is defined once, in `flags.py` (see its docstring for
why), and re-exported here where Prism had it.
"""

from __future__ import annotations

from functools import partial

from shared.models.agency_email_module import AgencyEmailModule
from src.core.config import Settings
from src.core.exceptions import ConflictError
from src.email_campaigns.flags import email_live_for, external_effects_on
from src.email_campaigns.lemlist_team import key_team_id
from src.email_campaigns.providers.base import EmailProvider
from src.email_campaigns.providers.dry_run import DryRunEmailProvider
from src.email_campaigns.providers.lemlist import LemlistEmailProvider, TeamUnknownError

__all__ = ["external_effects_on", "provider_for"]


def provider_for(settings: Settings, row: AgencyEmailModule | None) -> EmailProvider:
    """The provider of the agency whose `agency_email_module` row is `row`.

    Lemlist, with the platform key and the cached team lookup, only when
    `email_live_for(row)`; otherwise the dry run. Raises 409
    `email.provider_unavailable` when the agency is bound, effects are on and
    Lemlist does not say which team the key opens (decision A4)."""
    try:
        live = email_live_for(row, settings)
    except TeamUnknownError:
        raise ConflictError(
            "Lemlist did not say which team the platform key opens.",
            code="email.provider_unavailable",
            params={"reason": "team_unknown"},
        ) from None
    if live:
        return LemlistEmailProvider(
            settings.lemlist_api_key, team_lookup=partial(key_team_id, settings)
        )
    return DryRunEmailProvider()
