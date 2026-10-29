"""scripts/email_admin.py, the Email module's administration script (lot D).

Every command acts as the agent `--as` names, through the routes' managers,
after `authorize_agent` (enforce()'s AGENT face without a Request) on each
route it declares. Proven here:
- the production guard: ENVIRONMENT=production without FLY_APP_NAME, a Fly
  machine that is not `nidria-api`, and the local run;
- the rights: an agency admin reaches no platform command, an agent without
  `email.sync` no import, and a refusal stops a command before it writes.
  The `--as` agent is the platform's superadmin, or a role holding the
  email.* keys explicitly: never an agency admin supposed to hold them (the
  rights lot takes them from the admin role);
- the output: never an @, whatever went in (every run is checked by
  `script()`); an @ about to be printed ends in an internal error, and
  nothing but the document reaches stdout or stderr;
- `webhook-create` idempotent, `import-apply` refused with conflicts,
  `import-verify` explaining each gap, a smoke test cleaned up of what it
  created only, its mark kept by the reconciliation, `job-run` on a disabled
  job (E1), the audit of the superadmin role flaw;
- `authorize_agent` itself: the binding, the external agent, the
  impersonation denials, the consent gate, the billing lock, the permission.

Lemlist is `FakeProvider` (the manager's provider) or `FakeLemlist` (the
platform account's adapter, an httpx.MockTransport): no socket.
"""

import argparse
import io
import json
import logging
import uuid
import warnings
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, selectinload, sessionmaker

from scripts import email_admin
from shared.models.agency import Agency
from shared.models.agency_email_module import AgencyEmailModule
from shared.models.agent import Agent
from shared.models.client_profile import ClientProfile
from shared.models.consent import ConsentDocument
from shared.models.email_campaign import ORIGIN_LEMLIST_IMPORT, EmailCampaign
from shared.models.impersonation import ImpersonationLog
from shared.models.job import JobConfig
from shared.models.rbac import Role
from src.core.config import Settings, get_settings
from src.core.enums import Audience, ConsentDocumentType
from src.core.exceptions import ForbiddenError
from src.core.rbac.baseline import collect_bindings
from src.core.rbac.enforcement import authorize_agent
from src.core.rbac.permissions import Permission
from src.email_campaigns import email_admin_manager
from src.email_campaigns.email_manager import SMOKE_TEST_METADATA_KEY, EmailManager
from src.email_campaigns.email_schema import EmailProviderAccountRead, EmailTeamInfo
from src.email_campaigns.email_sync import EmailSyncManager
from src.email_campaigns.providers.base import ProviderCampaign, ProviderSender
from src.email_campaigns.providers.dry_run import is_dry_run_id
from src.email_campaigns.providers.lemlist import LemlistEmailProvider
from src.jobs.jobs_baseline import seed_job_configs
from src.main import app
from tests.email_fakes import FakeLemlist, FakeProvider, lemlist_double
from tests.plugins.agency_plugin import MakeAgency, MakeAgentInvitation
from tests.plugins.agent_plugin import MakeAgent
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    TEAM_A,
    EmailWorld,
    LemlistTeamDouble,
    MakeCompanyLink,
    MakeEmailCampaign,
    MakeEmailMember,
    email_env,
)
from tests.plugins.rbac_plugin import MakeRole
from tests.test_email_import import LEMLIST_A, LEMLIST_B, team_scenario

pytestmark = pytest.mark.usefixtures("rbac_baseline")

#: The `--as` account: stored lower-cased, as every account address is.
ROOT_EMAIL = "Root.Operator@Nidria-Ops.example"
HOOK_URL = "https://api.nidria.test/webhooks/lemlist"
SECRET = "whsec-never-shown-5678"
#: Real-looking addresses given to the smoke test, never printed back.
A = "Recette.A@Example.org"
B = "recette.b@example.org"
#: Every key a role may hold for the Email module, but `email.sync`.
EMAIL_WITHOUT_SYNC = (
    Permission.EMAIL_VIEW,
    Permission.EMAIL_MANAGE,
    Permission.EMAIL_ENROLL,
    Permission.EMAIL_PAUSE,
    Permission.EMAIL_REMOVE,
    Permission.CASE_VIEW,
    Permission.CASE_EDIT,
)


# ── harness ──────────────────────────────────────────────────────────────────


@dataclass
class Ran:
    code: int
    doc: dict[str, Any]
    text: str


async def script(
    sessions: async_sessionmaker[AsyncSession],
    *argv: str,
    sync_sessions: sessionmaker[Session] | None = None,
    environ: dict[str, str] | None = None,
) -> Ran:
    """One run of the script, its document parsed. Whatever the command and
    whatever went in, the output never carries an @."""
    out = io.StringIO()
    code = await email_admin.run(
        list(argv),
        out=out,
        sessions=sessions,
        sync_sessions=sync_sessions,
        environ={} if environ is None else environ,
    )
    text = out.getvalue()
    assert "@" not in text, text
    return Ran(code, json.loads(text), text)


@pytest.fixture
def sessions(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """The script's sessions on the test database (its own engine is a
    placeholder here), as the API's: commits keep the objects."""
    return async_sessionmaker(bind=db_session.bind, expire_on_commit=False)


@pytest.fixture
def lemlist(monkeypatch: pytest.MonkeyPatch) -> Generator[FakeLemlist, None, None]:
    """The platform account's adapter on a simulated Lemlist."""
    fake, adapter = lemlist_double(monkeypatch)

    def account(_settings: object) -> LemlistEmailProvider:
        return adapter

    monkeypatch.setattr(email_admin_manager, "lemlist_account", account)
    email_admin_manager.clear_account_cache()
    yield fake
    email_admin_manager.clear_account_cache()


@pytest_asyncio.fixture
async def root(
    email_world: EmailWorld, make_agent: MakeAgent, system_roles: dict[str, Role]
) -> Agent:
    """The platform's superadmin, in the pilot agency (Alexandre's place)."""
    return await make_agent(
        agency_id=email_world.agency_id, role=system_roles["superadmin"], email=ROOT_EMAIL.lower()
    )


async def count(db: AsyncSession, model: type[Any], *where: Any) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def account_answers(fake: FakeLemlist, hooks: list[dict[str, object]]) -> None:
    """What Lemlist answers about the team, its plan, mailboxes and hooks."""
    team = {"_id": TEAM_A, "name": "Nidria test team", "userIds": ["usr_1"]}
    channels = {"plan": "emailPro", "email": {"accounts": [{"email": "hello@nidria.test"}]}}
    fake.queue("GET", "/team", 200, team)  # read_team
    fake.queue("GET", "/user/channels", 200, channels)  # read_plan
    fake.queue("GET", "/team/senders", 200, [])  # nobody sends a campaign yet
    fake.queue("GET", "/team", 200, team)  # the team's users
    fake.queue("GET", "/user/channels", 200, channels)  # the key user's mailboxes
    fake.queue("GET", "/hooks", 200, hooks)


# ── what the script declares ─────────────────────────────────────────────────


def test_each_command_declares_bound_agent_routes() -> None:
    assert set(email_admin.COMMANDS) == {
        "status",
        "webhook-create",
        "agency-enable",
        "bind-team",
        "import-plan",
        "import-apply",
        "import-verify",
        "smoke-start",
        "smoke-status",
        "smoke-cleanup",
        "job-enable",
        "job-run",
        "platform-roles-audit",
    }
    declared = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    bindings = {(binding.method, binding.path): binding for binding in collect_bindings()}
    every_option = argparse.Namespace(sync=True)
    for command in email_admin.COMMANDS.values():
        routes = command.routes(every_option)
        for route in routes:
            assert (route.method, route.path) in declared, (command.name, route)
            assert bindings[(route.method, route.path)].audience == Audience.AGENT
        # Only the audit stands for no route: it is the platform's superadmin's.
        assert bool(routes) == (command.name != "platform-roles-audit"), command.name


@pytest.mark.parametrize("hour, minute", [(0, 5), (10, 7), (14, 0), (23, 58)])
def test_the_smoke_window_contains_now_within_the_day(hour: int, minute: int) -> None:
    now = datetime(2026, 10, 8, hour, minute, tzinfo=UTC)
    start, end = email_admin.sending_window(now)
    assert start <= f"{hour:02d}:{minute:02d}" <= end
    assert "00:00" <= start < end <= "23:59"


# ── the production guard ─────────────────────────────────────────────────────


def test_the_production_guard_decides_where_the_script_runs() -> None:
    guard = email_admin.production_guard
    # Production without a Fly app name, or on another app: refused.
    assert guard("production", None) == "fly_app_name_missing"
    assert guard("production", "") == "fly_app_name_missing"
    assert guard("production", "nidria-api-staging") == "fly_app_name_mismatch"
    # Any Fly machine but nidria-api, whatever the environment.
    assert guard("development", "nidria-front") == "fly_app_name_mismatch"
    # The production machine, and the local run.
    assert guard("production", "nidria-api") is None
    assert guard("development", None) is None
    assert guard("test", None) is None


async def test_the_guard_refuses_before_any_read(
    sessions: async_sessionmaker[AsyncSession],
    root: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unknown account would be refused too: the guard answers first.
    unknown = "nobody.at.all@nidria-ops.example"
    monkeypatch.setattr(get_settings(), "environment", "production")
    missing = await script(sessions, "--as", unknown, "status")
    assert (missing.code, missing.doc["code"], missing.doc["params"]) == (
        2,
        "script.production_guard",
        {"reason": "fly_app_name_missing"},
    )
    assert missing.doc["actor"] is None
    other = await script(
        sessions, "--as", unknown, "status", environ={"FLY_APP_NAME": "nidria-api-staging"}
    )
    assert (other.code, other.doc["params"]) == (2, {"reason": "fly_app_name_mismatch"})
    # On nidria-api, it runs (status without real effects: no Lemlist call).
    machine = {"FLY_APP_NAME": "nidria-api"}
    ran = await script(sessions, "--as", ROOT_EMAIL, "status", environ=machine)
    assert (ran.code, ran.doc["ok"], ran.doc["result"]["dry_run"]) == (0, True, True)
    # Locally (no FLY_APP_NAME, not production), it runs.
    monkeypatch.setattr(get_settings(), "environment", "development")
    local = await script(sessions, "--as", ROOT_EMAIL, "status")
    assert local.code == 0
    # A Fly machine that is not nidria-api is refused even out of production.
    refused = await script(
        sessions, "--as", ROOT_EMAIL, "status", environ={"FLY_APP_NAME": "nidria-front"}
    )
    assert (refused.code, refused.doc["code"]) == (2, "script.production_guard")


# ── the output ───────────────────────────────────────────────────────────────


def test_settings_that_refuse_to_load_are_named_never_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Loaded first, before the database module reads them at import: when
    they refuse to load, pydantic's own report carries every value it was
    given (a key, here); the script prints the fault's place only."""

    def refused() -> Settings:
        return Settings(
            _env_file=None,
            environment="production",
            bcrypt_rounds=4,
            lemlist_api_key="lemlist-secret-123",
        )

    with pytest.raises(PydanticValidationError) as raw:
        refused()
    assert "lemlist-secret-123" in str(raw.value)
    monkeypatch.setattr(email_admin, "get_settings", refused)
    with pytest.raises(SystemExit) as stopped:
        email_admin._settings_or_exit()
    assert stopped.value.code == 2
    printed = capsys.readouterr()
    assert printed.err == ""
    assert json.loads(printed.out) == {
        "ok": False,
        "command": None,
        "code": "script.settings_invalid",
        "params": {"fields": ["settings"], "type": "ValidationError"},
    }
    assert "lemlist-secret-123" not in printed.out


def test_the_output_filter_refuses_an_address() -> None:
    with pytest.raises(email_admin.OutputRefused) as found:
        email_admin.render({"a": {"b": ["fine", "claire@example.org"]}})
    assert found.value.where == "$.a.b[1]"
    with pytest.raises(email_admin.OutputRefused) as keyed:
        email_admin.render({"by_status": {"someone@example.org": 1}})
    assert keyed.value.where == "$.by_status.?"
    assert "@" not in str(found.value) and "@" not in str(keyed.value)
    text = email_admin.render({"id": uuid.UUID(int=1), "at": datetime(2026, 10, 8, tzinfo=UTC)})
    assert json.loads(text)["id"] == str(uuid.UUID(int=1))
    # A refusal's params known to carry an address are replaced.
    assert email_admin.safe_params(
        {"mailbox": "box@nidria.test", "sending_from": ["x@y.z"], "email": "c@d.e", "code": "X"}
    ) == {"mailbox": "<redacted>", "sending_from": "<redacted>", "email": "<redacted>", "code": "X"}


async def test_an_address_about_to_be_printed_ends_in_an_internal_error(
    sessions: async_sessionmaker[AsyncSession],
    root: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def leaky(_self: object) -> EmailProviderAccountRead:
        return EmailProviderAccountRead(
            dry_run=False,
            external_effects=True,
            mocked=False,
            key_configured=True,
            key_fingerprint="0123456789ab",
            team=EmailTeamInfo(id="tea_x", name="owner@example.org's Team"),
        )

    monkeypatch.setattr(email_admin_manager.EmailAdminManager, "provider_account", leaky)
    ran = await script(sessions, "--as", ROOT_EMAIL, "status")
    assert ran.code == 3
    assert ran.doc == {
        "ok": False,
        "command": "status",
        "code": "script.output_refused",
        "params": {"field": "$.result.team.name"},
    }
    assert "owner" not in ran.text


async def test_nothing_but_the_document_reaches_stdout_or_stderr(
    sessions: async_sessionmaker[AsyncSession],
    root: Agent,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def noisy(_self: object) -> EmailProviderAccountRead:
        logging.getLogger("src.email_campaigns").error("lookup of %s", "someone@example.org")
        warnings.warn("someone@example.org", UserWarning, stacklevel=1)
        return EmailProviderAccountRead(
            dry_run=True, external_effects=False, mocked=True, key_configured=False
        )

    monkeypatch.setattr(email_admin_manager.EmailAdminManager, "provider_account", noisy)
    ran = await script(sessions, "--as", ROOT_EMAIL, "status")
    assert ran.code == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    assert "someone" not in caplog.text
    # The log switch is given back.
    assert logging.root.manager.disable == logging.NOTSET


# ── the --as agent ───────────────────────────────────────────────────────────


async def test_the_agent_is_found_by_address_and_must_be_active_and_internal(
    sessions: async_sessionmaker[AsyncSession],
    root: Agent,
    email_world: EmailWorld,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    upper = await script(sessions, "--as", ROOT_EMAIL.upper(), "status")
    assert upper.code == 0
    assert upper.doc["actor"]["agent_id"] == str(root.id)
    assert upper.doc["actor"]["agency"]["id"] == str(email_world.agency_id)

    unknown = await script(sessions, "--as", "nobody.at.all@nidria-ops.example", "status")
    assert (unknown.code, unknown.doc["code"]) == (2, "script.agent_not_found")
    await make_agent(
        agency_id=email_world.agency_id,
        role=system_roles["superadmin"],
        email="gone.operator@nidria-ops.example",
        deactivated_at=datetime.now(UTC),
    )
    gone = await script(sessions, "--as", "gone.operator@nidria-ops.example", "status")
    assert (gone.code, gone.doc["code"]) == (2, "script.agent_inactive")
    await make_agent(
        agency_id=email_world.agency_id,
        role=system_roles["external_lawyer"],
        email="lawyer@cabinet.example",
        is_external=True,
    )
    external = await script(sessions, "--as", "lawyer@cabinet.example", "status")
    assert (external.code, external.doc["code"]) == (2, "script.agent_external")


async def test_usage_errors_never_quote_an_address(
    sessions: async_sessionmaker[AsyncSession], root: Agent
) -> None:
    quoted = await script(
        sessions, "--as", ROOT_EMAIL, "smoke-status", "--campaign", "not-an-id@example.org"
    )
    assert (quoted.code, quoted.doc["code"], quoted.doc["params"]) == (
        2,
        "script.usage",
        {"message": "invalid arguments"},
    )
    nonsense = await script(sessions, "--as", ROOT_EMAIL, "nonsense")
    assert (nonsense.code, nonsense.doc["code"]) == (2, "script.usage")
    bad = await script(sessions, "--as", ROOT_EMAIL, "smoke-start", "--to", "not-an-address")
    assert (bad.code, bad.doc["code"], bad.doc["params"]) == (
        2,
        "script.invalid_email",
        {"to_index": 1},
    )
    twice = await script(sessions, "--as", ROOT_EMAIL, "smoke-start", "--to", A, "--to", A.lower())
    assert (twice.code, twice.doc["code"], twice.doc["params"]) == (
        2,
        "script.duplicate_recipient",
        {"to_index": 2},
    )
    many = ("--to", "one@example.org", "--to", "two@example.org", "--to", "three@example.org")
    three = await script(sessions, "--as", ROOT_EMAIL, "smoke-start", *many)
    assert (three.code, three.doc["code"]) == (2, "script.too_many_recipients")
    async with sessions() as db:
        assert await count(db, EmailCampaign) == 0


# ── rights ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("argv", "permission"),
    [
        (["status"], "platform.task_manage"),
        (["webhook-create"], "platform.task_manage"),
        (["agency-enable", "--own-agency"], "agency.create"),
        (["bind-team", "--own-agency"], "agency.create"),
        (["job-enable"], "job.manage"),
        (["job-run"], "job.manage"),
        (["platform-roles-audit"], "agency.create"),
    ],
)
async def test_an_agency_admin_reaches_no_platform_command(
    sessions: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    lemlist: FakeLemlist,
    lemlist_team_double: LemlistTeamDouble,
    argv: list[str],
    permission: str,
) -> None:
    await seed_job_configs(db_session)
    await make_agent(
        agency_id=email_world.agency_id,
        role=system_roles["admin"],
        email="agency.admin@relocation.example",
    )
    ran = await script(sessions, "--as", "agency.admin@relocation.example", *argv)
    assert (ran.code, ran.doc["code"], ran.doc["params"]) == (
        1,
        "permission.denied",
        {"permission": permission},
    )
    assert ran.doc["result"] is None
    # Nothing asked of Lemlist, nothing written: the agency's module as the
    # platform left it, the job as seeded.
    assert lemlist.calls == [] and lemlist_team_double.requests == []
    async with sessions() as db:
        module = await db.get(AgencyEmailModule, email_world.agency_id)
        assert module is not None
        assert (module.provider_team_id, module.changed_by_agent_id) == (None, None)
        job = await db.scalar(select(JobConfig).where(JobConfig.job_id == "email_sync"))
        assert job is not None and job.is_enabled is False
        assert job.last_run_at is None


async def test_without_email_sync_no_import_and_no_smoke_test(
    sessions: async_sessionmaker[AsyncSession],
    email_world: EmailWorld,
    make_agent: MakeAgent,
    make_role: MakeRole,
    fake_email_provider: FakeProvider,
) -> None:
    role = await make_role(permissions=EMAIL_WITHOUT_SYNC, agency_id=email_world.agency_id)
    await make_agent(agency_id=email_world.agency_id, role=role, email="editor@relocation.example")
    for argv in (
        ["import-plan"],
        ["import-apply"],
        ["import-verify"],
        ["smoke-start", "--to", A],
    ):
        ran = await script(sessions, "--as", "editor@relocation.example", *argv)
        assert (ran.code, ran.doc["code"], ran.doc["params"]) == (
            1,
            "permission.denied",
            {"permission": "email.sync"},
        ), argv
    # Refused before the first call: nothing asked of Lemlist, nothing made.
    assert fake_email_provider.ops == []
    async with sessions() as db:
        assert await count(db, EmailCampaign) == 0
        assert await count(db, ClientProfile) == 0


# ── status, webhook-create ───────────────────────────────────────────────────


async def test_status_names_the_mailboxes_by_id_and_the_key_by_its_fingerprint(
    sessions: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    root: Agent,
    email_world: EmailWorld,
    lemlist: FakeLemlist,
    lemlist_live: LemlistTeamDouble,
) -> None:
    module = await db_session.get(AgencyEmailModule, email_world.agency_id)
    assert module is not None
    module.provider_team_id = TEAM_A
    await db_session.commit()
    pilot = await db_session.get(Agency, email_world.agency_id)
    assert pilot is not None
    account_answers(lemlist, [{"_id": "hoo_1", "targetUrl": HOOK_URL}])
    with email_env(EMAIL_WEBHOOK_URL=HOOK_URL):
        ran = await script(sessions, "--as", ROOT_EMAIL, "status")
    assert ran.code == 0, ran.doc
    result = ran.doc["result"]
    assert (result["dry_run"], result["external_effects"], result["lemlist_mocked"]) == (
        False,
        True,
        False,
    )
    assert result["key_configured"] is True and len(result["key_fingerprint"]) == 12
    assert result["team"] == {"id": TEAM_A, "name": "Nidria test team"}
    assert result["plan"] == "emailPro"
    assert result["senders"] == [{"user_id": "usr_1", "campaigns": 0}]
    assert result["webhook"] == {
        "configured_url": HOOK_URL,
        "exists": True,
        "hook_id": "hoo_1",
        "has_type": False,
    }
    assert result["bound_agency"] == {
        "id": str(pilot.id),
        "slug": pilot.slug,
        "name": pilot.name,
    }
    assert "test-lemlist-key" not in ran.text
    assert all(method == "GET" for method, _, _, _ in lemlist.calls)


async def test_webhook_create_is_idempotent_and_recreates_on_demand(
    sessions: async_sessionmaker[AsyncSession],
    root: Agent,
    lemlist: FakeLemlist,
    lemlist_live: LemlistTeamDouble,
) -> None:
    lemlist.queue("GET", "/hooks", 200, [])
    lemlist.queue("POST", "/hooks", 200, {"_id": "hoo_1", "targetUrl": HOOK_URL})
    with email_env(EMAIL_WEBHOOK_URL=HOOK_URL, EMAIL_WEBHOOK_SECRET=SECRET):
        first = await script(sessions, "--as", ROOT_EMAIL, "webhook-create")
        lemlist.queue("GET", "/hooks", 200, [{"_id": "hoo_1", "targetUrl": HOOK_URL}])
        second = await script(sessions, "--as", ROOT_EMAIL, "webhook-create")
        lemlist.queue("GET", "/hooks", 200, [{"_id": "hoo_1", "targetUrl": HOOK_URL}])
        lemlist.queue("DELETE", "/hooks/hoo_1", 200, {"_id": "hoo_1"})
        lemlist.queue("POST", "/hooks", 200, {"_id": "hoo_2", "targetUrl": HOOK_URL})
        rotated = await script(sessions, "--as", ROOT_EMAIL, "webhook-create", "--recreate")
    assert first.code == second.code == rotated.code == 0
    assert first.doc["result"] == {
        "hook_id": "hoo_1",
        "created": True,
        "recreated": False,
        "has_type": False,
        "target_url": HOOK_URL,
    }
    # Idempotent: the same hook, nothing created again.
    assert second.doc["result"]["hook_id"] == "hoo_1"
    assert (second.doc["result"]["created"], second.doc["result"]["recreated"]) == (False, False)
    assert (rotated.doc["result"]["hook_id"], rotated.doc["result"]["recreated"]) == ("hoo_2", True)
    assert [method for method, _, _, _ in lemlist.calls].count("POST") == 2
    for ran in (first, second, rotated):
        assert SECRET not in ran.text


# ── agency-enable, bind-team ─────────────────────────────────────────────────


async def test_agency_enable_and_bind_team_print_the_agency_and_its_module(
    sessions: async_sessionmaker[AsyncSession],
    root: Agent,
    make_agency: MakeAgency,
    lemlist_live: LemlistTeamDouble,
) -> None:
    pilot = await make_agency(name="Pilot Relocation", slug="pilot-relocation")
    enabled = await script(
        sessions, "--as", ROOT_EMAIL, "agency-enable", "--agency", "pilot-relocation"
    )
    assert enabled.code == 0, enabled.doc
    agency = {"id": str(pilot.id), "slug": "pilot-relocation", "name": "Pilot Relocation"}
    assert enabled.doc["result"]["agency"] == agency
    module = enabled.doc["result"]["module"]
    assert (module["enabled"], module["team"]) == (True, None)
    assert module["changed_by_agent_id"] == str(root.id)

    bound = await script(sessions, "--as", ROOT_EMAIL, "bind-team", "--agency", "pilot-relocation")
    assert bound.code == 0, bound.doc
    assert bound.doc["result"]["agency"] == agency
    assert bound.doc["result"]["module"]["team"] == {"id": TEAM_A, "name": "Nidria test team"}
    assert bound.doc["result"]["module"]["provider_mode"] == "LIVE"

    # The team serves one agency: the --as agent's own is refused, by code.
    own = await script(sessions, "--as", ROOT_EMAIL, "bind-team", "--own-agency")
    assert (own.code, own.doc["code"]) == (1, "email.team_bound_elsewhere")
    assert own.doc["params"] == {"agency_id": str(pilot.id), "agency_name": "Pilot Relocation"}
    # Refused or not, the agency aimed at is named.
    assert own.doc["result"] == {"agency": own.doc["actor"]["agency"]}

    unknown = await script(sessions, "--as", ROOT_EMAIL, "agency-enable", "--agency", "nowhere")
    assert (unknown.code, unknown.doc["code"]) == (1, "agency.not_found")


# ── the import ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def lemlist_team(
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_company_link: MakeCompanyLink,
) -> FakeProvider:
    """Lot C's team: A (paused, Max live in a Nidria campaign: a conflict)
    and B (running, Ana also observed in A: both histories kept) importable."""
    await team_scenario(
        email_world,
        fake_email_provider,
        make_client_profile,
        make_email_campaign,
        make_email_member,
        make_company_link,
    )
    return fake_email_provider


async def test_import_blocks_native_conflicts_but_preserves_provider_histories(
    sessions: async_sessionmaker[AsyncSession], root: Agent, lemlist_team: FakeProvider
) -> None:
    plan = await script(sessions, "--as", ROOT_EMAIL, "import-plan")
    assert plan.code == 0, plan.doc
    campaigns = plan.doc["result"]["plan"]["campaigns"]
    assert [c["provider_campaign_id"] for c in campaigns] == [LEMLIST_A, LEMLIST_B]
    assert [c["name"] for c in campaigns] == ["2026-08 · Avocats · s2", "2026-09 · Avocats · s4"]
    assert (campaigns[0]["leads"]["conflicts"], campaigns[1]["leads"]["conflicts_in_plan"]) == (
        1,
        0,
    )
    for name in ("Lopez", "Moss", "Stone", "Twice", "acme"):
        assert name not in plan.text

    refused = await script(sessions, "--as", ROOT_EMAIL, "import-apply")
    assert (refused.code, refused.doc["code"], refused.doc["params"]) == (
        1,
        "script.import_conflicts",
        {"conflicts": 1},
    )
    assert refused.doc["result"]["conflicts"] == {LEMLIST_A: 1, LEMLIST_B: 0}
    named = await script(sessions, "--as", ROOT_EMAIL, "import-apply", "--campaign", LEMLIST_A)
    assert (named.code, named.doc["params"]) == (1, {"conflicts": 1})
    async with sessions() as db:
        assert await count(db, EmailCampaign, EmailCampaign.origin == ORIGIN_LEMLIST_IMPORT) == 0
    assert "read_activities" not in lemlist_team.names()

    # A provider-only overlap requires no bypass flag and duplicates no person.
    named = await script(sessions, "--as", ROOT_EMAIL, "import-apply", "--campaign", LEMLIST_B)
    assert named.code == 0, named.doc
    assert named.doc["result"]["campaigns"][0]["members_created"] == 3

    accepted = await script(sessions, "--as", ROOT_EMAIL, "import-apply", "--accept-conflicts")
    assert accepted.code == 0, accepted.doc
    rows = {row["provider_campaign_id"]: row for row in accepted.doc["result"]["campaigns"]}
    assert rows[LEMLIST_A]["ok"] and rows[LEMLIST_B]["ok"]
    assert (rows[LEMLIST_A]["members_created"], rows[LEMLIST_A]["conflicts"]) == (10, 1)
    assert rows[LEMLIST_B]["not_imported"] == {}
    async with sessions() as db:
        assert await count(db, EmailCampaign, EmailCampaign.origin == ORIGIN_LEMLIST_IMPORT) == 2


async def test_import_verify_sets_nidria_against_lemlist_and_explains_each_gap(
    sessions: async_sessionmaker[AsyncSession], root: Agent, lemlist_team: FakeProvider
) -> None:
    imported = await script(sessions, "--as", ROOT_EMAIL, "import-apply", "--accept-conflicts")
    assert imported.code == 0, imported.doc
    calls = len(lemlist_team.ops)
    ran = await script(sessions, "--as", ROOT_EMAIL, "import-verify")
    assert ran.code == 0, ran.doc
    # Lemlist read again, in reads only.
    assert {name for name, _ in lemlist_team.ops[calls:]} <= {
        "list_campaigns",
        "read_campaign",
        "read_sequences",
        "list_contacts",
        "export_leads",
        "lookup_contacts",
    }
    by_id = {row["provider_campaign_id"]: row for row in ran.doc["result"]["campaigns"]}
    a = by_id[LEMLIST_A]
    assert a["members"]["open_with_lead_id"] == 10
    assert a["members"]["total"] == a["members"]["open"] == 10
    assert a["events_by_type"] and a["messages"]["OUTBOUND"]["total"] > 0
    assert a["lemlist"]["read"] is True
    assert (a["lemlist"]["listed"], a["lemlist"]["without_email"]) == (16, 1)
    # The export misses one listed lead (identified by its contact): 16 in all.
    assert (a["lemlist"]["exported"], a["lemlist"]["total"], a["lemlist"]["not_listed"]) == (
        15,
        16,
        0,
    )
    assert sum(a["lemlist"]["by_status"].values()) == sum(a["lemlist"]["by_state"].values()) == 16
    assert a["gaps"] == {
        "lemlist_minus_open_memberships": 6,
        "set_aside_by_plan": 6,
        "set_aside_by_reason": {
            "conflict": 1,
            "duplicate_email": 1,
            "no_email": 1,
            "not_launched": 1,
            "skipped_at_provider": 1,
            "status_uncovered": 1,
        },
        "new_at_lemlist": 0,
        "open_memberships_not_at_lemlist": 0,
        "explained": True,
    }
    b = by_id[LEMLIST_B]
    assert b["gaps"]["explained"] is True
    assert b["gaps"]["set_aside_by_reason"] == {}
    assert ran.doc["result"]["importable_not_imported"] == []
    for name in ("Lopez", "Moss", "Stone", "Twice", "acme"):
        assert name not in ran.text


# ── the smoke test ───────────────────────────────────────────────────────────


async def test_a_smoke_test_runs_reads_and_cleans_up_only_what_it_created(
    sessions: async_sessionmaker[AsyncSession],
    root: Agent,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    known = await make_client_profile(
        agency_id=email_world.agency_id, first_name="Bea", last_name="Known", email=B
    )
    bystander = await make_client_profile(
        agency_id=email_world.agency_id, email="bystander@example.org"
    )
    started = await script(
        sessions,
        "--as",
        ROOT_EMAIL,
        "smoke-start",
        "--to",
        A,
        "--to",
        B,
        "--followup-days",
        "0",
        "--name",
        "RECETTE interne test",
    )
    assert started.code == 0, started.doc
    assert "recette.a" not in started.text.lower() and "recette.b" not in started.text.lower()
    result = started.doc["result"]
    campaign_id = uuid.UUID(result["campaign"]["id"])
    assert (result["campaign"]["status"], result["campaign"]["provider_mode"]) == ("ACTIVE", "LIVE")
    settings = result["settings"]
    assert (settings["sending_days"], settings["daily_limit"]) == ([1, 2, 3, 4, 5, 6, 7], 2)
    assert settings["sender_user_id"] == "usr_1"
    assert [(s["position"], s["delay_value"]) for s in result["steps"]] == [(1, 0), (2, 0)]
    assert all(s["provider_step_id"] for s in result["steps"])
    rows = {row["to_index"]: row for row in result["recipients"]}
    assert (rows[1]["origin"], rows[2]["origin"]) == ("created", "reused")
    assert rows[2]["client_profile_id"] == str(known.id)
    assert all(row["member_status"] == "EMAIL_QUEUED" for row in rows.values())
    assert all(row["provider_lead_id"] for row in rows.values())
    created_id = uuid.UUID(rows[1]["client_profile_id"])
    names = fake_email_provider.names()
    assert names.count("add_contact") == 2 and names.count("launch_contact") == 2

    async with sessions() as db:
        created = await db.get(ClientProfile, created_id)
        assert created is not None
        assert (created.first_name, created.last_name) == ("Recette", "interne")
        assert (created.email, created.source) == (A.lower(), "email_smoke_test")
        reused = await db.get(ClientProfile, known.id)
        assert reused is not None and (reused.first_name, reused.source) == ("Bea", None)
        campaign = await db.get(EmailCampaign, campaign_id)
        assert campaign is not None
        mark = campaign.provider_metadata[SMOKE_TEST_METADATA_KEY]
        assert mark["created_profile_ids"] == [str(created_id)]
        assert mark["reused_profile_ids"] == [str(known.id)]
        assert campaign.settings["sender"]["user_id"] == "usr_1"

    # The status reads Nidria only: nothing asked of Lemlist.
    asked = len(fake_email_provider.ops)
    status = await script(
        sessions, "--as", ROOT_EMAIL, "smoke-status", "--campaign", str(campaign_id)
    )
    assert status.code == 0, status.doc
    assert len(fake_email_provider.ops) == asked
    members = {m["client_profile_id"]: m for m in status.doc["result"]["members"]}
    assert members[str(created_id)]["origin"] == "created"
    assert members[str(known.id)]["origin"] == "reused"
    assert {m["status"] for m in members.values()} == {"EMAIL_QUEUED"}
    assert status.doc["result"]["campaign"]["provider_campaign_id"]
    # --sync runs the campaign's Sync (its events read at Lemlist).
    synced = await script(
        sessions, "--as", ROOT_EMAIL, "smoke-status", "--campaign", str(campaign_id), "--sync"
    )
    assert synced.code == 0, synced.doc
    assert synced.doc["result"]["sync"][0]["external"] is True
    assert "sync_campaign" in fake_email_provider.names()[asked:]

    cleaned = await script(
        sessions, "--as", ROOT_EMAIL, "smoke-cleanup", "--campaign", str(campaign_id)
    )
    assert cleaned.code == 0, cleaned.doc
    report = cleaned.doc["result"]
    assert [row["ok"] for row in report["members_removed"]] == [True, True]
    assert report["campaign"]["status"] == "ARCHIVED"
    assert report["created_profiles"] == [
        {"client_profile_id": str(created_id), "action": "deleted"}
    ]
    assert report["reused_profiles_kept"] == [str(known.id)]
    names = fake_email_provider.names()
    assert names.count("remove_contact") == 2 and "archive_campaign" in names
    async with sessions() as db:
        assert await db.get(ClientProfile, created_id) is None
        assert await db.get(ClientProfile, known.id) is not None
        assert await db.get(ClientProfile, bystander.id) is not None
        campaign = await db.get(EmailCampaign, campaign_id)
        assert campaign is not None and campaign.status == "ARCHIVED"
        assert campaign.provider_metadata[SMOKE_TEST_METADATA_KEY]["cleaned_at"]

    # Again: nothing left to do, nothing asked of Lemlist.
    asked = len(fake_email_provider.ops)
    again = await script(
        sessions, "--as", ROOT_EMAIL, "smoke-cleanup", "--campaign", str(campaign_id)
    )
    assert again.code == 0, again.doc
    assert again.doc["result"]["members_removed"] == []
    assert again.doc["result"]["created_profiles"] == [
        {"client_profile_id": str(created_id), "action": "already_deleted"}
    ]
    assert len(fake_email_provider.ops) == asked


async def test_without_lemlist_the_smoke_test_runs_in_dry_run(
    sessions: async_sessionmaker[AsyncSession], root: Agent
) -> None:
    """Locally, an agency that does not talk to Lemlist runs in dry run: the
    same smoke test, nobody to choose as a sender, nothing leaves Nidria (its
    ids are dry-run ones), and it is cleaned up the same way."""
    started = await script(
        sessions, "--as", ROOT_EMAIL, "smoke-start", "--to", A, "--to", B, "--followup-days", "0"
    )
    assert started.code == 0, started.doc
    result = started.doc["result"]
    assert result["campaign"]["provider_mode"] == "DRY_RUN"
    assert result["settings"]["sender_user_id"] is None
    rows = result["recipients"]
    assert [row["origin"] for row in rows] == ["created", "created"]
    assert {row["member_status"] for row in rows} == {"EMAIL_QUEUED"}
    assert all(is_dry_run_id(row["provider_lead_id"]) for row in rows)
    cleaned = await script(
        sessions, "--as", ROOT_EMAIL, "smoke-cleanup", "--campaign", result["campaign"]["id"]
    )
    assert cleaned.code == 0, cleaned.doc
    assert cleaned.doc["result"]["campaign"]["status"] == "ARCHIVED"
    assert [row["action"] for row in cleaned.doc["result"]["created_profiles"]] == [
        "deleted",
        "deleted",
    ]
    async with sessions() as db:
        assert await count(db, ClientProfile) == 0


async def test_the_smoke_commands_refuse_a_campaign_that_is_not_a_smoke_test(
    sessions: async_sessionmaker[AsyncSession],
    root: Agent,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
) -> None:
    campaign = await make_email_campaign(agency_id=email_world.agency_id, name="Real outreach")
    member = await make_email_member(campaign_id=campaign.id)
    for command in ("smoke-cleanup", "smoke-status"):
        ran = await script(sessions, "--as", ROOT_EMAIL, command, "--campaign", str(campaign.id))
        assert (ran.code, ran.doc["code"]) == (1, "script.not_a_smoke_campaign"), command
    elsewhere = await make_email_campaign(agency_id=email_world.other_agency_id)
    other = await script(
        sessions, "--as", ROOT_EMAIL, "smoke-cleanup", "--campaign", str(elsewhere.id)
    )
    assert (other.code, other.doc["code"]) == (1, "email.campaign_not_found")
    assert fake_email_provider.ops == []
    async with sessions() as db:
        kept = await db.get(EmailCampaign, campaign.id)
        assert kept is not None and kept.status == "DRAFT"
        assert await db.get(ClientProfile, member.client_profile_id) is not None


async def test_the_smoke_test_asks_which_mailbox_among_several(
    sessions: async_sessionmaker[AsyncSession],
    root: Agent,
    fake_email_provider: FakeProvider,
) -> None:
    fake_email_provider.senders = [
        ProviderSender("usr_1", "one@nidria.test", 0),
        ProviderSender("usr_2", "two@nidria.test", 1),
    ]
    asked = await script(sessions, "--as", ROOT_EMAIL, "smoke-start", "--to", A)
    assert (asked.code, asked.doc["code"], asked.doc["params"]) == (
        2,
        "script.sender_required",
        {"senders": ["usr_1", "usr_2"]},
    )
    unknown = await script(
        sessions, "--as", ROOT_EMAIL, "smoke-start", "--to", A, "--sender", "usr_9"
    )
    assert (unknown.code, unknown.doc["code"]) == (2, "script.sender_unknown")
    async with sessions() as db:
        assert await count(db, EmailCampaign) == 0
    chosen = await script(
        sessions, "--as", ROOT_EMAIL, "smoke-start", "--to", A, "--sender", "usr_2"
    )
    assert chosen.code == 0, chosen.doc
    assert chosen.doc["result"]["settings"]["sender_user_id"] == "usr_2"
    started = [kwargs for name, kwargs in fake_email_provider.ops if name == "start_campaign"]
    assert started and started[-1]["mailbox"] == "two@nidria.test"


async def test_the_smoke_mark_survives_the_reconciliation(
    sessions: async_sessionmaker[AsyncSession],
    root: Agent,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
) -> None:
    started = await script(sessions, "--as", ROOT_EMAIL, "smoke-start", "--to", A)
    assert started.code == 0, started.doc
    campaign_id = uuid.UUID(started.doc["result"]["campaign"]["id"])
    provider_id = started.doc["result"]["campaign"]["provider_campaign_id"]
    # Lemlist's own word on the campaign replaces what it said before, never
    # what Nidria noted (its team, the smoke test's mark).
    fake_email_provider.remote[provider_id] = ProviderCampaign(
        provider_id, exists=True, status="running", metadata={"status": "running"}
    )
    async with sessions() as session:
        await EmailSyncManager(session, EmailManager(session)).reconcile_campaign(
            campaign_id, email_world.agency_id, provider=fake_email_provider, key_team="tea_current"
        )
    async with sessions() as db:
        campaign = await db.get(EmailCampaign, campaign_id)
        assert campaign is not None
        assert campaign.provider_metadata["status"] == "running"
        assert campaign.provider_metadata["team_id"] == "tea_current"
        assert campaign.provider_metadata[SMOKE_TEST_METADATA_KEY]["created_profile_ids"]
    cleaned = await script(
        sessions, "--as", ROOT_EMAIL, "smoke-cleanup", "--campaign", str(campaign_id)
    )
    assert cleaned.code == 0, cleaned.doc
    assert [row["action"] for row in cleaned.doc["result"]["created_profiles"]] == ["deleted"]


# ── the job ──────────────────────────────────────────────────────────────────


async def test_job_run_on_a_disabled_job_is_skipped_until_job_enable(
    sessions: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    root: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_job_configs(db_session)
    skipped = await script(
        sessions, "--as", ROOT_EMAIL, "job-run", sync_sessions=sync_session_local
    )
    # E1: a manual run of a disabled job is SKIPPED, whoever triggers it.
    assert (skipped.code, skipped.doc["code"], skipped.doc["params"]) == (
        1,
        "script.job_skipped",
        {"is_enabled": False, "paused": False},
    )
    assert skipped.doc["result"]["run"]["status"] == "skipped"
    assert skipped.doc["result"]["run"]["stats"] == {}
    # Its log is never printed.
    assert "log_output" not in skipped.text and "disabled" not in skipped.text

    enabled = await script(sessions, "--as", ROOT_EMAIL, "job-enable")
    assert enabled.code == 0, enabled.doc
    assert enabled.doc["result"]["job"]["is_enabled"] is True
    assert enabled.doc["result"]["job"]["job_id"] == "email_sync"
    # SCHEDULER_ENABLED off (the tests'): no running scheduler takes it.
    assert enabled.doc["result"]["running_api_picks_up_within_seconds"] is None
    # On: the running API's next reconciliation, within a minute.
    monkeypatch.setattr(get_settings(), "scheduler_enabled", True)
    again = await script(sessions, "--as", ROOT_EMAIL, "job-enable")
    assert again.doc["result"]["running_api_picks_up_within_seconds"] == 60

    ran = await script(sessions, "--as", ROOT_EMAIL, "job-run", sync_sessions=sync_session_local)
    assert ran.code == 0, ran.doc
    run = ran.doc["result"]["run"]
    assert (run["status"], run["triggered_by"], run["error_type"]) == ("success", "manual", None)
    # Real effects are off in the tests: the pipeline's own skip, no call.
    assert run["stats"] == {"skipped": True, "reason": "dry_run"}
    assert "log_output" not in ran.text


async def test_the_auto_import_job_is_enabled_and_run_by_name(
    sessions: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    root: Agent,
) -> None:
    """`--job email_auto_import` (Alexandre, 11/10/2026): that job alone,
    email_sync left as it was; an unknown job is a usage refusal."""
    await seed_job_configs(db_session)
    enabled = await script(sessions, "--as", ROOT_EMAIL, "job-enable", "--job", "email_auto_import")
    assert enabled.code == 0, enabled.doc
    assert enabled.doc["result"]["job"]["job_id"] == "email_auto_import"
    assert enabled.doc["result"]["job"]["is_enabled"] is True
    assert enabled.doc["result"]["job"]["cron_expression"] == "5,35 * * * *"
    sync = await db_session.scalar(
        select(JobConfig)
        .where(JobConfig.job_id == "email_sync")
        .execution_options(populate_existing=True)
    )
    assert sync is not None and sync.is_enabled is False

    ran = await script(
        sessions,
        "--as",
        ROOT_EMAIL,
        "job-run",
        "--job",
        "email_auto_import",
        sync_sessions=sync_session_local,
    )
    assert ran.code == 0, ran.doc
    run = ran.doc["result"]["run"]
    assert (run["job_id"], run["status"]) == ("email_auto_import", "success")
    # Real effects are off in the tests: the pipeline's own skip, no call.
    assert run["stats"] == {"skipped": True, "reason": "dry_run"}

    unknown = await script(sessions, "--as", ROOT_EMAIL, "job-run", "--job", "trial_nurture")
    assert (unknown.code, unknown.doc["code"]) == (2, "script.usage")


# ── the audit of the superadmin role ─────────────────────────────────────────


async def test_the_platform_roles_audit_counts_what_the_flaw_left(
    sessions: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    root: Agent,
    email_world: EmailWorld,
    make_agent: MakeAgent,
    make_role: MakeRole,
    make_agent_invitation: MakeAgentInvitation,
    system_roles: dict[str, Role],
) -> None:
    superadmin = system_roles["superadmin"]
    clone = await make_role(
        name="Operations",
        agency_id=email_world.agency_id,
        cloned_from_role_id=superadmin.id,
        permissions=(Permission.AGENCY_CREATE, Permission.JOB_MANAGE),
    )
    holder = await make_agent(
        agency_id=email_world.agency_id, role=clone, email="holder@relocation.example"
    )
    await make_agent(
        agency_id=email_world.agency_id,
        role=clone,
        email="former@relocation.example",
        deactivated_at=datetime.now(UTC),
    )
    scheduler = await make_role(
        name="Scheduler", agency_id=email_world.agency_id, permissions=(Permission.JOB_MANAGE,)
    )
    await make_agent_invitation(
        agency_id=email_world.agency_id, role_id=clone.id, status="accepted"
    )
    await make_agent_invitation(
        agency_id=email_world.agency_id, role_id=superadmin.id, status="pending"
    )
    admin = await make_agent(
        agency_id=email_world.agency_id,
        role=system_roles["admin"],
        email="agency.admin@relocation.example",
    )
    later = datetime.now(UTC) + timedelta(minutes=30)
    db_session.add_all(
        [
            # An agency admin entering a superadmin's session: the flaw.
            ImpersonationLog(
                impersonator_agent_id=admin.id,
                target_type="agent",
                target_id=root.id,
                expires_at=later,
            ),
            # The operator acting as the clone's holder.
            ImpersonationLog(
                impersonator_agent_id=root.id,
                target_type="agent",
                target_id=holder.id,
                expires_at=later,
            ),
        ]
    )
    await db_session.commit()

    ran = await script(sessions, "--as", ROOT_EMAIL, "platform-roles-audit")
    assert ran.code == 0, ran.doc
    result = ran.doc["result"]
    assert result["platform_role_ids"] == [str(superadmin.id)]
    assert result["platform_role_holders"] == {"agents": 1, "active_agents": 1}
    [found] = result["clones"]
    assert (found["role_id"], found["agency_id"]) == (str(clone.id), str(email_world.agency_id))
    assert (found["agents"], found["active_agents"]) == (2, 1)
    granted = {row["role_id"]: row for row in result["roles_with_platform_permissions"]}
    assert set(granted) == {str(clone.id), str(scheduler.id)}
    assert granted[str(clone.id)]["permissions"] == ["agency.create", "job.manage"]
    assert result["invitations"] == {"platform_role": {"pending": 1}, "clones": {"accepted": 1}}
    impersonations = result["impersonations"]
    assert (impersonations["count"], impersonations["by_others"]) == (2, 1)
    by_target = {event["target_agent_id"]: event for event in impersonations["events"]}
    assert by_target[str(root.id)]["target_role"] == "platform_role"
    assert by_target[str(root.id)]["by_platform_operator"] is False
    assert by_target[str(holder.id)]["target_role"] == "clone"
    assert by_target[str(holder.id)]["by_platform_operator"] is True
    assert result["summary"] == {
        "clones": 1,
        "clone_holders": 2,
        "roles_with_platform_permissions": 2,
        "invitations_naming_platform_roles": 2,
        "impersonations_of_platform_role_holders": 2,
    }

    # The clone holds agency.create, yet it is not the platform role.
    refused = await script(sessions, "--as", "holder@relocation.example", "platform-roles-audit")
    assert (refused.code, refused.doc["code"], refused.doc["params"]) == (
        1,
        "permission.denied",
        {"permission": "agency.create"},
    )


# ── authorize_agent: enforce()'s AGENT face without a Request ────────────────


async def as_enforce_reads(db: AsyncSession, agent: Agent) -> Agent:
    """The agent as enforce() reads it: its role and permissions loaded."""
    found = await db.scalar(
        select(Agent)
        .where(Agent.id == agent.id)
        .options(selectinload(Agent.role).selectinload(Role.permissions))
        .execution_options(populate_existing=True)
    )
    assert found is not None
    return found


async def test_authorize_agent_runs_the_steps_of_enforce_without_a_request(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
) -> None:
    agency = await make_agency(name="Healthy")
    reader_role = await make_role(permissions=(Permission.CASE_VIEW,), agency_id=agency.id)
    reader = await as_enforce_reads(
        db_session, await make_agent(agency_id=agency.id, role=reader_role)
    )
    binding = await authorize_agent(db_session, reader, "GET", "/client-profiles")
    assert binding.permission is not None and binding.permission.key == "case.view"

    def refused(exc: pytest.ExceptionInfo[ForbiddenError]) -> tuple[str, dict[str, Any]]:
        return exc.value.code, exc.value.params

    # Deny by default: an unbound route, and a route of another audience.
    with pytest.raises(ForbiddenError) as unbound:
        await authorize_agent(db_session, reader, "GET", "/nowhere")
    assert refused(unbound) == ("forbidden", {})
    with pytest.raises(ForbiddenError) as public:
        await authorize_agent(db_session, reader, "POST", "/webhooks/lemlist")
    assert refused(public) == ("forbidden", {})
    # The binding's permission.
    with pytest.raises(ForbiddenError) as missing:
        await authorize_agent(db_session, reader, "POST", "/client-profiles")
    assert refused(missing) == ("permission.denied", {"permission": "case.edit"})

    # An external provider: its allowlist only, before any permission.
    lawyer = await as_enforce_reads(
        db_session,
        await make_agent(
            agency_id=agency.id, role=system_roles["external_lawyer"], is_external=True
        ),
    )
    with pytest.raises(ForbiddenError) as external:
        await authorize_agent(db_session, lawyer, "GET", "/client-profiles")
    assert refused(external) == ("permission.denied", {})
    await authorize_agent(db_session, lawyer, "GET", "/auth/agent/me")

    # The impersonation mask: its denied routes refused, the others pass.
    sender_role = await make_role(
        permissions=(Permission.EMAIL_SYNC, Permission.CASE_VIEW), agency_id=agency.id
    )
    sender = await as_enforce_reads(
        db_session, await make_agent(agency_id=agency.id, role=sender_role)
    )
    launch = "/email/campaigns/{campaign_id}/launch"
    await authorize_agent(db_session, sender, "POST", launch)
    with pytest.raises(ForbiddenError) as masked:
        await authorize_agent(db_session, sender, "POST", launch, impersonator_id=uuid.uuid4())
    assert refused(masked) == ("impersonation.denied", {})
    await authorize_agent(
        db_session, sender, "GET", "/client-profiles", impersonator_id=uuid.uuid4()
    )

    # The billing lock: a blocked agency reads, never writes.
    locked = await make_agency(name="Locked", trial_ends_at=datetime.now(UTC) - timedelta(days=1))
    editor_role = await make_role(
        permissions=(Permission.CASE_VIEW, Permission.CASE_EDIT), agency_id=locked.id
    )
    editor = await as_enforce_reads(
        db_session, await make_agent(agency_id=locked.id, role=editor_role)
    )
    await authorize_agent(db_session, editor, "GET", "/client-profiles")
    with pytest.raises(ForbiddenError) as blocked:
        await authorize_agent(db_session, editor, "POST", "/client-profiles")
    assert refused(blocked) == ("billing.subscription_required", {"reason": "trial_expired"})

    # The consent gate (the agency admin), skipped under the mask.
    db_session.add(
        ConsentDocument(
            type=ConsentDocumentType.AGENCY_TERMS.value,
            version=1,
            content_md="Terms",
            content_hash="0" * 64,
            published_at=datetime.now(UTC),
            is_active=True,
        )
    )
    await db_session.commit()
    admin = await as_enforce_reads(
        db_session, await make_agent(agency_id=agency.id, role=system_roles["admin"])
    )
    with pytest.raises(ForbiddenError) as consent:
        await authorize_agent(db_session, admin, "GET", "/client-profiles")
    assert consent.value.code == "consent.required"
    await authorize_agent(
        db_session, admin, "GET", "/client-profiles", impersonator_id=uuid.uuid4()
    )
