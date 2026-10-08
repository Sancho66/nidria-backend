"""The Email module's switches and the Lemlist team lookup, without HTTP routes.

What is proven here:
- the two locks (env master AND the agency's row) and the 409 guard the
  agent routes will run first (`email.module_disabled`);
- the three conditions of a real Lemlist call (N5);
- `team_of_key`: no network at all while mocked or without effects (the
  network guard stays at zero), Basic auth with an empty login, a ten-minute
  cache per key, and "Lemlist did not say" as a distinct error, never cached
  and never mistaken for "another team" (decision A4); the key never logged;
- `email_live_for` / `provider_state_for`: unbound = dry run, same team =
  live, another team = mismatch, unknown team = raised / UNAVAILABLE.
"""

import base64
import logging
import uuid
from typing import Any

import httpx
import pytest

from shared.models.agency_email_module import AgencyEmailModule
from src.core.config import Settings, get_settings
from src.core.exceptions import ConflictError
from src.email_campaigns import lemlist_team
from src.email_campaigns.flags import (
    email_live_for,
    email_module_effectively_enabled,
    external_effects_on,
    is_real_provider_id,
    lemlist_mocked,
    provider_state_for,
    require_email_module,
)
from src.email_campaigns.lemlist_team import (
    TEAM_URL,
    LemlistTeam,
    TeamUnknownError,
    fingerprint,
    team_of_key,
)
from tests import network_guard
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
    data: dict[str, Any] = {"agency_id": uuid.uuid4(), "enabled": True, **overrides}
    return AgencyEmailModule(**data)


# --- the two locks ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("master", "row", "expected"),
    [
        (False, None, False),
        (False, {"enabled": True}, False),
        (True, None, False),
        (True, {"enabled": False}, False),
        (True, {"enabled": True}, True),
    ],
)
def test_the_module_needs_the_master_switch_and_the_agency_row(
    master: bool, row: dict[str, bool] | None, expected: bool
) -> None:
    settings = _settings(email_module_enabled=master)
    record = _row(**row) if row is not None else None
    assert email_module_effectively_enabled(record, settings) is expected


def test_the_guard_refuses_a_disabled_module_with_its_code() -> None:
    for settings, row in (
        (_settings(email_module_enabled=False), _row()),
        (_settings(email_module_enabled=True), None),
        (_settings(email_module_enabled=True), _row(enabled=False)),
    ):
        with pytest.raises(ConflictError) as exc:
            require_email_module(row, settings)
        assert exc.value.code == "email.module_disabled"
        assert exc.value.status_code == 409
        assert exc.value.params == {}
    enabled = _row()
    assert require_email_module(enabled, _settings(email_module_enabled=True)) is enabled


# --- the three conditions of a real call ----------------------------------------------


@pytest.mark.parametrize(
    ("effects", "mock_lemlist", "mock_services", "key", "expected"),
    [
        (True, False, True, FAKE_LEMLIST_KEY, True),
        (False, False, True, FAKE_LEMLIST_KEY, False),
        (True, True, False, FAKE_LEMLIST_KEY, False),
        (True, False, True, "", False),
        # MOCK_LEMLIST unset: it follows MOCK_SERVICES.
        (True, None, True, FAKE_LEMLIST_KEY, False),
        (True, None, False, FAKE_LEMLIST_KEY, True),
    ],
)
def test_external_effects_need_all_three_conditions(
    effects: bool, mock_lemlist: bool | None, mock_services: bool, key: str, expected: bool
) -> None:
    settings = _settings(
        email_provider_external_effects=effects,
        mock_lemlist=mock_lemlist,
        mock_services=mock_services,
        lemlist_api_key=key,
    )
    assert external_effects_on(settings) is expected


def test_the_test_suite_runs_with_lemlist_mocked_and_effects_off() -> None:
    settings = get_settings()
    assert lemlist_mocked(settings) is True
    assert external_effects_on(settings) is False
    assert settings.email_module_enabled is False


def test_a_real_provider_id_is_present_and_not_a_dry_run_one() -> None:
    assert is_real_provider_id("cam_8gtvSu") is True
    assert is_real_provider_id("dryXcampaign") is True  # `_` is literal, not a wildcard
    assert is_real_provider_id("dry_cam_1a2b") is False
    assert is_real_provider_id("") is False
    assert is_real_provider_id(None) is False


# --- team_of_key: no network while it would be pointless ------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {},  # the suite's settings: mocked, effects off, no key
        {"email_provider_external_effects": True, "lemlist_api_key": FAKE_LEMLIST_KEY},
        {"mock_lemlist": False, "lemlist_api_key": FAKE_LEMLIST_KEY},
        {"email_provider_external_effects": True, "mock_lemlist": False},
    ],
)
def test_no_team_and_no_call_unless_real_effects_are_possible(
    overrides: dict[str, Any], lemlist_team_double: LemlistTeamDouble
) -> None:
    violations = len(network_guard._VIOLATIONS)
    assert team_of_key(_settings(**overrides)) is None
    assert lemlist_team_double.requests == []
    assert len(network_guard._VIOLATIONS) == violations


def test_the_key_team_is_read_with_basic_auth_and_an_empty_login(
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    team = team_of_key(_live())
    assert team == LemlistTeam(id=TEAM_A, name="Nidria test team")
    (request,) = lemlist_team_double.requests
    assert request.method == "GET"
    assert str(request.url) == TEAM_URL
    expected = base64.b64encode(f":{FAKE_LEMLIST_KEY}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"


def test_a_team_without_a_name_is_still_a_team(lemlist_team_double: LemlistTeamDouble) -> None:
    lemlist_team_double.name = None
    assert team_of_key(_live()) == LemlistTeam(id=TEAM_A, name=None)


def test_the_team_is_cached_ten_minutes_per_key(
    lemlist_team_double: LemlistTeamDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(lemlist_team.time, "monotonic", lambda: clock[0])
    settings = _live()
    assert team_of_key(settings) is not None
    clock[0] += 599
    assert team_of_key(settings) is not None
    assert len(lemlist_team_double.requests) == 1  # served from the cache
    clock[0] += 2  # 601 s after the answer
    lemlist_team_double.team_id = TEAM_B
    team = team_of_key(settings)
    assert team is not None and team.id == TEAM_B
    assert len(lemlist_team_double.requests) == 2
    # Another key is another cache entry.
    assert team_of_key(_live(lemlist_api_key="another-fake-key")) is not None
    assert len(lemlist_team_double.requests) == 3


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"status_code": 500}, "http_500"),
        ({"status_code": 401}, "http_401"),
        ({"error": "connect"}, "network"),
        ({"error": "timeout"}, "network"),
        ({"team_id": ""}, "payload"),
    ],
)
def test_lemlist_not_saying_is_unknown_never_no_team(
    setup: dict[str, Any], reason: str, lemlist_team_double: LemlistTeamDouble
) -> None:
    error = setup.pop("error", None)
    if error == "connect":
        lemlist_team_double.error = httpx.ConnectError("simulated")
    elif error == "timeout":
        lemlist_team_double.error = httpx.ReadTimeout("simulated")
    for attr, value in setup.items():
        setattr(lemlist_team_double, attr, value)
    with pytest.raises(TeamUnknownError) as exc:
        team_of_key(_live())
    assert exc.value.reason == reason
    # A failure is not cached: the next call asks Lemlist again.
    with pytest.raises(TeamUnknownError):
        team_of_key(_live())
    assert len(lemlist_team_double.requests) == 2


def test_an_unreadable_body_is_unknown(lemlist_team_double: LemlistTeamDouble) -> None:
    lemlist_team_double.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"<html>maintenance</html>")
    )
    lemlist_team.transport_override = lemlist_team_double.transport
    with pytest.raises(TeamUnknownError) as exc:
        team_of_key(_live())
    assert exc.value.reason == "payload"


def test_the_key_is_never_logged(
    lemlist_team_double: LemlistTeamDouble, caplog: pytest.LogCaptureFixture
) -> None:
    lemlist_team_double.status_code = 503
    with (
        caplog.at_level(logging.WARNING, logger=lemlist_team.__name__),
        pytest.raises(TeamUnknownError),
    ):
        team_of_key(_live())
    assert caplog.records, "the failure must be logged"
    assert FAKE_LEMLIST_KEY not in caplog.text
    assert fingerprint(FAKE_LEMLIST_KEY) in caplog.text


# --- live or dry run for one agency ---------------------------------------------------------


def test_an_unbound_or_disabled_agency_is_dry_run_without_any_call(
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    assert email_live_for(_row(provider_team_id=None), _live()) is False
    assert email_live_for(_row(enabled=False, provider_team_id=TEAM_A), _live()) is False
    assert email_live_for(None, _live()) is False
    off = _live(email_module_enabled=False)
    assert email_live_for(_row(provider_team_id=TEAM_A), off) is False
    mocked = _live(mock_lemlist=True)
    assert email_live_for(_row(provider_team_id=TEAM_A), mocked) is False
    assert lemlist_team_double.requests == []


def test_a_bound_agency_is_live_only_on_the_key_team(
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    assert email_live_for(_row(provider_team_id=TEAM_A), _live()) is True
    assert email_live_for(_row(provider_team_id=TEAM_B), _live()) is False
    assert provider_state_for(_row(provider_team_id=TEAM_A), _live()).mode == "LIVE"
    mismatch = provider_state_for(_row(provider_team_id=TEAM_B), _live())
    assert (mismatch.mode, mismatch.team_mismatch) == ("DRY_RUN", True)


def test_an_unknown_team_is_raised_never_a_silent_dry_run(
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    lemlist_team_double.status_code = 502
    with pytest.raises(TeamUnknownError):
        email_live_for(_row(provider_team_id=TEAM_A), _live())
    state = provider_state_for(_row(provider_team_id=TEAM_A), _live())
    assert (state.mode, state.team_mismatch) == ("UNAVAILABLE", False)
