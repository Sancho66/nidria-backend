"""Which provider an agency's Email module talks to (`providers/registry.py`).

Ported from Prism's `test_external_effects_are_off_unless_asked_twice`
(tests/test_email_rendering.py, reference 629a2b0), plus its Nidria
equivalent with the agency (rules N4, N5): Lemlist only for an agency whose
module is on and which is bound to the very team the key opens, the dry run
otherwise, and a refusal, never a silent dry run, when Lemlist does not say
which team the key opens (decision A4).

No network: `GET /team` is answered by the `lemlist_team_double` fixture
(httpx.MockTransport), the one adapter call made goes through a
MockTransport too, and the guard counts nothing.
"""

import base64
import uuid
from typing import Any

import httpx
import pytest

from shared.models.agency_email_module import AgencyEmailModule
from src.core.config import Settings, get_settings
from src.core.exceptions import ConflictError
from src.email_campaigns import flags, lemlist_team
from src.email_campaigns.lemlist_team import key_team_id
from src.email_campaigns.providers import lemlist as lemlist_module
from src.email_campaigns.providers import registry
from src.email_campaigns.providers.base import CampaignRef, ProviderError
from src.email_campaigns.providers.dry_run import DryRunEmailProvider
from src.email_campaigns.providers.lemlist import LemlistEmailProvider, TeamUnknownError
from src.email_campaigns.providers.registry import external_effects_on, provider_for
from tests.plugins.email_plugin import FAKE_LEMLIST_KEY, TEAM_A, TEAM_B, LemlistTeamDouble


def _settings(**overrides: Any) -> Settings:
    return get_settings().model_copy(update=overrides)


def _live(**overrides: Any) -> Settings:
    """Real effects on, Lemlist unmocked, a fake key, the module's master on."""
    values: dict[str, Any] = {
        "email_provider_external_effects": True,
        "mock_lemlist": False,
        "lemlist_api_key": FAKE_LEMLIST_KEY,
        "email_module_enabled": True,
        **overrides,
    }
    return _settings(**values)


def _row(**overrides: Any) -> AgencyEmailModule:
    """An agency whose module is on and bound to the team the key opens."""
    data: dict[str, Any] = {
        "agency_id": uuid.uuid4(),
        "enabled": True,
        "provider_team_id": TEAM_A,
        **overrides,
    }
    return AgencyEmailModule(**data)


def test_external_effects_are_off_unless_asked_twice(
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    bound = _row()
    # The suite's settings: Lemlist mocked, effects off, no key.
    assert isinstance(provider_for(get_settings(), bound), DryRunEmailProvider)
    flagged = _live(mock_lemlist=True)
    assert external_effects_on(flagged) is False  # Lemlist still mocked
    assert isinstance(provider_for(flagged, bound), DryRunEmailProvider)
    live = _live()
    assert external_effects_on(live) is True
    assert isinstance(provider_for(live, bound), LemlistEmailProvider)
    keyless = _live(lemlist_api_key="")
    assert isinstance(provider_for(keyless, bound), DryRunEmailProvider)
    off = _live(email_provider_external_effects=False)
    assert isinstance(provider_for(off, bound), DryRunEmailProvider)
    # Only the live settings asked Lemlist which team the key opens.
    assert len(lemlist_team_double.requests) == 1


def test_lemlist_only_for_an_agency_on_and_bound_to_the_key_team(
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    live = _live()
    # No row, the agency's switch off, the platform's master off, unbound:
    # the dry run, and Lemlist is not even asked (N4, N5).
    assert isinstance(provider_for(live, None), DryRunEmailProvider)
    assert isinstance(provider_for(live, _row(enabled=False)), DryRunEmailProvider)
    master_off = _live(email_module_enabled=False)
    assert isinstance(provider_for(master_off, _row()), DryRunEmailProvider)
    assert isinstance(provider_for(live, _row(provider_team_id=None)), DryRunEmailProvider)
    assert lemlist_team_double.requests == []
    # Bound to another team than the one the key opens: the dry run (its
    # real campaigns are held by TEAM_MISMATCH and EXTERNAL_EFFECTS_OFF).
    assert isinstance(provider_for(live, _row(provider_team_id=TEAM_B)), DryRunEmailProvider)
    # Bound to the key's team: Lemlist.
    assert isinstance(provider_for(live, _row()), LemlistEmailProvider)


@pytest.mark.parametrize("outage", ["http_502", "network", "payload"])
def test_an_unknown_team_is_refused_never_a_silent_dry_run(
    outage: str, lemlist_team_double: LemlistTeamDouble
) -> None:
    if outage == "http_502":
        lemlist_team_double.status_code = 502
    elif outage == "network":
        lemlist_team_double.error = httpx.ConnectError("simulated")
    else:
        lemlist_team_double.team_id = ""
    with pytest.raises(ConflictError) as refused:
        provider_for(_live(), _row())
    assert refused.value.code == "email.provider_unavailable"
    assert refused.value.status_code == 409
    assert refused.value.params == {"reason": "team_unknown"}
    # Unbound, or effects off: nothing to ask, the dry run is the rule.
    assert isinstance(provider_for(_live(), _row(provider_team_id=None)), DryRunEmailProvider)
    off = _live(email_provider_external_effects=False)
    assert isinstance(provider_for(off, _row()), DryRunEmailProvider)


def test_the_adapter_carries_the_key_and_the_cached_team(
    lemlist_team_double: LemlistTeamDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = provider_for(_live(), _row())
    assert isinstance(provider, LemlistEmailProvider)
    # Its team is the ten-minute cache's: no second `GET /team`.
    assert provider.team_id() == TEAM_A
    assert len(lemlist_team_double.requests) == 1
    # Its key is the settings' one: Basic auth with an empty login.
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"_id": "cam_1", "status": "running"})

    monkeypatch.setattr(lemlist_module, "_THROTTLE", lemlist_module._Throttle())
    monkeypatch.setattr(provider, "_transport", httpx.MockTransport(answer))
    assert provider.read_campaign(CampaignRef("cam_1")).exists
    expected = base64.b64encode(f":{FAKE_LEMLIST_KEY}".encode()).decode()
    assert seen == [f"Basic {expected}"]
    # The cache expired and Lemlist no longer answers: the adapter's team is
    # unknown, raised as a provider error (A4), never None.
    lemlist_team.clear_cache()
    lemlist_team_double.status_code = 503
    with pytest.raises(TeamUnknownError) as unknown:
        provider.team_id()
    assert isinstance(unknown.value, ProviderError)
    assert (unknown.value.code, unknown.value.reason) == ("TEAM_UNKNOWN", "http_503")


def test_the_key_team_id_is_a_team_or_an_error_never_none(
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    assert key_team_id(_live()) == TEAM_A
    # Effects off: no team to name, said as an error (no call is made).
    with pytest.raises(TeamUnknownError) as off:
        key_team_id(_settings())
    assert off.value.reason == "not_configured"
    assert len(lemlist_team_double.requests) == 1


def test_external_effects_have_one_definition() -> None:
    assert registry.external_effects_on is flags.external_effects_on
    assert lemlist_team.TeamUnknownError is TeamUnknownError
