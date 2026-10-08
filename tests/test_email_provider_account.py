"""The platform's Lemlist account (spec 10.5): `GET /admin/email/provider`
and `POST /admin/email/provider/webhook`, superadmin only
(`platform.task_manage`, a platform permission no agency role holds).

- Without real effects, the GET answers `dry_run` and asks Lemlist NOTHING;
  the POST is 409 `email.provider_unavailable`.
- With them, the GET reads the team, the plan, the mailboxes and the
  webhook (cached 60 s) and names the bound agency; the key is shown as its
  sha256[:12], never itself.
- The POST creates the webhook for EMAIL_WEBHOOK_URL with no type, once:
  an existing one is returned untouched. 409 `email.webhook_not_configured`
  without the URL or the secret. The secret is never returned nor logged.

Lemlist answers through `FakeLemlist` (an `httpx.MockTransport`): no socket.
"""

import hashlib
import logging
from collections.abc import Generator

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.email_campaigns import email_admin_manager
from src.email_campaigns.providers.lemlist import LemlistEmailProvider
from tests.email_fakes import FakeLemlist, lemlist_double
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.email_plugin import (
    FAKE_LEMLIST_KEY,
    TEAM_A,
    LemlistTeamDouble,
    MakeAgencyEmailModule,
    email_env,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")

GET = "/admin/email/provider"
POST = "/admin/email/provider/webhook"
HOOK_URL = "https://api.nidria.test/webhooks/lemlist"
FINGERPRINT = hashlib.sha256(FAKE_LEMLIST_KEY.encode()).hexdigest()[:12]
SECRET = "whsec-never-shown-1234"


@pytest.fixture(autouse=True)
def fresh_cache() -> Generator[None, None, None]:
    email_admin_manager.clear_account_cache()
    yield
    email_admin_manager.clear_account_cache()


@pytest.fixture
def lemlist(monkeypatch: pytest.MonkeyPatch) -> FakeLemlist:
    """The account routes' adapter on a simulated Lemlist."""
    fake, adapter = lemlist_double(monkeypatch)

    def account(_settings: object) -> LemlistEmailProvider:
        return adapter

    monkeypatch.setattr(email_admin_manager, "lemlist_account", account)
    return fake


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"])


@pytest_asyncio.fixture
async def root(superadmin: Agent, agent_headers: AuthHeaders) -> dict[str, str]:
    return agent_headers(superadmin)


def _account(fake: FakeLemlist, *, hooks: list[dict[str, object]] | None = None) -> None:
    """What Lemlist answers about the team, its plan, mailboxes and hooks."""
    team = {"_id": TEAM_A, "name": "Nidria test team", "userIds": ["usr_1"]}
    channels = {"plan": "emailPro", "email": {"accounts": [{"email": "hello@nidria.test"}]}}
    fake.queue("GET", "/team", 200, team)  # read_team
    fake.queue("GET", "/user/channels", 200, channels)  # read_plan
    fake.queue("GET", "/team/senders", 200, [])  # nobody sends a campaign yet
    fake.queue("GET", "/team", 200, team)  # the team's users
    fake.queue("GET", "/user/channels", 200, channels)  # the key user's mailboxes
    fake.queue("GET", "/hooks", 200, hooks or [])


# ── reserved to the superadmin ───────────────────────────────────────────────


@pytest.mark.parametrize("role_name", ["admin", "case_manager", "member", "viewer"])
async def test_every_agency_role_is_refused(
    client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    lemlist: FakeLemlist,
    role_name: str,
) -> None:
    headers = agent_headers(await make_agent(role=system_roles[role_name]))
    for method, path in (("GET", GET), ("POST", POST)):
        response = await client.request(method, path, headers=headers)
        assert response.status_code == 403, (role_name, method, response.text)
    assert lemlist.calls == []


async def test_a_token_is_required(client: AsyncClient, lemlist: FakeLemlist) -> None:
    for method, path in (("GET", GET), ("POST", POST)):
        assert (await client.request(method, path)).status_code == 401
    assert lemlist.calls == []


# ── without real effects: nothing asked ──────────────────────────────────────


async def test_without_real_effects_nothing_is_asked_of_lemlist(
    client: AsyncClient,
    root: dict[str, str],
    lemlist: FakeLemlist,
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    answer = await client.get(GET, headers=root)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["dry_run"] is True
    assert (body["external_effects"], body["mocked"], body["key_configured"]) == (
        False,
        True,
        False,
    )
    assert (body["team"], body["plan"], body["senders"], body["webhook"]) == (None, None, [], None)
    # A key set, effects still off: its fingerprint, never the key; no call.
    with email_env(LEMLIST_API_KEY=FAKE_LEMLIST_KEY):
        keyed = (await client.get(GET, headers=root)).json()
        assert keyed["dry_run"] is True and keyed["key_configured"] is True
        assert keyed["key_fingerprint"] == FINGERPRINT
        refused = await client.post(POST, headers=root)
    assert refused.status_code == 409
    assert (refused.json()["code"], refused.json()["params"]) == (
        "email.provider_unavailable",
        {"reason": "not_configured"},
    )
    assert lemlist.calls == [] and lemlist_team_double.requests == []


# ── with real effects ────────────────────────────────────────────────────────


async def test_the_account_is_read_cached_and_the_key_never_shown(
    client: AsyncClient,
    root: dict[str, str],
    lemlist: FakeLemlist,
    lemlist_live: LemlistTeamDouble,
    make_agency: MakeAgency,
    make_agency_email_module: MakeAgencyEmailModule,
) -> None:
    agency: Agency = await make_agency(name="Relocation Lisboa")
    await make_agency_email_module(agency_id=agency.id, provider_team_id=TEAM_A)
    _account(lemlist, hooks=[{"_id": "hoo_1", "targetUrl": HOOK_URL, "type": "emailsSent"}])
    with email_env(EMAIL_WEBHOOK_URL=HOOK_URL):
        answer = await client.get(GET, headers=root)
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["dry_run"] is False
        assert (body["external_effects"], body["mocked"], body["key_configured"]) == (
            True,
            False,
            True,
        )
        assert body["key_fingerprint"] == FINGERPRINT
        assert FAKE_LEMLIST_KEY not in answer.text
        assert body["team"] == {"id": TEAM_A, "name": "Nidria test team"}
        assert body["plan"] == "emailPro"
        assert body["senders"] == [
            {"user_id": "usr_1", "email": "hello@nidria.test", "campaigns": 0}
        ]
        assert body["webhook"] == {
            "configured_url": HOOK_URL,
            "exists": True,
            "hook_id": "hoo_1",
            "has_type": True,  # limited to one type: Nidria wants every event
        }
        assert body["bound_agency"] == {"id": str(agency.id), "name": "Relocation Lisboa"}
        calls = len(lemlist.calls)
        # Cached 60 s: the next read asks Lemlist nothing.
        again = await client.get(GET, headers=root)
        assert again.json()["team"] == body["team"]
        assert len(lemlist.calls) == calls
    assert all(method == "GET" for method, _, _, _ in lemlist.calls)


async def test_a_lemlist_refusal_is_a_coded_409(
    client: AsyncClient,
    root: dict[str, str],
    lemlist: FakeLemlist,
    lemlist_live: LemlistTeamDouble,
) -> None:
    lemlist.queue("GET", "/team", 401, {"message": "Invalid API key"})
    answer = await client.get(GET, headers=root)
    assert answer.status_code == 409
    assert answer.json()["code"] == "email.provider_error"
    assert FAKE_LEMLIST_KEY not in answer.text


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"EMAIL_WEBHOOK_SECRET": SECRET}, "url"),
        ({"EMAIL_WEBHOOK_URL": HOOK_URL}, "secret"),
        ({}, "both"),
    ],
)
async def test_the_webhook_needs_its_url_and_its_secret(
    client: AsyncClient,
    root: dict[str, str],
    lemlist: FakeLemlist,
    lemlist_live: LemlistTeamDouble,
    env: dict[str, str],
    missing: str,
) -> None:
    with email_env(**env):
        refused = await client.post(POST, headers=root)
    assert refused.status_code == 409
    assert (refused.json()["code"], refused.json()["params"]) == (
        "email.webhook_not_configured",
        {"missing": missing},
    )
    assert lemlist.calls == []


async def test_the_webhook_is_created_once_without_a_type_and_its_secret_never_shown(
    client: AsyncClient,
    root: dict[str, str],
    lemlist: FakeLemlist,
    lemlist_live: LemlistTeamDouble,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    lemlist.queue("GET", "/hooks", 200, [{"_id": "hoo_other", "targetUrl": "https://else"}])
    lemlist.queue("POST", "/hooks", 200, {"_id": "hoo_1", "targetUrl": HOOK_URL})
    with email_env(EMAIL_WEBHOOK_URL=HOOK_URL, EMAIL_WEBHOOK_SECRET=SECRET):
        created = await client.post(POST, headers=root)
        assert created.status_code == 200, created.text
        assert created.json() == {
            "created": True,
            "webhook": {"hook_id": "hoo_1", "target_url": HOOK_URL, "has_type": False},
        }
        # Lemlist got the URL and the secret, and no type: every event.
        [post] = [c for c in lemlist.calls if c[0] == "POST"]
        assert post[1:] == ("/hooks", {}, {"targetUrl": HOOK_URL, "secret": SECRET})

        # Idempotent: it exists now, it is returned, nothing is created again.
        lemlist.queue("GET", "/hooks", 200, [{"_id": "hoo_1", "targetUrl": HOOK_URL}])
        again = await client.post(POST, headers=root)
        assert again.json() == {
            "created": False,
            "webhook": {"hook_id": "hoo_1", "target_url": HOOK_URL, "has_type": False},
        }
    assert [c[0] for c in lemlist.calls].count("POST") == 1
    for text in (created.text, again.text, caplog.text):
        assert SECRET not in text and FAKE_LEMLIST_KEY not in text


async def test_a_refused_creation_never_carries_the_secret(
    client: AsyncClient,
    root: dict[str, str],
    lemlist: FakeLemlist,
    lemlist_live: LemlistTeamDouble,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    lemlist.queue("GET", "/hooks", 200, [])
    lemlist.queue("POST", "/hooks", 400, {"message": f"bad secret {SECRET}"})
    with email_env(EMAIL_WEBHOOK_URL=HOOK_URL, EMAIL_WEBHOOK_SECRET=SECRET):
        refused = await client.post(POST, headers=root)
    assert refused.status_code == 409
    assert refused.json()["code"] == "email.provider_error"
    assert SECRET not in refused.text and SECRET not in caplog.text
