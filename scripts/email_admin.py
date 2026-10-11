"""Administration of the Email module from inside the API machine: the only
door Claude Code uses in production, and the local smoke test's tool.

    python scripts/email_admin.py --as <email> <command> [options]

In production, through `fly ssh console -a nidria-api -C "..."` and nothing
else: no SQL by hand, no command that prints the environment.

How it acts. Every command acts as the agent `--as` names (found by address
whatever its case; unknown, deactivated or external: refused), through the
managers of the routes, with the routes' arguments and the routes' rights.
Each command declares the routes it stands for (method and path template,
`Route`), and each call runs as the API serves a request: a fresh session,
the agent read again, `authorize_agent` (enforcement.py: the route's
binding, an external agent refused, consent, the billing lock on a write,
the binding's permission; no token, hence no impersonation), then the
route's manager, whose own checks (`access()`, `require(...)`) apply as for
a route. Every route of a command is authorized before its first call: a
refusal stops it before anything is written. `platform-roles-audit` stands
for no route: it needs the platform's superadmin role and `agency.create`.

What it prints. One JSON document on stdout and nothing else (log lines,
warnings and the SQL echo are silenced): `ok`, `command`, `actor` (the
agent's id and agency), `result`, and `code` and `params` when it fell
short. Ids, counts, codes, statuses, fingerprints, campaign and agency
names: never an address, a person's name nor a secret. Every printed value
goes through a filter that refuses a string carrying an @: the run then
ends in an internal error rather than a leak. Exit codes: 0 done; 1 refused
by a rule (its code printed); 2 usage or guard; 3 internal error (a bug, or
an @ the filter stopped).

Production guard. With ENVIRONMENT=production, or on any Fly machine
(FLY_APP_NAME set), it runs only when FLY_APP_NAME is `nidria-api`, else
exit 2 (`script.production_guard`, with its reason only). Locally (no
FLY_APP_NAME, another environment), it runs. Settings that refuse to load
are named, never printed (exit 2, `script.settings_invalid`): pydantic's
own report would show the values it was given.

Going live, in this order (spec 15, corrected by E1):
1. `status`: the key's fingerprint and the team must be the ones checked
   locally;
2. `webhook-create`;
3. `agency-enable --own-agency` (or `--agency <slug>`), then `bind-team`,
   for the pilot agency: the Nidria agency of the `--as` account;
4. `import-plan`, then `import-apply` (refused while the plan counts
   conflicts, unless `--accept-conflicts`);
5. `import-verify`;
6. a short smoke test with A only: `smoke-start --to <A>`, then
   `smoke-status` until it is sent; reply from A: the reply must come in
   through the webhook alone, without the job (`smoke-status` reads
   nothing at Lemlist);
7. `job-enable`, then `job-run`: no event counted twice
   (`duplicate_events`), the imported campaigns followed. E1: the PATCH
   `{"is_enabled": true}` switches the job on, never `resume` (which only
   lifts a pause), and a manual run of a disabled job is SKIPPED, hence
   this order. The running API's scheduler (when SCHEDULER_ENABLED) reads
   `job_config` again every minute: the cron of `email_sync` starts within
   60 s, without a restart (`running_api_picks_up_within_seconds`); `job-run`
   runs it at once;
8. `smoke-cleanup`.
Locally, the smoke test also runs with A and B and a follow-up at J+0
(`--followup-days 0`), and `smoke-status --sync` reads Lemlist without a
webhook. Its emails carry no unsubscribe link: Nidria has no token for one
yet (spec 9.4).

What each command may change:
- `status`: nothing (Lemlist read: team, plan, mailboxes, hooks; cached
  60 s);
- `webhook-create [--recreate]`: the team's webhook at Lemlist, created
  once (idempotent), or deleted and created again (`--recreate`: the
  secret's rotation);
- `agency-enable`: the agency's Email switch, on;
- `bind-team`: the agency's Lemlist team (the one the key opens);
- `import-plan`, `import-verify`: nothing (Lemlist read in GET only);
- `import-apply [--campaign <Lemlist id> ...] [--accept-conflicts]`: the
  imported campaigns (all the importable ones without `--campaign`), their
  steps, memberships, events and messages, the client records the import
  creates (source `lemlist`) and the opposition list; Lemlist read in GET
  only;
- `smoke-start --to <email> [--to <email>] [--name <name>]
  [--followup-days N] [--url <url>] [--sender <user id>]`: a smoke test
  campaign in the agent's agency, with its mark (`provider_metadata`, key
  `smoke_test`: the client records it created), its steps, a client record
  "Recette interne" (source `email_smoke_test`) for each address the agency
  does not know (an existing record of that address is reused, never
  changed), the memberships; at Lemlist the campaign, its own schedule,
  its steps, its sender, the leads, their emails and the launch: REAL
  emails to the `--to` addresses. Every day, a window around now in the
  campaign's timezone (Nidria knows no agency timezone: the campaigns'
  default, Europe/Paris), daily limit 2; the sender is the team's only one,
  else `--sender`; `--url` defaults to Nidria's public home page;
- `smoke-status --campaign <id> [--sync]`: nothing, and nothing read at
  Lemlist; `--sync` runs the campaign's Sync, as the app's button does: its
  events read, its campaign, schedule and steps sent again as they stand
  (idempotent), its running leads' next emails refreshed, nobody launched;
- `smoke-cleanup --campaign <id>`: its memberships removed (the leads
  deleted at Lemlist), the campaign archived (paused at Lemlist), then the
  client records the smoke test created deleted, never a reused one;
  refused for a campaign without the mark;
- `job-enable [--job email_sync|email_auto_import]`: that job's
  `job_config.is_enabled` (`email_sync` by default), taken by the running
  API's scheduler within a minute;
- `job-run [--job …]`: one run of that job (a `job_run` row; `email_sync`
  reads Lemlist only, `email_auto_import` imports the campaigns Nidria does
  not know yet, Alexandre's decision of 11/10/2026); its `log_output`, which
  may name recipients, is never printed;
- `platform-roles-audit`: nothing (whether the flaw of the superadmin role,
  closed on 08/10/2026, was used: its clones and their holders, the roles
  still holding a platform permission, the invitations and the logged
  impersonations naming it).
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
import warnings
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple, NoReturn, TextIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError as PydanticValidationError  # noqa: E402

from src.core.config import get_settings  # noqa: E402


def _settings_or_exit() -> None:
    """The settings, loaded before anything reads them at import (the
    database module does): when they refuse to load, the names of the
    fields at fault and nothing else, exit 2. pydantic's own report would
    print the values it was given, secrets included."""
    try:
        get_settings()
    except Exception as exc:  # noqa: BLE001 (the boundary: names, never values)
        errors = exc.errors() if isinstance(exc, PydanticValidationError) else []
        fields = sorted(
            {".".join(map(str, error.get("loc", ()))) or "settings" for error in errors}
        )
        document = {
            "ok": False,
            "command": None,
            "code": "script.settings_invalid",
            "params": {"fields": fields, "type": type(exc).__name__},
        }
        text = json.dumps(document, indent=2)
        sys.stdout.write((text if "@" not in text else '{"ok": false}') + "\n")
        raise SystemExit(2) from None


_settings_or_exit()

from pydantic import TypeAdapter  # noqa: E402
from sqlalchemy import Select, func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402
from sqlalchemy.orm import Session, aliased, selectinload, sessionmaker  # noqa: E402

from shared.models.agency import Agency  # noqa: E402
from shared.models.agent import Agent  # noqa: E402
from shared.models.email_campaign import (  # noqa: E402
    ORIGIN_LEMLIST_IMPORT,
    EmailCampaign,
    EmailCampaignMember,
    EmailMessage,
    EmailProviderEvent,
)
from shared.models.impersonation import ImpersonationLog  # noqa: E402
from shared.models.invitation import AgentInvitation  # noqa: E402
from shared.models.job import JobConfig  # noqa: E402
from shared.models.rbac import Permission as PermissionRow  # noqa: E402
from shared.models.rbac import Role, RolePermission  # noqa: E402
from src.client_profiles.client_profiles_manager import (  # noqa: E402
    ClientProfilesManager,
    ProfileSeed,
    seed_client_profiles,
)
from src.client_profiles.client_profiles_repository import ClientProfilesRepository  # noqa: E402
from src.core.database import async_session_maker, engine  # noqa: E402
from src.core.email import NormalizedEmailStr, normalize_email  # noqa: E402
from src.core.enums import ActorType  # noqa: E402
from src.core.exceptions import ForbiddenError, NidriaError  # noqa: E402
from src.core.rbac.baseline import PLATFORM_PERMISSION_KEYS, PLATFORM_ROLE_NAMES  # noqa: E402
from src.core.rbac.delegation import is_platform_role  # noqa: E402
from src.core.rbac.enforcement import authorize_agent, effective_permissions  # noqa: E402
from src.core.rbac.permissions import Permission  # noqa: E402
from src.core.scheduler import RECONCILE_INTERVAL_SECONDS, make_session_local  # noqa: E402
from src.email_campaigns.email_admin_manager import EmailAdminManager  # noqa: E402
from src.email_campaigns.email_import import EmailImportManager  # noqa: E402
from src.email_campaigns.email_manager import (  # noqa: E402
    SMOKE_TEST_METADATA_KEY,
    EmailManager,
    campaign_provider_mode,
    import_metadata,
)
from src.email_campaigns.email_repository import EmailRepository  # noqa: E402
from src.email_campaigns.email_schema import (  # noqa: E402
    AgencyEmailModuleResponse,
    AgencyEmailModuleUpdateRequest,
    EmailActionResult,
    EmailAddMembersRequest,
    EmailCampaignCreate,
    EmailCampaignDetailRead,
    EmailCampaignSettings,
    EmailCampaignUpdate,
    EmailImportPlanRead,
    EmailImportRequest,
    EmailLaunchRequest,
    EmailLaunchResult,
    EmailSender,
    EmailSendersRead,
    EmailStepCreate,
    EmailSyncRequest,
    EmailSyncResult,
)
from src.email_campaigns.email_sync import EmailSyncManager  # noqa: E402
from src.email_campaigns.import_job import JOB_ID as AUTO_IMPORT_JOB_ID  # noqa: E402
from src.email_campaigns.sync_job import JOB_ID as SYNC_JOB_ID  # noqa: E402
from src.jobs.jobs_manager import JobsManager  # noqa: E402
from src.jobs.jobs_schema import (  # noqa: E402
    JobConfigResponse,
    JobConfigUpdateRequest,
    JobTriggerRequest,
)

#: Exit codes (see the module docstring).
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3

#: The one Fly app the script runs on in production.
PRODUCTION_APP = "nidria-api"
#: Params of a refusal known to carry an address (a client record's, a
#: mailbox Lemlist names): replaced before printing; the filter stays behind.
ADDRESS_PARAMS = frozenset({"email", "mailbox", "sending_from"})
REDACTED = "<redacted>"

#: The smoke test's client records: their name, and their `source` (the CRM
#: block), which `smoke-cleanup` checks again before deleting one.
SMOKE_FIRST_NAME = "Recette"
SMOKE_LAST_NAME = "interne"
SMOKE_SOURCE = "email_smoke_test"
#: Nidria's public home page: no account and no client data behind it.
SMOKE_URL = "https://nidria.com/"
SMOKE_DAILY_LIMIT = 2
SMOKE_MAX_RECIPIENTS = 2
SMOKE_DAYS = [1, 2, 3, 4, 5, 6, 7]
SMOKE_SUBJECT = "Recette Nidria pour {{client.first_name}}"
SMOKE_FOLLOWUP_BODY = (
    "Bonjour {{client.first_name}},\n\nCeci est la relance de recette de {{agency.name}}.\n"
)
#: Calls of the Sync, or of the Launch, one command makes at most.
MAX_ROUNDS = 5
#: Memberships removed per call (the route's own limit).
REMOVE_BATCH = 1000


def smoke_body(url: str) -> str:
    """The first email of the smoke test (plain text, as an agent writes it)."""
    return (
        "Bonjour {{client.first_name}},\n\n"
        "Ceci est un email de recette interne de {{agency.name}}, envoyé par le module "
        "Email de Nidria.\n\n"
        "Lien de contrôle : " + url + "\n\n"
        "Merci de répondre à cet email : la réponse doit remonter dans Nidria.\n"
    )


# ── output ──────────────────────────────────────────────────────────────────


class OutputRefused(Exception):
    """A value about to be printed carries an @: never printed. `where` is
    its place in the document, by keys and ranks only."""

    def __init__(self, where: str) -> None:
        super().__init__(where)
        self.where = where


def _jsonable(value: object) -> str:
    """The only values the script prints beyond JSON's own: ids and times."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"Not printable: {type(value).__name__}")


def _find_address(value: object, where: str) -> None:
    """Raise OutputRefused at the first key or string carrying an @."""
    if isinstance(value, str):
        if "@" in value:
            raise OutputRefused(where)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if "@" in name:
                raise OutputRefused(f"{where}.?")
            _find_address(item, f"{where}.{name}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _find_address(item, f"{where}[{index}]")


def render(document: Mapping[str, Any]) -> str:
    """The document as printed: indented JSON, every value through the
    filter that refuses an @ (an internal error rather than a leak)."""
    _find_address(document, "$")
    # ASCII: printable whatever the terminal's encoding (Lemlist's campaign
    # names carry a "·"); an @ stays an @, so the check below still sees it.
    text = json.dumps(document, indent=2, ensure_ascii=True, default=_jsonable)
    if "@" in text:  # what the walk cannot see: an id's or a time's text
        raise OutputRefused("$")
    return text


def safe_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """A refusal's params, the ones known to carry an address replaced."""
    return {key: REDACTED if key in ADDRESS_PARAMS else value for key, value in params.items()}


def _print(stream: TextIO, document: Mapping[str, Any], exit_code: int) -> int:
    try:
        text = render(document)
    except OutputRefused as exc:
        exit_code = EXIT_INTERNAL
        text = render(_internal(document, "script.output_refused", {"field": exc.where}))
    except (TypeError, ValueError) as exc:
        exit_code = EXIT_INTERNAL
        text = render(_internal(document, "script.internal_error", {"type": type(exc).__name__}))
    stream.write(text + "\n")
    stream.flush()
    return exit_code


def _internal(document: Mapping[str, Any], code: str, params: dict[str, Any]) -> dict[str, Any]:
    """The document of an internal error: the command's name and the code,
    nothing of what could not be printed."""
    return {"ok": False, "command": document.get("command"), "code": code, "params": params}


@contextmanager
def _silenced() -> Iterator[None]:
    """Nothing but the document reaches the output: no log line (the
    managers', SQLAlchemy's echo, which prints statements with their
    values), no warning. Restored afterwards (tests share the process)."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logging.disable(previous)


# ── refusals ────────────────────────────────────────────────────────────────


class Refusal(Exception):
    """The script's own refusal: a code, its params, the exit code, and what
    the command did before it, when it had started."""

    def __init__(
        self,
        code: str,
        params: Mapping[str, Any] | None = None,
        *,
        exit_code: int = EXIT_REFUSED,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.params = dict(params or {})
        self.exit_code = exit_code
        self.result = dict(result) if result is not None else None


@contextmanager
def keeping(result: dict[str, Any]) -> Iterator[None]:
    """A refusal met halfway is told with what was done before it (the
    campaign's id, to clean it up)."""
    try:
        yield
    except NidriaError as exc:
        raise Refusal(exc.code, safe_params(exc.params), result=result) from exc
    except Refusal as exc:
        if exc.result is None:
            exc.result = result
        raise


@dataclass(frozen=True)
class Outcome:
    """What a command did; `code` when it fell short (exit 1, the result
    printed all the same)."""

    result: dict[str, Any]
    code: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


def production_guard(environment: str, fly_app_name: str | None) -> str | None:
    """None when the script may run here, else why not: with
    ENVIRONMENT=production or on a Fly machine, only on `nidria-api`."""
    if not fly_app_name:
        return "fly_app_name_missing" if environment == "production" else None
    if fly_app_name != PRODUCTION_APP:
        return "fly_app_name_mismatch"
    return None


# ── the agent, the routes, the calls ────────────────────────────────────────


class Route(NamedTuple):
    """A route of the API as its binding names it: method, path template."""

    method: str
    path: str


PROVIDER_ACCOUNT = Route("GET", "/admin/email/provider")
PROVIDER_WEBHOOK = Route("POST", "/admin/email/provider/webhook")
AGENCY_MODULE = Route("PATCH", "/agencies/{agency_id}/email-module")
AGENCY_TEAM = Route("PUT", "/agencies/{agency_id}/email-module/team")
IMPORT_PLAN = Route("GET", "/email/import/lemlist")
IMPORT_APPLY = Route("POST", "/email/import/lemlist")
CAMPAIGNS = Route("GET", "/email/campaigns")
SENDERS = Route("GET", "/email/senders")
CAMPAIGN_CREATE = Route("POST", "/email/campaigns")
CAMPAIGN_READ = Route("GET", "/email/campaigns/{campaign_id}")
CAMPAIGN_UPDATE = Route("PATCH", "/email/campaigns/{campaign_id}")
CAMPAIGN_SYNC = Route("POST", "/email/campaigns/{campaign_id}/sync")
CAMPAIGN_LAUNCH = Route("POST", "/email/campaigns/{campaign_id}/launch")
STEP_CREATE = Route("POST", "/email/campaigns/{campaign_id}/steps")
MEMBERS = Route("GET", "/email/members")
MEMBERS_ADD = Route("POST", "/email/campaigns/{campaign_id}/members/add")
MEMBERS_REMOVE = Route("POST", "/email/members/remove")
PROFILE_CREATE = Route("POST", "/client-profiles")
PROFILE_DELETE = Route("DELETE", "/client-profiles/{profile_id}")
JOB_UPDATE = Route("PATCH", "/jobs/{job_id}")
JOB_TRIGGER = Route("POST", "/jobs/{job_id}/trigger")


@dataclass(frozen=True)
class Actor:
    """The `--as` agent, by ids only (its address is never kept)."""

    agent_id: uuid.UUID
    agency_id: uuid.UUID
    agency_slug: str
    agency_name: str

    def agency(self) -> dict[str, Any]:
        return {"id": self.agency_id, "slug": self.agency_slug, "name": self.agency_name}


@dataclass(frozen=True)
class Context:
    sessions: async_sessionmaker[AsyncSession]
    sync_sessions: Callable[[], sessionmaker[Session]]
    actor: Actor
    #: The routes the running command declared: the only ones it may call.
    routes: frozenset[Route]


def _agents() -> Select[tuple[Agent]]:
    """Agents with their role and its permissions, as enforce() reads one."""
    return select(Agent).options(selectinload(Agent.role).selectinload(Role.permissions))


async def find_actor(db: AsyncSession, email: str) -> Actor:
    """The `--as` agent, by address whatever its case."""
    found = (
        (await db.execute(_agents().where(func.lower(Agent.email) == normalize_email(email))))
        .scalars()
        .all()
    )
    if not found:
        raise Refusal("script.agent_not_found", exit_code=EXIT_USAGE)
    if len(found) > 1:
        raise Refusal("script.agent_ambiguous", {"count": len(found)}, exit_code=EXIT_USAGE)
    agent = found[0]
    if agent.deactivated_at is not None:
        raise Refusal("script.agent_inactive", exit_code=EXIT_USAGE)
    if agent.is_external:
        raise Refusal("script.agent_external", exit_code=EXIT_USAGE)
    agency = await db.get(Agency, agent.agency_id)
    assert agency is not None  # an agent belongs to an agency (FK)
    return Actor(agent.id, agency.id, agency.slug, agency.name)


async def reload_agent(db: AsyncSession, actor: Actor) -> Agent:
    """The agent read again, as enforce() reads it on every request: gone or
    deactivated meanwhile, refused."""
    agent = (await db.execute(_agents().where(Agent.id == actor.agent_id))).scalar_one_or_none()
    if agent is None or agent.deactivated_at is not None:
        raise Refusal("script.agent_inactive", exit_code=EXIT_USAGE)
    return agent


async def through[T](
    ctx: Context, route: Route, call: Callable[[AsyncSession, Agent], Awaitable[T]]
) -> T:
    """One call as the API serves a request: a fresh session, the agent read
    again, `authorize_agent` on the route's template, then the route's
    manager (`call`)."""
    if route not in ctx.routes:
        raise RuntimeError(f"Undeclared route {route.method} {route.path}")
    async with ctx.sessions() as db:
        agent = await reload_agent(db, ctx.actor)
        await authorize_agent(db, agent, route.method, route.path)
        return await call(db, agent)


async def preflight(ctx: Context) -> None:
    """Every route the command declared, authorized before its first call:
    a refusal stops it before anything is written."""
    async with ctx.sessions() as db:
        agent = await reload_agent(db, ctx.actor)
        for route in sorted(ctx.routes):
            await authorize_agent(db, agent, route.method, route.path)


def require_platform_operator(agent: Agent) -> None:
    """The platform's superadmin role (the system row, never an agency
    clone of it) holding `agency.create`."""
    key = Permission.AGENCY_CREATE.value
    if not (is_platform_role(agent.role) and key in effective_permissions(agent)):
        raise ForbiddenError(
            "Missing permission.", code="permission.denied", params={"permission": key}
        )


async def as_platform[T](ctx: Context, call: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """A call that stands for no route (`platform-roles-audit`): the agent
    read again, the platform's superadmin only."""
    async with ctx.sessions() as db:
        require_platform_operator(await reload_agent(db, ctx.actor))
        return await call(db)


# ── reads shared by the commands ────────────────────────────────────────────


def module_read(state: AgencyEmailModuleResponse) -> dict[str, Any]:
    return state.model_dump()


async def agency_read(ctx: Context, agency_id: uuid.UUID) -> dict[str, Any]:
    async with ctx.sessions() as db:
        agency = await db.get(Agency, agency_id)
    if agency is None:
        return {"id": agency_id, "slug": None, "name": None}
    return {"id": agency.id, "slug": agency.slug, "name": agency.name}


def smoke_mark(campaign: EmailCampaign) -> dict[str, Any] | None:
    """A smoke test campaign's mark, or None for any other campaign."""
    found = (campaign.provider_metadata or {}).get(SMOKE_TEST_METADATA_KEY)
    return dict(found) if isinstance(found, dict) else None


def _uuids(values: object) -> list[uuid.UUID]:
    if not isinstance(values, list):
        return []
    return [uuid.UUID(str(value)) for value in values]


def member_read(member: EmailCampaignMember) -> dict[str, Any]:
    """A membership by ids, statuses and times (never the person)."""
    return {
        "member_id": member.id,
        "client_profile_id": member.client_profile_id,
        "status": member.status,
        "status_reason": member.status_reason,
        "current_step": member.current_step,
        "provider_lead_id": member.provider_lead_id,
        "provider_contact_id": member.provider_contact_id,
        "queued_at": member.queued_at,
        "started_at": member.started_at,
        "last_event_type": member.last_event_type,
        "last_event_at": member.last_event_at,
        "removed_at": member.removed_at,
    }


async def _members_of(
    db: AsyncSession, agency_id: uuid.UUID, campaign_id: uuid.UUID
) -> list[EmailCampaignMember]:
    stmt = (
        select(EmailCampaignMember)
        .where(
            EmailCampaignMember.agency_id == agency_id,
            EmailCampaignMember.campaign_id == campaign_id,
        )
        .order_by(EmailCampaignMember.added_at, EmailCampaignMember.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def campaign_members(
    db: AsyncSession, agent: Agent, campaign_id: uuid.UUID
) -> list[dict[str, Any]]:
    """GET /email/members of one campaign, by ids and statuses: the module's
    own access (module on, `email.view`, `case.view`), the agency's campaign
    or 404."""
    manager = EmailManager(db)
    access = await manager.access(agent)
    await manager.campaign_for(access, campaign_id)
    return [member_read(m) for m in await _members_of(db, access.agency_id, campaign_id)]


def sync_read(result: EmailSyncResult) -> dict[str, Any]:
    return {
        "external": result.external,
        "campaign_created": result.campaign_created,
        "steps_created": result.steps_created,
        "steps_updated": result.steps_updated,
        "events_ingested": result.events_ingested,
        "members_updated": result.members_updated,
        "updates_remaining": result.updates_remaining,
        "members_held": result.members_held,
        "remaining": result.remaining,
        "daily_limit": result.daily_limit,
        "pushed_today": result.pushed_today,
        "unknown_leads": result.unknown_leads,
        "errors": [error.model_dump() for error in result.errors],
        "notes": [note.model_dump() for note in result.notes],
        "last_synced_at": result.last_synced_at,
    }


def launch_read(result: EmailLaunchResult) -> dict[str, Any]:
    """A Launch by ids and codes: its failures without the person's name."""
    return {
        **sync_read(result),
        "members_pushed": result.members_pushed,
        "members_failed": result.members_failed,
        "launched_ids": list(result.launched_ids),
        "failures": [
            {
                "member_id": failure.member_id,
                "client_profile_id": failure.client_profile_id,
                "code": failure.code,
                "params": safe_params(failure.params),
            }
            for failure in result.failures
        ],
        "skipped": [
            {"member_id": skip.member_id, "code": skip.code, "params": safe_params(skip.params)}
            for skip in result.skipped
        ],
        "stopped": result.stopped,
    }


async def sync_rounds(ctx: Context, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    """The campaign's Sync, called again while running leads remain to
    refresh (as the frontend loops), at most MAX_ROUNDS times."""
    rounds: list[dict[str, Any]] = []
    for _ in range(MAX_ROUNDS):
        done = await through(
            ctx,
            CAMPAIGN_SYNC,
            lambda db, agent: EmailSyncManager(db).sync_campaign(
                agent, campaign_id, EmailSyncRequest()
            ),
        )
        rounds.append(sync_read(done))
        if done.updates_remaining == 0:
            break
    return rounds


def plan_read(plan: EmailImportPlanRead) -> dict[str, Any]:
    """The plan as the route answers it, counts and campaign names only
    (the mapping's rules aside: each state already names its rule)."""
    return {
        "listing_complete": plan.listing_complete,
        "calls": plan.calls.model_dump(),
        "campaigns": [
            {
                "provider_campaign_id": campaign.provider_campaign_id,
                "name": campaign.name,
                "status": campaign.status,
                "already_imported": campaign.already_imported,
                "error": campaign.error,
                "steps": campaign.steps.model_dump() if campaign.steps is not None else None,
                "leads": campaign.leads.model_dump() if campaign.leads is not None else None,
                "states": [state.model_dump() for state in campaign.states],
                "calls": campaign.calls.model_dump(),
            }
            for campaign in plan.campaigns
        ],
        "not_importable": [ref.model_dump() for ref in plan.not_importable],
        "created_by_nidria": [ref.model_dump() for ref in plan.created_by_nidria],
    }


async def import_plan(ctx: Context) -> EmailImportPlanRead:
    return await through(ctx, IMPORT_PLAN, lambda db, agent: EmailImportManager(db).plan(agent))


# ── status, webhook-create ──────────────────────────────────────────────────


async def cmd_status(ctx: Context, args: argparse.Namespace) -> Outcome:
    """GET /admin/email/provider: the mailboxes by id, never their address."""
    account = await through(
        ctx, PROVIDER_ACCOUNT, lambda db, _agent: EmailAdminManager(db).provider_account()
    )
    webhook = account.webhook
    return Outcome(
        {
            "dry_run": account.dry_run,
            "external_effects": account.external_effects,
            "lemlist_mocked": account.mocked,
            "key_configured": account.key_configured,
            "key_fingerprint": account.key_fingerprint,
            "team": account.team.model_dump() if account.team is not None else None,
            "plan": account.plan,
            "senders": [
                {"user_id": sender.user_id, "campaigns": sender.campaigns}
                for sender in account.senders
            ],
            "webhook": webhook.model_dump() if webhook is not None else None,
            "bound_agency": (
                await agency_read(ctx, account.bound_agency.id)
                if account.bound_agency is not None
                else None
            ),
        }
    )


async def cmd_webhook_create(ctx: Context, args: argparse.Namespace) -> Outcome:
    """POST /admin/email/provider/webhook[?recreate=true]."""
    recreate = bool(args.recreate)
    done = await through(
        ctx,
        PROVIDER_WEBHOOK,
        lambda db, _agent: EmailAdminManager(db).ensure_webhook(recreate=recreate),
    )
    return Outcome(
        {
            "hook_id": done.webhook.hook_id,
            "created": done.created,
            "recreated": done.recreated,
            "has_type": done.webhook.has_type,
            "target_url": done.webhook.target_url,
        }
    )


# ── agency-enable, bind-team ────────────────────────────────────────────────


async def target_agency(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    """The agency `--agency <slug>` names, or the `--as` agent's own
    (`--own-agency`), always printed with its id, slug and name."""
    if args.own_agency:
        return ctx.actor.agency()
    async with ctx.sessions() as db:
        agency = (
            await db.execute(select(Agency).where(Agency.slug == str(args.agency).strip()))
        ).scalar_one_or_none()
    if agency is None:
        raise Refusal("agency.not_found")
    return {"id": agency.id, "slug": agency.slug, "name": agency.name}


async def cmd_agency_enable(ctx: Context, args: argparse.Namespace) -> Outcome:
    """PATCH /agencies/{agency_id}/email-module {"enabled": true}."""
    agency = await target_agency(ctx, args)
    body = AgencyEmailModuleUpdateRequest(enabled=True)
    with keeping({"agency": agency}):  # a refusal names the agency too
        state = await through(
            ctx,
            AGENCY_MODULE,
            lambda db, agent: EmailAdminManager(db).set_enabled(
                agent, agency["id"], body.enabled, pause_campaigns=body.pause_campaigns
            ),
        )
    return Outcome({"agency": agency, "module": module_read(state)})


async def cmd_bind_team(ctx: Context, args: argparse.Namespace) -> Outcome:
    """PUT /agencies/{agency_id}/email-module/team."""
    agency = await target_agency(ctx, args)
    with keeping({"agency": agency}):  # a refusal names the agency too
        state = await through(
            ctx,
            AGENCY_TEAM,
            lambda db, agent: EmailAdminManager(db).bind_team(agent, agency["id"]),
        )
    return Outcome({"agency": agency, "module": module_read(state)})


# ── import-plan, import-apply, import-verify ────────────────────────────────


async def cmd_import_plan(ctx: Context, args: argparse.Namespace) -> Outcome:
    """GET /email/import/lemlist, in the `--as` agent's agency (`actor`)."""
    return Outcome({"plan": plan_read(await import_plan(ctx))})


async def cmd_import_apply(ctx: Context, args: argparse.Namespace) -> Outcome:
    """The plan (GET), then POST /email/import/lemlist: the campaigns named,
    else every importable campaign the plan could read; refused while those
    count conflicts, unless `--accept-conflicts`."""
    named = list(dict.fromkeys(str(value).strip() for value in args.campaign))
    if named:
        EmailImportRequest(campaign_ids=named)  # the route's own checks, before any call
    plan = await import_plan(ctx)
    planned = {campaign.provider_campaign_id: campaign for campaign in plan.campaigns}
    selected = named or [c.provider_campaign_id for c in plan.campaigns if c.error is None]
    conflicts: dict[str, int] = {}
    for campaign_id in selected:
        leads = planned[campaign_id].leads if campaign_id in planned else None
        if leads is not None:
            conflicts[campaign_id] = leads.conflicts + leads.conflicts_in_plan
    result: dict[str, Any] = {
        "selected": selected,
        "conflicts": conflicts,
        "accept_conflicts": bool(args.accept_conflicts),
    }
    if not selected:
        return Outcome(result, code="script.nothing_to_import")
    total = sum(conflicts.values())
    if total and not args.accept_conflicts:
        return Outcome(result, code="script.import_conflicts", params={"conflicts": total})
    body = EmailImportRequest(campaign_ids=selected)
    done = await through(
        ctx, IMPORT_APPLY, lambda db, agent: EmailImportManager(db).apply(agent, body)
    )
    result["calls"] = done.calls
    result["campaigns"] = [row.model_dump() for row in done.campaigns]
    failed = sum(1 for row in done.campaigns if not row.ok)
    if failed:
        return Outcome(result, code="script.import_incomplete", params={"failed": failed})
    return Outcome(result)


async def imported_counts(db: AsyncSession, agent: Agent) -> list[dict[str, Any]]:
    """The agency's imported campaigns, counted in Nidria (reads only):
    memberships by status (removed ones included, and not), events by
    type, messages by direction, status and content state."""
    access = await EmailManager(db).access(agent)
    agency_id = access.agency_id
    campaigns = (
        (
            await db.execute(
                select(EmailCampaign)
                .where(
                    EmailCampaign.agency_id == agency_id,
                    EmailCampaign.origin == ORIGIN_LEMLIST_IMPORT,
                )
                .order_by(EmailCampaign.created_at, EmailCampaign.id)
            )
        )
        .scalars()
        .all()
    )
    ids = [campaign.id for campaign in campaigns]
    if not ids:
        return []
    is_open = EmailCampaignMember.removed_at.is_(None)
    has_lead = EmailCampaignMember.provider_lead_id.is_not(None)
    members = (
        await db.execute(
            select(
                EmailCampaignMember.campaign_id,
                EmailCampaignMember.status,
                is_open,
                has_lead,
                func.count(),
            )
            .where(
                EmailCampaignMember.agency_id == agency_id,
                EmailCampaignMember.campaign_id.in_(ids),
            )
            .group_by(
                EmailCampaignMember.campaign_id, EmailCampaignMember.status, is_open, has_lead
            )
        )
    ).all()
    events = (
        await db.execute(
            select(EmailProviderEvent.campaign_id, EmailProviderEvent.type, func.count())
            .where(
                EmailProviderEvent.agency_id == agency_id, EmailProviderEvent.campaign_id.in_(ids)
            )
            .group_by(EmailProviderEvent.campaign_id, EmailProviderEvent.type)
        )
    ).all()
    messages = (
        await db.execute(
            select(
                EmailMessage.campaign_id,
                EmailMessage.direction,
                EmailMessage.status,
                EmailMessage.content_state,
                func.count(),
            )
            .where(EmailMessage.agency_id == agency_id, EmailMessage.campaign_id.in_(ids))
            .group_by(
                EmailMessage.campaign_id,
                EmailMessage.direction,
                EmailMessage.status,
                EmailMessage.content_state,
            )
        )
    ).all()
    out: list[dict[str, Any]] = []
    for campaign in campaigns:
        by_status: Counter[str] = Counter()
        open_by_status: Counter[str] = Counter()
        open_with_lead = 0
        for campaign_id, status, member_open, member_lead, count in members:
            if campaign_id != campaign.id:
                continue
            by_status[status] += count
            if member_open:
                open_by_status[status] += count
                open_with_lead += count if member_lead else 0
        event_types: Counter[str] = Counter()
        for campaign_id, kind, count in events:
            if campaign_id == campaign.id:
                event_types[kind] += count
        sent: dict[str, dict[str, Any]] = {}
        for campaign_id, direction, status, content_state, count in messages:
            if campaign_id != campaign.id:
                continue
            bucket = sent.setdefault(
                direction, {"total": 0, "by_status": Counter(), "by_content_state": Counter()}
            )
            bucket["total"] += count
            bucket["by_status"][status] += count
            bucket["by_content_state"][content_state] += count
        noted = import_metadata(campaign.provider_metadata)
        out.append(
            {
                "campaign_id": campaign.id,
                "provider_campaign_id": campaign.provider_campaign_id,
                "name": campaign.name,
                "status": campaign.status,
                "provider_status": campaign.provider_status,
                "last_imported_at": noted.get("imported_at"),
                "unknown_leads": noted.get("unknown_leads"),
                "set_aside_at_last_import": len(noted.get("set_aside") or []),
                "members": {
                    "total": sum(by_status.values()),
                    "open": sum(open_by_status.values()),
                    "removed": sum(by_status.values()) - sum(open_by_status.values()),
                    "open_with_lead_id": open_with_lead,
                    "by_status": dict(sorted(by_status.items())),
                    "open_by_status": dict(sorted(open_by_status.items())),
                },
                "events_by_type": dict(sorted(event_types.items())),
                "messages": {
                    direction: {
                        "total": bucket["total"],
                        "by_status": dict(sorted(bucket["by_status"].items())),
                        "by_content_state": dict(sorted(bucket["by_content_state"].items())),
                    }
                    for direction, bucket in sorted(sent.items())
                },
            }
        )
    return out


async def cmd_import_verify(ctx: Context, args: argparse.Namespace) -> Outcome:
    """Each imported campaign of the agency: Nidria's counts against
    Lemlist's, read again in GET only by the plan (its export, its listing,
    both together, the export's leads the listing missed, its leads by
    status word and by state), the gap between Lemlist's leads (the export's
    and the listing's together: the listing's pages shift and lose a lead
    now and then) and Nidria's open memberships, and what explains it: the
    leads the plan sets aside (by reason), the ones it would import now, the
    memberships whose lead Lemlist no longer has."""
    plan = await import_plan(ctx)
    counted = await through(ctx, CAMPAIGNS, imported_counts)
    planned = {campaign.provider_campaign_id: campaign for campaign in plan.campaigns}
    apart = {ref.provider_campaign_id: ref.status for ref in plan.not_importable}
    imported_ids = {row["provider_campaign_id"] for row in counted}
    campaigns: list[dict[str, Any]] = []
    for row in counted:
        nidria = row["members"]
        campaign = planned.get(row["provider_campaign_id"])
        entry: dict[str, Any] = {**row, "lemlist": None, "gaps": None}
        if campaign is None:
            entry["lemlist"] = {
                "read": False,
                "status": apart.get(row["provider_campaign_id"]),
            }
        elif campaign.error is not None or campaign.leads is None:
            entry["lemlist"] = {"read": False, "status": campaign.status, "error": campaign.error}
        else:
            leads = campaign.leads
            by_status: Counter[str] = Counter()
            by_state: Counter[str] = Counter()
            for state in campaign.states:
                by_status[state.status or "none"] += state.count
                by_state[state.state or "none"] += state.count
            entry["lemlist"] = {
                "read": True,
                "status": campaign.status,
                "exported": leads.exported,
                "listed": leads.listed,
                "total": leads.total,
                "not_listed": leads.not_listed,
                "contact_ids_found": leads.contact_ids_found,
                "without_contact_id": leads.without_contact_id,
                "listing_complete": leads.listing_complete,
                "listing_duplicates": leads.listing_duplicates,
                "identities": leads.identities,
                "from_export": leads.from_export,
                "from_contacts": leads.from_contacts,
                "without_email": leads.without_email,
                "by_status": dict(sorted(by_status.items())),
                "by_state": dict(sorted(by_state.items())),
            }
            set_aside = sum(leads.not_imported.values())
            gone = nidria["open_with_lead_id"] - leads.to_update
            gap = leads.total - nidria["open_with_lead_id"]
            entry["gaps"] = {
                "lemlist_minus_open_memberships": gap,
                "set_aside_by_plan": set_aside,
                "set_aside_by_reason": dict(sorted(leads.not_imported.items())),
                "new_at_lemlist": leads.to_create,
                "open_memberships_not_at_lemlist": gone,
                "explained": gap == set_aside + leads.to_create - gone,
            }
        campaigns.append(entry)
    return Outcome(
        {
            "campaigns": campaigns,
            "importable_not_imported": [
                {
                    "provider_campaign_id": campaign.provider_campaign_id,
                    "name": campaign.name,
                    "status": campaign.status,
                    "error": campaign.error,
                }
                for campaign in plan.campaigns
                if campaign.provider_campaign_id not in imported_ids
            ],
        }
    )


# ── smoke-start ─────────────────────────────────────────────────────────────

_ADDRESS = TypeAdapter(NormalizedEmailStr)


def recipients(values: Sequence[str]) -> list[str]:
    """The `--to` addresses, checked as POST /client-profiles checks one and
    lower-cased; never printed: each is named by its rank, `to_index`."""
    if len(values) > SMOKE_MAX_RECIPIENTS:
        raise Refusal(
            "script.too_many_recipients", {"max": SMOKE_MAX_RECIPIENTS}, exit_code=EXIT_USAGE
        )
    found: list[str] = []
    for index, value in enumerate(values, start=1):
        try:
            address = _ADDRESS.validate_python(value)
        except PydanticValidationError:
            raise Refusal(
                "script.invalid_email", {"to_index": index}, exit_code=EXIT_USAGE
            ) from None
        if address in found:
            raise Refusal("script.duplicate_recipient", {"to_index": index}, exit_code=EXIT_USAGE)
        found.append(address)
    return found


def smoke_url(value: str | None) -> str:
    if value is None:
        return SMOKE_URL
    url = value.strip()
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or "@" in url
        or any(char.isspace() for char in url)
        or len(url) > 500
    ):
        raise Refusal("script.invalid_url", exit_code=EXIT_USAGE)
    return url


def followup_days(value: int | None) -> int | None:
    if value is not None and not 0 <= value <= 365:
        raise Refusal("script.invalid_followup_days", {"max": 365}, exit_code=EXIT_USAGE)
    return value


def sending_window(local_now: datetime) -> tuple[str, str]:
    """From an hour before now to four hours after, on quarter hours, within
    the day: a window that contains now."""
    minute = local_now.hour * 60 + local_now.minute
    start = max(0, (minute - 60) // 15 * 15)
    end = min(23 * 60 + 59, -(-(minute + 240) // 15) * 15)
    return f"{start // 60:02d}:{start % 60:02d}", f"{end // 60:02d}:{end % 60:02d}"


def pick_sender(senders: EmailSendersRead, wanted: str | None) -> EmailSender | None:
    """Who sends the smoke test: in a dry run nobody (Lemlist is not asked);
    else the team's only mailbox, or the one `--sender` names among
    several (ids printed, never addresses)."""
    options = senders.senders
    ids = [option.user_id for option in options]
    if not senders.external:
        if wanted is not None:
            raise Refusal("script.sender_unknown", {"senders": ids}, exit_code=EXIT_USAGE)
        return None
    if senders.note == "provider_error":
        raise Refusal(senders.error_code or "email.provider_error")
    if wanted is not None:
        chosen = next((option for option in options if option.user_id == wanted), None)
        if chosen is None:
            raise Refusal("script.sender_unknown", {"senders": ids}, exit_code=EXIT_USAGE)
        return EmailSender(user_id=chosen.user_id, email=chosen.email)
    if not options:
        raise Refusal("script.no_sender")
    if len(options) > 1:
        raise Refusal("script.sender_required", {"senders": ids}, exit_code=EXIT_USAGE)
    return EmailSender(user_id=options[0].user_id, email=options[0].email)


async def create_marked(
    db: AsyncSession, agent: Agent, body: EmailCampaignCreate
) -> EmailCampaignDetailRead:
    """POST /email/campaigns, then the campaign's mark: what tells
    `smoke-cleanup` it is a smoke test, and which records it created."""
    detail = await EmailManager(db).create_campaign(agent, body)
    campaign = await EmailRepository(db).campaign(agent.agency_id, detail.id, lock=True)
    assert campaign is not None  # created just above, in the agent's agency
    mark = {
        "started_at": datetime.now(UTC).isoformat(),
        "started_by_agent_id": str(agent.id),
        "created_profile_ids": [],
        "reused_profile_ids": [],
    }
    campaign.provider_metadata = {
        **(campaign.provider_metadata or {}),
        SMOKE_TEST_METADATA_KEY: mark,
    }
    await db.commit()
    return detail


async def recipe_profiles(
    db: AsyncSession, agent: Agent, campaign_id: uuid.UUID, addresses: Sequence[str]
) -> list[dict[str, Any]]:
    """The client records of the `--to` addresses, under POST
    /client-profiles' rights: the agency's record of each address (rule N2:
    the account's address when linked, else its own) reused and never
    changed, else one built by `create_profile`'s own core
    (`seed_client_profiles`, as the Lemlist import does: no account,
    invitation nor email), named "Recette interne", source
    `email_smoke_test`. The records created and the campaign's mark are
    written in one transaction: a created record is always marked."""
    seeds = [
        ProfileSeed(SMOKE_FIRST_NAME, SMOKE_LAST_NAME, address, SMOKE_SOURCE)
        for address in addresses
    ]
    existing, built = await seed_client_profiles(
        ClientProfilesRepository(db), agent.agency_id, seeds
    )
    campaign = await EmailRepository(db).campaign(agent.agency_id, campaign_id, lock=True)
    if campaign is None or (mark := smoke_mark(campaign)) is None:
        raise Refusal("script.not_a_smoke_campaign")
    created = {profile.email: profile.id for profile in built}
    rows: list[dict[str, Any]] = []
    for index, address in enumerate(addresses, start=1):
        if address in existing:
            rows.append(
                {"to_index": index, "client_profile_id": existing[address], "origin": "reused"}
            )
        else:
            rows.append(
                {"to_index": index, "client_profile_id": created[address], "origin": "created"}
            )
    db.add_all(built)
    mark["created_profile_ids"] = [
        str(row["client_profile_id"]) for row in rows if row["origin"] == "created"
    ]
    mark["reused_profile_ids"] = [
        str(row["client_profile_id"]) for row in rows if row["origin"] == "reused"
    ]
    campaign.provider_metadata = {
        **(campaign.provider_metadata or {}),
        SMOKE_TEST_METADATA_KEY: mark,
    }
    await db.commit()
    return rows


async def create_step(
    ctx: Context, campaign_id: uuid.UUID, step: EmailStepCreate
) -> EmailCampaignDetailRead:
    return await through(
        ctx, STEP_CREATE, lambda db, agent: EmailManager(db).create_step(agent, campaign_id, step)
    )


async def launch_members(
    ctx: Context, campaign_id: uuid.UUID, member_ids: list[uuid.UUID]
) -> EmailLaunchResult:
    request = EmailLaunchRequest(member_ids=member_ids)
    return await through(
        ctx,
        CAMPAIGN_LAUNCH,
        lambda db, agent: EmailSyncManager(db).launch(agent, campaign_id, request),
    )


def _merge_members(recipients_out: list[dict[str, Any]], members: list[dict[str, Any]]) -> None:
    """Each recipient's membership in the smoke test campaign, by its record."""
    by_profile = {member["client_profile_id"]: member for member in members}
    for row in recipients_out:
        member = by_profile.get(row["client_profile_id"])
        row["member_id"] = member["member_id"] if member else None
        row["member_status"] = member["status"] if member else None
        row["status_reason"] = member["status_reason"] if member else None
        row["provider_lead_id"] = member["provider_lead_id"] if member else None


async def cmd_smoke_start(ctx: Context, args: argparse.Namespace) -> Outcome:
    """A smoke test campaign, as an agent makes one in the app: created
    (with its mark), its steps, its recipients' records, the recipients
    added, the campaign activated, synced, then the recipients launched."""
    addresses = recipients(args.to)
    url = smoke_url(args.url)
    followup = followup_days(args.followup_days)
    zone = EmailCampaignSettings().timezone
    local_now = datetime.now(ZoneInfo(zone))
    start, end = sending_window(local_now)
    senders = await through(ctx, SENDERS, lambda db, agent: EmailManager(db).senders(agent))
    sender = pick_sender(senders, args.sender)
    settings = EmailCampaignSettings(
        timezone=zone,
        sending_days=SMOKE_DAYS,
        sending_start_time=start,
        sending_end_time=end,
        daily_limit=SMOKE_DAILY_LIMIT,
        sender=sender,
    )
    name = args.name.strip() if args.name else f"RECETTE interne {local_now:%Y-%m-%d %H:%M}"
    create = EmailCampaignCreate(name=name, settings=settings)
    first = EmailStepCreate(
        name="Recette 1", subject_template=SMOKE_SUBJECT, body_template=smoke_body(url)
    )
    second = (
        EmailStepCreate(
            name="Recette 2",
            delay_value=followup,
            subject_template="",
            body_template=SMOKE_FOLLOWUP_BODY,
        )
        if followup is not None
        else None
    )
    report: dict[str, Any] = {
        "settings": {
            "timezone": settings.timezone,
            "sending_days": settings.sending_days,
            "sending_start_time": settings.sending_start_time,
            "sending_end_time": settings.sending_end_time,
            "daily_limit": settings.daily_limit,
            "sender_user_id": sender.user_id if sender is not None else None,
        }
    }
    with keeping(report):
        detail = await through(
            ctx, CAMPAIGN_CREATE, lambda db, agent: create_marked(db, agent, create)
        )
        campaign_id = detail.id
        report["campaign"] = {"id": campaign_id, "name": detail.name}
        for step in (first, second):
            if step is None:
                continue
            detail = await create_step(ctx, campaign_id, step)
        report["steps"] = [
            {"id": step.id, "position": step.position, "delay_value": step.delay_value}
            for step in detail.steps
        ]
        rows = await through(
            ctx,
            PROFILE_CREATE,
            lambda db, agent: recipe_profiles(db, agent, campaign_id, addresses),
        )
        report["recipients"] = rows
        add = EmailAddMembersRequest(client_profile_ids=[row["client_profile_id"] for row in rows])
        added = await through(
            ctx,
            MEMBERS_ADD,
            lambda db, agent: EmailManager(db).add_members(agent, campaign_id, add),
        )
        verdicts = {verdict.client_profile_id: verdict for verdict in added.rows}
        for row in rows:
            verdict = verdicts.get(row["client_profile_id"])
            row["eligibility"] = verdict.eligibility if verdict is not None else None
            row["added"] = verdict is not None and verdict.action == "ADD"
        if not added.added:
            return Outcome(report, code="script.smoke_no_recipient")
        activate = EmailCampaignUpdate(status="ACTIVE")
        await through(
            ctx,
            CAMPAIGN_UPDATE,
            lambda db, agent: EmailManager(db).update_campaign(agent, campaign_id, activate),
        )
        report["sync"] = await sync_rounds(ctx, campaign_id)
        members = await through(
            ctx, MEMBERS, lambda db, agent: campaign_members(db, agent, campaign_id)
        )
        waiting = [m["member_id"] for m in members if m["status"] == "EMAIL_READY"]
        launches: list[dict[str, Any]] = []
        for _ in range(MAX_ROUNDS):
            if not waiting:
                break
            done = await launch_members(ctx, campaign_id, waiting)
            launches.append(launch_read(done))
            waiting = [member_id for member_id in waiting if member_id not in done.launched_ids]
            if not done.launched_ids or done.stopped:
                break
        report["launch"] = launches
        members = await through(
            ctx, MEMBERS, lambda db, agent: campaign_members(db, agent, campaign_id)
        )
        _merge_members(rows, members)
        final = await through(
            ctx, CAMPAIGN_READ, lambda db, agent: EmailManager(db).get_campaign(agent, campaign_id)
        )
        report["campaign"].update(
            status=final.status,
            provider_mode=final.provider_mode,
            provider_campaign_id=final.provider_campaign_id,
        )
        report["steps"] = [
            {
                "id": step.id,
                "position": step.position,
                "delay_value": step.delay_value,
                "provider_step_id": step.provider_step_id,
            }
            for step in final.steps
        ]
    launched = ("EMAIL_QUEUED", "EMAIL_ACTIVE")
    short = [row["to_index"] for row in rows if row.get("member_status") not in launched]
    if short:
        return Outcome(report, code="script.smoke_not_launched", params={"to_index": short})
    return Outcome(report)


# ── smoke-status, smoke-cleanup ─────────────────────────────────────────────


async def smoke_report(db: AsyncSession, agent: Agent, campaign_id: uuid.UUID) -> dict[str, Any]:
    """A smoke test campaign read in Nidria only (nothing asked of Lemlist):
    its provider ids and syncs, its steps, each membership's status,
    events by type and messages with their content state."""
    manager = EmailManager(db)
    access = await manager.access(agent)
    campaign = await manager.campaign_for(access, campaign_id)
    mark = smoke_mark(campaign)
    if mark is None:
        raise Refusal("script.not_a_smoke_campaign")
    created = set(_uuids(mark.get("created_profile_ids")))
    reused = set(_uuids(mark.get("reused_profile_ids")))
    members = await _members_of(db, access.agency_id, campaign.id)
    events = (
        await db.execute(
            select(EmailProviderEvent.campaign_member_id, EmailProviderEvent.type, func.count())
            .where(
                EmailProviderEvent.agency_id == access.agency_id,
                EmailProviderEvent.campaign_id == campaign.id,
            )
            .group_by(EmailProviderEvent.campaign_member_id, EmailProviderEvent.type)
        )
    ).all()
    messages = (
        await db.execute(
            select(
                EmailMessage.campaign_member_id,
                EmailMessage.direction,
                EmailMessage.status,
                EmailMessage.content_state,
                EmailMessage.step_position,
                EmailMessage.sent_at,
                EmailMessage.received_at,
                EmailMessage.provider_message_id.is_not(None),
            )
            .where(
                EmailMessage.agency_id == access.agency_id,
                EmailMessage.campaign_id == campaign.id,
            )
            .order_by(EmailMessage.created_at, EmailMessage.id)
        )
    ).all()
    campaign_events: Counter[str] = Counter()
    member_events: dict[uuid.UUID, Counter[str]] = {}
    for member_id, kind, count in events:
        campaign_events[kind] += count
        if member_id is not None:
            member_events.setdefault(member_id, Counter())[kind] += count
    member_messages: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for (
        member_id,
        direction,
        status,
        content_state,
        position,
        sent_at,
        received_at,
        has_id,
    ) in messages:
        member_messages.setdefault(member_id, []).append(
            {
                "direction": direction,
                "status": status,
                "content_state": content_state,
                "step_position": position,
                "at": received_at or sent_at,
                "provider_message_id": bool(has_id),
            }
        )
    rows: list[dict[str, Any]] = []
    for member in members:
        row = member_read(member)
        profile_id = member.client_profile_id
        row["origin"] = (
            "created" if profile_id in created else "reused" if profile_id in reused else "other"
        )
        row["events_by_type"] = dict(sorted(member_events.get(member.id, Counter()).items()))
        row["messages"] = member_messages.get(member.id, [])
        rows.append(row)
    return {
        "campaign": {
            "id": campaign.id,
            "name": campaign.name,
            "status": campaign.status,
            "provider_mode": campaign_provider_mode(campaign.provider_campaign_id),
            "provider_campaign_id": campaign.provider_campaign_id,
            "provider_sequence_id": campaign.provider_sequence_id,
            "provider_schedule_id": campaign.provider_schedule_id,
            "provider_status": campaign.provider_status,
            "last_synced_at": campaign.last_synced_at,
            "last_activity_sync_at": campaign.last_activity_sync_at,
            "last_provider_sync_at": campaign.last_provider_sync_at,
            "sync_error": campaign.sync_error,
            "last_provider_error": campaign.last_provider_error,
        },
        "smoke_test": {
            "started_at": mark.get("started_at"),
            "created_profile_ids": sorted(created, key=str),
            "reused_profile_ids": sorted(reused, key=str),
            "cleaned_at": mark.get("cleaned_at"),
        },
        "steps": [
            {
                "id": step.id,
                "position": step.position,
                "delay_value": step.delay_value,
                "provider_step_id": step.provider_step_id,
            }
            for step in sorted(campaign.steps, key=lambda s: s.position)
        ],
        "members": rows,
        "events_by_type": dict(sorted(campaign_events.items())),
    }


async def cmd_smoke_status(ctx: Context, args: argparse.Namespace) -> Outcome:
    """The smoke test read in Nidria only: by default nothing is asked of
    Lemlist (in production the reply must come in through the webhook);
    `--sync` runs the campaign's Sync first (POST .../sync)."""
    campaign_id: uuid.UUID = args.campaign
    report = await through(
        ctx, CAMPAIGN_READ, lambda db, agent: smoke_report(db, agent, campaign_id)
    )
    if args.sync:
        rounds = await sync_rounds(ctx, campaign_id)
        report = await through(
            ctx, CAMPAIGN_READ, lambda db, agent: smoke_report(db, agent, campaign_id)
        )
        report["sync"] = rounds
    return Outcome(report)


@dataclass(frozen=True)
class SmokeState:
    status: str
    open_member_ids: list[uuid.UUID]
    created_profile_ids: list[uuid.UUID]
    reused_profile_ids: list[uuid.UUID]


async def smoke_state(db: AsyncSession, agent: Agent, campaign_id: uuid.UUID) -> SmokeState:
    manager = EmailManager(db)
    access = await manager.access(agent)
    campaign = await manager.campaign_for(access, campaign_id)
    mark = smoke_mark(campaign)
    if mark is None:
        raise Refusal("script.not_a_smoke_campaign")
    members = await _members_of(db, access.agency_id, campaign.id)
    return SmokeState(
        status=campaign.status,
        open_member_ids=[member.id for member in members if member.removed_at is None],
        created_profile_ids=_uuids(mark.get("created_profile_ids")),
        reused_profile_ids=_uuids(mark.get("reused_profile_ids")),
    )


async def delete_created_profile(
    db: AsyncSession, agent: Agent, campaign_id: uuid.UUID, profile_id: uuid.UUID
) -> dict[str, Any]:
    """DELETE /client-profiles/{profile_id} for a record the smoke test
    created, checked again first: still its source, and in no other
    campaign (deleting it would take those memberships along)."""
    outcome: dict[str, Any] = {"client_profile_id": profile_id}
    profile = await ClientProfilesRepository(db).get_for_agency(agent.agency_id, profile_id)
    if profile is None:
        return {**outcome, "action": "already_deleted"}
    if profile.source != SMOKE_SOURCE:
        return {**outcome, "action": "kept", "code": "script.profile_not_from_smoke_test"}
    elsewhere = await db.scalar(
        select(func.count())
        .select_from(EmailCampaignMember)
        .where(
            EmailCampaignMember.client_profile_id == profile_id,
            EmailCampaignMember.campaign_id != campaign_id,
        )
    )
    if elsewhere:
        return {
            **outcome,
            "action": "kept",
            "code": "script.profile_used_elsewhere",
            "params": {"memberships": int(elsewhere)},
        }
    try:
        await ClientProfilesManager(db).delete_profile(agent, profile_id)
    except NidriaError as exc:
        await db.rollback()
        return {**outcome, "action": "kept", "code": exc.code, "params": safe_params(exc.params)}
    return {**outcome, "action": "deleted"}


async def remove_members(ctx: Context, ids: list[uuid.UUID]) -> EmailActionResult:
    return await through(ctx, MEMBERS_REMOVE, lambda db, agent: EmailManager(db).remove(agent, ids))


async def delete_profile(
    ctx: Context, campaign_id: uuid.UUID, profile_id: uuid.UUID
) -> dict[str, Any]:
    return await through(
        ctx,
        PROFILE_DELETE,
        lambda db, agent: delete_created_profile(db, agent, campaign_id, profile_id),
    )


async def mark_cleaned(
    db: AsyncSession, agent: Agent, campaign_id: uuid.UUID, deleted: Sequence[uuid.UUID]
) -> None:
    campaign = await EmailRepository(db).campaign(agent.agency_id, campaign_id, lock=True)
    if campaign is None or (mark := smoke_mark(campaign)) is None:
        raise Refusal("script.not_a_smoke_campaign")
    mark.update(
        cleaned_at=datetime.now(UTC).isoformat(),
        cleaned_by_agent_id=str(agent.id),
        deleted_profile_ids=sorted({*mark.get("deleted_profile_ids", []), *map(str, deleted)}),
    )
    campaign.provider_metadata = {
        **(campaign.provider_metadata or {}),
        SMOKE_TEST_METADATA_KEY: mark,
    }
    await db.commit()


async def cmd_smoke_cleanup(ctx: Context, args: argparse.Namespace) -> Outcome:
    """Every membership removed (POST /email/members/remove: the leads
    deleted at Lemlist), the campaign archived (PATCH: paused at Lemlist),
    then each record the smoke test created deleted (DELETE
    /client-profiles/{id}); a reused record is never touched."""
    campaign_id: uuid.UUID = args.campaign
    state = await through(ctx, CAMPAIGN_READ, lambda db, agent: smoke_state(db, agent, campaign_id))
    report: dict[str, Any] = {"campaign": {"id": campaign_id, "status_before": state.status}}
    failures = 0
    with keeping(report):
        removed: list[dict[str, Any]] = []
        for start in range(0, len(state.open_member_ids), REMOVE_BATCH):
            done = await remove_members(ctx, state.open_member_ids[start : start + REMOVE_BATCH])
            removed.extend(
                {
                    "member_id": row.id,
                    "ok": row.ok,
                    "code": row.code,
                    "params": safe_params(row.params),
                }
                for row in done.rows
            )
        failures += sum(1 for row in removed if not row["ok"])
        report["members_removed"] = removed
        status = state.status
        if status != "ARCHIVED":
            archive = EmailCampaignUpdate(status="ARCHIVED")
            detail = await through(
                ctx,
                CAMPAIGN_UPDATE,
                lambda db, agent: EmailManager(db).update_campaign(agent, campaign_id, archive),
            )
            status = detail.status
        report["campaign"]["status"] = status
        profiles: list[dict[str, Any]] = []
        for profile_id in state.created_profile_ids:
            profiles.append(await delete_profile(ctx, campaign_id, profile_id))
        failures += sum(1 for row in profiles if row["action"] == "kept")
        report["created_profiles"] = profiles
        report["reused_profiles_kept"] = state.reused_profile_ids
        deleted = [row["client_profile_id"] for row in profiles if row["action"] == "deleted"]
        await through(
            ctx,
            CAMPAIGN_UPDATE,
            lambda db, agent: mark_cleaned(db, agent, campaign_id, deleted),
        )
    if failures:
        return Outcome(report, code="script.smoke_cleanup_incomplete", params={"failed": failures})
    return Outcome(report)


# ── job-enable, job-run ─────────────────────────────────────────────────────


async def cmd_job_enable(ctx: Context, args: argparse.Namespace) -> Outcome:
    """PATCH /jobs/{--job} {"is_enabled": true} (E1). The route reloads
    the scheduler of the API that serves it (`request.app.state`); this
    process has none. An API whose scheduler runs (SCHEDULER_ENABLED) brings
    it in line with `job_config` every RECONCILE_INTERVAL_SECONDS
    (`reconcile_jobs`): `running_api_picks_up_within_seconds`, null when
    SCHEDULER_ENABLED is off in this environment (on the API's machine, the
    API's own): no cron then, `job-run` only."""
    body = JobConfigUpdateRequest(is_enabled=True)

    async def update(db: AsyncSession, _agent: Agent) -> dict[str, Any]:
        config = await JobsManager(db).update_job(args.job, body, None, None)
        return JobConfigResponse.model_validate(config).model_dump()

    job = await through(ctx, JOB_UPDATE, update)
    scheduled = get_settings().scheduler_enabled
    return Outcome(
        {
            "job": job,
            "running_api_picks_up_within_seconds": (
                RECONCILE_INTERVAL_SECONDS if scheduled else None
            ),
        }
    )


async def cmd_job_run(ctx: Context, args: argparse.Namespace) -> Outcome:
    """POST /jobs/{--job}/trigger {"dry_run": false}: the run's id,
    status and statistics (counts and codes); never its `log_output`, which
    may name recipients, and of an error its class only."""
    body = JobTriggerRequest(dry_run=False)

    async def trigger(db: AsyncSession, agent: Agent) -> dict[str, Any]:
        run = await JobsManager(db).trigger_job(agent, args.job, body.dry_run, ctx.sync_sessions())
        config = (
            await db.execute(
                select(JobConfig)
                .where(JobConfig.job_id == args.job)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        return {
            "run": {
                "id": run.id,
                "job_id": run.job_id,
                "status": run.status,
                "triggered_by": run.triggered_by,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "duration_seconds": run.duration_seconds,
                "stats": dict(run.stats or {}),
                "error_type": run.error.split(":", 1)[0].strip() if run.error else None,
            },
            "job": {"is_enabled": config.is_enabled, "paused_until": config.paused_until},
        }

    result = await through(ctx, JOB_TRIGGER, trigger)
    status = result["run"]["status"]
    if status == "skipped":
        job = result["job"]
        return Outcome(
            result,
            code="script.job_skipped",
            params={"is_enabled": job["is_enabled"], "paused": job["paused_until"] is not None},
        )
    if status != "success":
        return Outcome(result, code="script.job_failed", params={"status": status})
    return Outcome(result)


# ── platform-roles-audit ────────────────────────────────────────────────────


async def roles_audit(db: AsyncSession) -> dict[str, Any]:
    """Whether the flaw of the superadmin role (closed on 08/10/2026) was
    used, by ids and counts: the platform role's clones and their holders,
    the other roles still holding a platform permission, the invitations
    naming the platform role or a clone, the logged impersonations of an
    agent on one of them (its role today: the log keeps none)."""
    platform_ids = list(
        (
            await db.execute(
                select(Role.id).where(
                    Role.is_system.is_(True),
                    Role.agency_id.is_(None),
                    Role.name.in_(PLATFORM_ROLE_NAMES),
                )
            )
        )
        .scalars()
        .all()
    )
    clones = (
        await db.execute(
            select(Role.id, Role.agency_id, Agency.slug, Role.created_at)
            .outerjoin(Agency, Agency.id == Role.agency_id)
            .where(Role.cloned_from_role_id.in_(platform_ids))
            .order_by(Role.created_at, Role.id)
        )
    ).all()
    clone_ids = [row[0] for row in clones]
    granted = (
        await db.execute(
            select(Role.id, Role.agency_id, Role.is_system, PermissionRow.key)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(PermissionRow, PermissionRow.id == RolePermission.permission_id)
            .where(
                PermissionRow.key.in_(sorted(PLATFORM_PERMISSION_KEYS)),
                Role.id.not_in(platform_ids),
            )
            .order_by(Role.id, PermissionRow.key)
        )
    ).all()
    granted_roles: dict[uuid.UUID, dict[str, Any]] = {}
    for role_id, agency_id, is_system, key in granted:
        entry = granted_roles.setdefault(
            role_id,
            {"role_id": role_id, "agency_id": agency_id, "is_system": is_system, "permissions": []},
        )
        entry["permissions"].append(key)
    watched = [*platform_ids, *clone_ids, *granted_roles]
    holders: dict[uuid.UUID, tuple[int, int]] = {}
    if watched:
        for role_id, total, active in (
            await db.execute(
                select(
                    Agent.role_id,
                    func.count(),
                    func.count().filter(Agent.deactivated_at.is_(None)),
                )
                .where(Agent.role_id.in_(watched))
                .group_by(Agent.role_id)
            )
        ).all():
            holders[role_id] = (int(total), int(active))
    invitations: dict[str, Counter[str]] = {"platform_role": Counter(), "clones": Counter()}
    named = [*platform_ids, *clone_ids]
    if named:
        for role_id, status, count in (
            await db.execute(
                select(AgentInvitation.role_id, AgentInvitation.status, func.count())
                .where(AgentInvitation.role_id.in_(named))
                .group_by(AgentInvitation.role_id, AgentInvitation.status)
            )
        ).all():
            kind = "platform_role" if role_id in platform_ids else "clones"
            invitations[kind][status] += count
    target = aliased(Agent)
    logged = (
        (
            await db.execute(
                select(
                    ImpersonationLog.created_at,
                    ImpersonationLog.impersonator_agent_id,
                    ImpersonationLog.target_id,
                    target.role_id,
                )
                .join(target, target.id == ImpersonationLog.target_id)
                .where(
                    ImpersonationLog.target_type == ActorType.AGENT.value,
                    target.role_id.in_(named),
                )
                .order_by(ImpersonationLog.created_at, ImpersonationLog.id)
            )
        ).all()
        if named
        else []
    )
    impersonators = {row[1] for row in logged}
    their_roles: dict[uuid.UUID, uuid.UUID] = {}
    if impersonators:
        their_roles = {
            agent_id: role_id
            for agent_id, role_id in (
                await db.execute(select(Agent.id, Agent.role_id).where(Agent.id.in_(impersonators)))
            ).all()
        }
    events = [
        {
            "at": at,
            "impersonator_agent_id": impersonator_id,
            "target_agent_id": target_id,
            "target_role": "platform_role" if role_id in platform_ids else "clone",
            "by_platform_operator": their_roles.get(impersonator_id) in platform_ids,
        }
        for at, impersonator_id, target_id, role_id in logged
    ]
    clone_rows = [
        {
            "role_id": role_id,
            "agency_id": agency_id,
            "agency_slug": slug,
            "created_at": created_at,
            "agents": holders.get(role_id, (0, 0))[0],
            "active_agents": holders.get(role_id, (0, 0))[1],
        }
        for role_id, agency_id, slug, created_at in clones
    ]
    granted_rows = [
        {
            **entry,
            "agents": holders.get(role_id, (0, 0))[0],
            "active_agents": holders.get(role_id, (0, 0))[1],
        }
        for role_id, entry in granted_roles.items()
    ]
    return {
        "platform_role_ids": platform_ids,
        "platform_role_holders": {
            "agents": sum(holders.get(role_id, (0, 0))[0] for role_id in platform_ids),
            "active_agents": sum(holders.get(role_id, (0, 0))[1] for role_id in platform_ids),
        },
        "clones": clone_rows,
        "roles_with_platform_permissions": granted_rows,
        "invitations": {kind: dict(sorted(counts.items())) for kind, counts in invitations.items()},
        "impersonations": {
            "count": len(events),
            "by_others": sum(1 for event in events if not event["by_platform_operator"]),
            "first_at": events[0]["at"] if events else None,
            "last_at": events[-1]["at"] if events else None,
            "events": events,
        },
        "summary": {
            "clones": len(clone_rows),
            "clone_holders": sum(row["agents"] for row in clone_rows),
            "roles_with_platform_permissions": len(granted_rows),
            "invitations_naming_platform_roles": sum(
                sum(counts.values()) for counts in invitations.values()
            ),
            "impersonations_of_platform_role_holders": len(events),
        },
    }


async def cmd_platform_roles_audit(ctx: Context, args: argparse.Namespace) -> Outcome:
    """Read only, no route: the platform's superadmin only."""
    return Outcome(await as_platform(ctx, roles_audit))


# ── commands ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    run: Callable[[Context, argparse.Namespace], Awaitable[Outcome]]
    #: The routes the command stands for, given its options (empty: none,
    #: the platform's superadmin only).
    routes: Callable[[argparse.Namespace], tuple[Route, ...]]
    options: Callable[[argparse.ArgumentParser], None] = lambda _parser: None


def _routes(*routes: Route) -> Callable[[argparse.Namespace], tuple[Route, ...]]:
    return lambda _args: routes


def _agency_options(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--agency", metavar="SLUG", help="the agency's slug")
    target.add_argument("--own-agency", action="store_true", help="the agency of the --as agent")


def _webhook_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--recreate", action="store_true", help="delete then create it again (secret rotation)"
    )


def _import_apply_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--campaign",
        action="append",
        default=[],
        metavar="LEMLIST_ID",
        help="a Lemlist campaign of the plan (repeated); default: every importable one",
    )
    parser.add_argument(
        "--accept-conflicts", action="store_true", help="import even if the plan counts conflicts"
    )


def _smoke_start_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--to", action="append", required=True, metavar="EMAIL", help="a recipient (repeated)"
    )
    parser.add_argument("--name", help="the campaign's name")
    parser.add_argument(
        "--followup-days", type=int, metavar="N", help="add a follow-up N days after"
    )
    parser.add_argument("--url", help="the link of the first email")
    parser.add_argument(
        "--sender", metavar="USER_ID", help="the mailbox's Lemlist user id, among several"
    )


def _job_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--job",
        choices=(SYNC_JOB_ID, AUTO_IMPORT_JOB_ID),
        default=SYNC_JOB_ID,
        help="the Email job (default: email_sync)",
    )


def _campaign_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--campaign", type=uuid.UUID, required=True, metavar="ID", help="the Nidria campaign id"
    )


def _smoke_status_options(parser: argparse.ArgumentParser) -> None:
    _campaign_options(parser)
    parser.add_argument(
        "--sync", action="store_true", help="run the campaign's Sync first (reads Lemlist)"
    )


def _smoke_status_routes(args: argparse.Namespace) -> tuple[Route, ...]:
    reads = (CAMPAIGN_READ, MEMBERS)
    return (*reads, CAMPAIGN_SYNC) if args.sync else reads


COMMANDS: dict[str, Command] = {
    command.name: command
    for command in (
        Command(
            "status",
            "the platform's Lemlist account (read only)",
            cmd_status,
            _routes(PROVIDER_ACCOUNT),
        ),
        Command(
            "webhook-create",
            "the team's webhook (idempotent)",
            cmd_webhook_create,
            _routes(PROVIDER_WEBHOOK),
            _webhook_options,
        ),
        Command(
            "agency-enable",
            "switch the Email module on for an agency",
            cmd_agency_enable,
            _routes(AGENCY_MODULE),
            _agency_options,
        ),
        Command(
            "bind-team",
            "bind an agency to the key's Lemlist team",
            cmd_bind_team,
            _routes(AGENCY_TEAM),
            _agency_options,
        ),
        Command(
            "import-plan",
            "what importing the running Lemlist campaigns would do",
            cmd_import_plan,
            _routes(IMPORT_PLAN),
        ),
        Command(
            "import-apply",
            "import the running Lemlist campaigns",
            cmd_import_apply,
            _routes(IMPORT_PLAN, IMPORT_APPLY),
            _import_apply_options,
        ),
        Command(
            "import-verify",
            "Nidria's counts against Lemlist's, per imported campaign",
            cmd_import_verify,
            _routes(IMPORT_PLAN, CAMPAIGNS),
        ),
        Command(
            "smoke-start",
            "create and launch a smoke test campaign",
            cmd_smoke_start,
            _routes(
                SENDERS,
                CAMPAIGN_CREATE,
                STEP_CREATE,
                PROFILE_CREATE,
                MEMBERS_ADD,
                CAMPAIGN_UPDATE,
                CAMPAIGN_SYNC,
                MEMBERS,
                CAMPAIGN_LAUNCH,
                CAMPAIGN_READ,
            ),
            _smoke_start_options,
        ),
        Command(
            "smoke-status",
            "a smoke test campaign's state (Nidria only, unless --sync)",
            cmd_smoke_status,
            _smoke_status_routes,
            _smoke_status_options,
        ),
        Command(
            "smoke-cleanup",
            "remove, archive, delete what a smoke test created",
            cmd_smoke_cleanup,
            _routes(CAMPAIGN_READ, MEMBERS_REMOVE, CAMPAIGN_UPDATE, PROFILE_DELETE),
            _campaign_options,
        ),
        Command(
            "job-enable",
            "switch an Email job on (email_sync unless --job)",
            cmd_job_enable,
            _routes(JOB_UPDATE),
            _job_options,
        ),
        Command(
            "job-run",
            "run an Email job now (email_sync unless --job)",
            cmd_job_run,
            _routes(JOB_TRIGGER),
            _job_options,
        ),
        Command(
            "platform-roles-audit",
            "whether the superadmin role flaw was used (read only)",
            cmd_platform_roles_audit,
            _routes(),
        ),
    )
}


class _Parser(argparse.ArgumentParser):
    """A usage error is the script's refusal (exit 2), its message dropped
    when it quotes an argument carrying an @."""

    def error(self, message: str) -> NoReturn:
        raise Refusal(
            "script.usage",
            {"message": message if "@" not in message else "invalid arguments"},
            exit_code=EXIT_USAGE,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="email_admin.py",
        description="Administration of the Email module (see the script's docstring).",
    )
    parser.add_argument(
        "--as",
        dest="as_email",
        required=True,
        metavar="EMAIL",
        help="the agent the script acts as (never printed)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for command in COMMANDS.values():
        command.options(commands.add_parser(command.name, help=command.help))
    return parser


@cache
def _default_sync_sessions() -> sessionmaker[Session]:
    return make_session_local()


async def run(
    argv: Sequence[str],
    *,
    out: TextIO | None = None,
    sessions: async_sessionmaker[AsyncSession] | None = None,
    sync_sessions: sessionmaker[Session] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """One command, its document printed; returns the exit code. Tests give
    their own sessions, environment and stream."""
    stream = out if out is not None else sys.stdout
    document: dict[str, Any] = {"ok": False, "command": None, "actor": None, "result": None}
    exit_code = EXIT_INTERNAL
    with _silenced():
        try:
            args = build_parser().parse_args(list(argv))
            command = COMMANDS[args.command]
            document["command"] = command.name
            env = os.environ if environ is None else environ
            reason = production_guard(get_settings().environment, env.get("FLY_APP_NAME"))
            if reason is not None:
                raise Refusal("script.production_guard", {"reason": reason}, exit_code=EXIT_USAGE)
            factory = sessions if sessions is not None else async_session_maker
            async with factory() as db:
                actor = await find_actor(db, args.as_email)
            document["actor"] = {"agent_id": actor.agent_id, "agency": actor.agency()}
            routes = command.routes(args)
            ctx = Context(
                sessions=factory,
                sync_sessions=(
                    (lambda: sync_sessions) if sync_sessions is not None else _default_sync_sessions
                ),
                actor=actor,
                routes=frozenset(routes),
            )
            if routes:
                await preflight(ctx)
            outcome = await command.run(ctx, args)
            document["result"] = outcome.result
            if outcome.code is None:
                document["ok"] = True
                exit_code = EXIT_OK
            else:
                document.update(code=outcome.code, params=outcome.params)
                exit_code = EXIT_REFUSED
        except Refusal as exc:
            document.update(result=exc.result, code=exc.code, params=safe_params(exc.params))
            exit_code = exc.exit_code
        except NidriaError as exc:
            document.update(code=exc.code, params=safe_params(exc.params))
            exit_code = EXIT_REFUSED
        except PydanticValidationError as exc:
            # Where the input was refused, never its value.
            fields = sorted(
                {".".join(str(part) for part in error["loc"]) for error in exc.errors()}
            )
            document.update(code="script.invalid_input", params={"fields": fields})
            exit_code = EXIT_USAGE
        except Exception as exc:  # noqa: BLE001 (the boundary: a class name, never a message)
            document.update(code="script.internal_error", params={"type": type(exc).__name__})
            exit_code = EXIT_INTERNAL
    return _print(stream, document, exit_code)


def main() -> int:
    # The API's engine echoes every statement with its values in
    # development: never in this script's output.
    engine.echo = False

    async def _main() -> int:
        try:
            return await run(sys.argv[1:])
        finally:
            await engine.dispose()

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
