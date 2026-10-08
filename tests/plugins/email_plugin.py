"""Email module harness: row factories, the module's env switches, a
`GET /team` double for the Lemlist team lookup, the provider double of the
manager (`fake_email_provider`) and a two-agency world for the agent routes.

Factories write rows straight to the database (`make_*(**overrides)`), so a
test can stand a campaign, a membership, a render, a message, an event or an
entry of the opposition list up without the agent routes (the provider's
events are phase 4's ingestion, which writes the list itself). The
conftest forces Lemlist mocked and real effects off; the `lemlist_live`
fixture is the ONLY way a test turns them on, and it installs the `GET /team`
double in the same gesture; `fake_email_provider` replaces the manager's provider
by an in-process double. No test can reach the network.
"""

import os
import uuid
from collections.abc import Awaitable, Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agency_email_module import AgencyEmailModule
from shared.models.agent import Agent
from shared.models.company_profile import CompanyProfile, CompanyProfileRole
from shared.models.email_campaign import (
    EmailCampaign,
    EmailCampaignAiVariable,
    EmailCampaignMember,
    EmailCampaignStep,
    EmailMemberRender,
    EmailMessage,
    EmailProviderEvent,
    EmailSuppression,
)
from shared.models.rbac import Role
from src.core.config import get_settings
from src.email_campaigns import email_manager, lemlist_team
from src.email_campaigns.providers.base import EmailProvider
from src.email_campaigns.suppression import email_hash
from tests.email_fakes import FakeProvider
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.client_profile_plugin import MakeClientProfile

#: A fake key: never a real one, it only has to be non-empty.
FAKE_LEMLIST_KEY = "test-lemlist-key"
TEAM_A = "tea_test_team_a"
TEAM_B = "tea_test_team_b"

MakeAgencyEmailModule = Callable[..., Awaitable[AgencyEmailModule]]
MakeEmailCampaign = Callable[..., Awaitable[EmailCampaign]]
MakeEmailStep = Callable[..., Awaitable[EmailCampaignStep]]
MakeEmailAiVariable = Callable[..., Awaitable[EmailCampaignAiVariable]]
MakeEmailMember = Callable[..., Awaitable[EmailCampaignMember]]
MakeEmailRender = Callable[..., Awaitable[EmailMemberRender]]
MakeEmailMessage = Callable[..., Awaitable[EmailMessage]]
MakeEmailEvent = Callable[..., Awaitable[EmailProviderEvent]]
MakeEmailSuppression = Callable[..., Awaitable[EmailSuppression]]
MakeCompanyLink = Callable[..., Awaitable[CompanyProfile]]


# --- env switches -----------------------------------------------------------------


@contextmanager
def email_env(**values: str) -> Iterator[None]:
    """Set env variables for the block, then restore them EXACTLY and drop
    the settings cache on both sides. Restoring before the cache is cleared
    keeps a later `get_settings()` from caching the test's values."""
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    get_settings.cache_clear()
    try:
        yield
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        get_settings.cache_clear()


@pytest.fixture
def email_module_on() -> Generator[None, None, None]:
    """The platform's master switch on (EMAIL_MODULE_ENABLED=true)."""
    with email_env(EMAIL_MODULE_ENABLED="true"):
        yield


class LemlistTeamDouble:
    """Answers `GET /team` like Lemlist, through an `httpx.MockTransport`:
    no socket is opened, so the network guard counts nothing. Every request
    is recorded; `status_code` or `error` simulate an outage."""

    def __init__(self) -> None:
        self.team_id: str = TEAM_A
        self.name: str | None = "Nidria test team"
        self.status_code = 200
        self.error: Exception | None = None
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"error": "simulated"})
        body: dict[str, Any] = {"_id": self.team_id}
        if self.name is not None:
            body["name"] = self.name
        return httpx.Response(200, json=body)


@pytest.fixture
def lemlist_team_double() -> Generator[LemlistTeamDouble, None, None]:
    """The `GET /team` double alone (settings untouched), cache cleared."""
    double = LemlistTeamDouble()
    lemlist_team.clear_cache()
    lemlist_team.transport_override = double.transport
    try:
        yield double
    finally:
        lemlist_team.transport_override = None
        lemlist_team.clear_cache()


@pytest.fixture
def lemlist_live(
    lemlist_team_double: LemlistTeamDouble,
) -> Generator[LemlistTeamDouble, None, None]:
    """Real effects on, Lemlist unmocked, a (fake) key: what production looks
    like once the key arrives. The `GET /team` double answers instead of
    Lemlist."""
    with email_env(
        EMAIL_PROVIDER_EXTERNAL_EFFECTS="true",
        MOCK_LEMLIST="false",
        LEMLIST_API_KEY=FAKE_LEMLIST_KEY,
    ):
        yield lemlist_team_double


# --- row factories ------------------------------------------------------------------


@pytest_asyncio.fixture
async def make_agency_email_module(db_session: AsyncSession) -> MakeAgencyEmailModule:
    async def _make(*, agency_id: uuid.UUID, **overrides: Any) -> AgencyEmailModule:
        data: dict[str, Any] = {"enabled": True, **overrides}
        if data["enabled"] and "enabled_at" not in data:
            data["enabled_at"] = datetime.now(UTC)
        row = AgencyEmailModule(agency_id=agency_id, **data)
        db_session.add(row)
        await db_session.commit()
        await db_session.refresh(row)
        return row

    return _make


@pytest_asyncio.fixture
async def make_email_campaign(
    db_session: AsyncSession, make_agency: MakeAgency
) -> MakeEmailCampaign:
    async def _make(**overrides: Any) -> EmailCampaign:
        data: dict[str, Any] = {"name": "Spring outreach", **overrides}
        if "agency_id" not in data:
            data["agency_id"] = (await make_agency()).id
        campaign = EmailCampaign(**data)
        db_session.add(campaign)
        await db_session.commit()
        await db_session.refresh(campaign)
        return campaign

    return _make


@pytest_asyncio.fixture
async def make_email_step(
    db_session: AsyncSession, make_email_campaign: MakeEmailCampaign
) -> MakeEmailStep:
    async def _make(**overrides: Any) -> EmailCampaignStep:
        data: dict[str, Any] = {
            "position": 1,
            "name": "First email",
            "subject_template": "Hello {{client.first_name}}",
            "body_template": "A word from your agency.",
            **overrides,
        }
        if "campaign_id" not in data:
            data["campaign_id"] = (await make_email_campaign()).id
        step = EmailCampaignStep(**data)
        db_session.add(step)
        await db_session.commit()
        await db_session.refresh(step)
        return step

    return _make


@pytest_asyncio.fixture
async def make_email_ai_variable(
    db_session: AsyncSession, make_email_step: MakeEmailStep
) -> MakeEmailAiVariable:
    async def _make(**overrides: Any) -> EmailCampaignAiVariable:
        data: dict[str, Any] = {
            "key": "opener",
            "prompt_template": "One friendly opening line for {{client.first_name}}.",
            **overrides,
        }
        if "campaign_step_id" not in data:
            data["campaign_step_id"] = (await make_email_step()).id
        variable = EmailCampaignAiVariable(**data)
        db_session.add(variable)
        await db_session.commit()
        await db_session.refresh(variable)
        return variable

    return _make


@pytest_asyncio.fixture
async def make_email_member(
    db_session: AsyncSession,
    make_email_campaign: MakeEmailCampaign,
    make_client_profile: MakeClientProfile,
) -> MakeEmailMember:
    """A membership. Without `campaign_id`, a fresh campaign; without
    `client_profile_id`, a fresh profile IN the campaign's agency (a profile
    and its campaign always share the agency)."""

    async def _make(**overrides: Any) -> EmailCampaignMember:
        data: dict[str, Any] = dict(overrides)
        if "campaign_id" not in data:
            campaign = await make_email_campaign(
                **({"agency_id": data["agency_id"]} if "agency_id" in data else {})
            )
            data["campaign_id"] = campaign.id
            data.setdefault("agency_id", campaign.agency_id)
        if "agency_id" not in data:
            campaign_row = await db_session.get(EmailCampaign, data["campaign_id"])
            assert campaign_row is not None
            data["agency_id"] = campaign_row.agency_id
        if "client_profile_id" not in data:
            data["client_profile_id"] = (await make_client_profile(agency_id=data["agency_id"])).id
        member = EmailCampaignMember(**data)
        db_session.add(member)
        await db_session.commit()
        await db_session.refresh(member)
        return member

    return _make


@pytest_asyncio.fixture
async def make_email_render(
    db_session: AsyncSession, make_email_member: MakeEmailMember
) -> MakeEmailRender:
    async def _make(**overrides: Any) -> EmailMemberRender:
        data: dict[str, Any] = {
            "step_position": 1,
            "subject": "Hello Claire",
            "body": "A word from your agency.",
            "template_version": 1,
            "template_hash": "0" * 64,
            **overrides,
        }
        if "campaign_member_id" not in data:
            data["campaign_member_id"] = (await make_email_member()).id
        render = EmailMemberRender(**data)
        db_session.add(render)
        await db_session.commit()
        await db_session.refresh(render)
        return render

    return _make


@pytest_asyncio.fixture
async def make_email_message(
    db_session: AsyncSession, make_email_member: MakeEmailMember
) -> MakeEmailMessage:
    async def _make(**overrides: Any) -> EmailMessage:
        data: dict[str, Any] = {
            "provider": "LEMLIST",
            "direction": "OUTBOUND",
            "status": "SENT",
            "content_state": "RENDERED",
            "step_position": 1,
            **overrides,
        }
        if "campaign_member_id" not in data:
            member = await make_email_member()
            data["campaign_member_id"] = member.id
            data.setdefault("campaign_id", member.campaign_id)
            data.setdefault("agency_id", member.agency_id)
        message = EmailMessage(**data)
        db_session.add(message)
        await db_session.commit()
        await db_session.refresh(message)
        return message

    return _make


@pytest_asyncio.fixture
async def make_email_event(
    db_session: AsyncSession, make_email_member: MakeEmailMember
) -> MakeEmailEvent:
    """A provider event of a membership (its campaign and agency by default)."""

    async def _make(**overrides: Any) -> EmailProviderEvent:
        data: dict[str, Any] = {
            "provider": "LEMLIST",
            "provider_event_id": f"act_{uuid.uuid4().hex[:12]}",
            "type": "SENT",
            "provider_type": "emailsSent",
            "step_position": 1,
            "payload": {},
            "occurred_at": datetime.now(UTC),
            **overrides,
        }
        if "campaign_member_id" not in data:
            member = await make_email_member()
            data["campaign_member_id"] = member.id
            data.setdefault("campaign_id", member.campaign_id)
            data.setdefault("agency_id", member.agency_id)
        if "agency_id" not in data or "campaign_id" not in data:
            member_row = await db_session.get(EmailCampaignMember, data["campaign_member_id"])
            assert member_row is not None
            data.setdefault("campaign_id", member_row.campaign_id)
            data.setdefault("agency_id", member_row.agency_id)
        event = EmailProviderEvent(**data)
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)
        return event

    return _make


@pytest_asyncio.fixture
async def make_email_suppression(db_session: AsyncSession) -> MakeEmailSuppression:
    """An address on an agency's opposition list, as an applied unsubscribe
    or bounce leaves it: `email` is fingerprinted here, never stored."""

    async def _make(
        *,
        agency_id: uuid.UUID,
        email: str,
        reason: str = "UNSUBSCRIBED",
        source_member_id: uuid.UUID | None = None,
    ) -> EmailSuppression:
        digest = email_hash(email)
        assert digest is not None
        row = EmailSuppression(
            agency_id=agency_id,
            email_hash=digest,
            reason=reason,
            source_member_id=source_member_id,
        )
        db_session.add(row)
        await db_session.commit()
        await db_session.refresh(row)
        return row

    return _make


@pytest_asyncio.fixture
async def make_company_link(db_session: AsyncSession) -> MakeCompanyLink:
    """A company of the client's agency, linked to the client by
    `company_profile_role` (what `company.*` variables read)."""

    async def _make(
        *, client_profile_id: uuid.UUID, agency_id: uuid.UUID, role: str = "manager", **values: Any
    ) -> CompanyProfile:
        data: dict[str, Any] = {"name": "Acme Relocation", "custom_fields": {}, **values}
        company = CompanyProfile(agency_id=agency_id, **data)
        db_session.add(company)
        await db_session.flush()
        db_session.add(
            CompanyProfileRole(
                company_profile_id=company.id, client_profile_id=client_profile_id, role=role
            )
        )
        await db_session.commit()
        await db_session.refresh(company)
        return company

    return _make


# --- the manager's provider -----------------------------------------------------------


@pytest.fixture
def fake_email_provider(monkeypatch: pytest.MonkeyPatch) -> Generator[FakeProvider, None, None]:
    """The agency's provider replaced by `FakeProvider` (external, team
    `tea_current`), whatever the settings: `email_manager.provider_for` is
    monkeypatched with the registry's signature `(settings, row)`. The
    manager's caches are cleared on both sides."""
    fake = FakeProvider()

    def _provider_for(_settings: Any, _row: AgencyEmailModule | None) -> EmailProvider:
        return fake

    monkeypatch.setattr(email_manager, "provider_for", _provider_for)
    email_manager.clear_caches()
    try:
        yield fake
    finally:
        email_manager.clear_caches()


# --- two agencies with the module on --------------------------------------------------


@dataclass
class EmailWorld:
    """Two agencies with the Email module on, an admin in each. Ids and
    headers are plain values: a test's request that rolls the shared session
    back never expires them."""

    agency_id: uuid.UUID
    other_agency_id: uuid.UUID
    admin_id: uuid.UUID
    headers: dict[str, str]
    other_headers: dict[str, str]


@pytest_asyncio.fixture
async def email_world(
    email_module_on: None,
    system_roles: dict[str, Role],
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    make_agency_email_module: MakeAgencyEmailModule,
) -> EmailWorld:
    email_manager.clear_caches()
    agency: Agency = await make_agency(name="Relocation Lisboa")
    other: Agency = await make_agency(name="Expat Porto")
    admin: Agent = await make_agent(agency_id=agency.id, role=system_roles["admin"])
    other_admin: Agent = await make_agent(agency_id=other.id, role=system_roles["admin"])
    await make_agency_email_module(agency_id=agency.id)
    await make_agency_email_module(agency_id=other.id)
    return EmailWorld(
        agency_id=agency.id,
        other_agency_id=other.id,
        admin_id=admin.id,
        headers=agent_headers(admin),
        other_headers=agent_headers(other_admin),
    )
