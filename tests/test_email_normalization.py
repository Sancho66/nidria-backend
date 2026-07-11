"""Identity email normalization + forgot-password observability (prod
incident: an account 'Contact@x' was silently unreachable by a
forgot-password typed 'contact@x' — exact case-sensitive match, silent
200 by design, zero server-side trace).

Covers: case-insensitive login and forgot-password (schema-level
NormalizedEmailStr + manager belt), lowercase persistence on every
identity write path (invitation, case principal, CSV import pivot), the
two forgot-password log branches, and the Resend message-id log on the
real send path (correlation handle with the Resend dashboard)."""

import logging

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.invitation import AgentInvitation
from shared.models.rbac import Role
from src.core import email as email_module
from tests.plugins.agent_plugin import DEFAULT_PASSWORD, AuthHeaders, MakeAgent
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.journey_plugin import MakeJourneyTemplate

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


# --- lookups are case-insensitive -------------------------------------------------------


async def test_login_accepts_any_casing(
    client: AsyncClient, make_agent: MakeAgent, system_roles: dict[str, Role]
) -> None:
    await make_agent(role=system_roles["admin"], email="casing@example.com")
    response = await client.post(
        "/auth/agent/login",
        json={"email": "  CASING@Example.COM ", "password": DEFAULT_PASSWORD},
    )
    assert response.status_code == 200, response.text


async def test_forgot_password_accepts_any_casing(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_agent(role=system_roles["admin"], email="casing@example.com")
    with caplog.at_level(logging.INFO, logger="src.auth.auth_manager"):
        response = await client.post(
            "/auth/agent/forgot-password", json={"email": "CASING@EXAMPLE.COM"}
        )
    assert response.status_code == 200
    assert [m.to for m in email_module.outbox] == ["casing@example.com"]
    assert any("reset mail sent" in r.message for r in caplog.records)


async def test_forgot_password_no_match_logs_silently(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The non-revealing 200 is for the HTTP response only: the server
    log records the no-match branch (else 'mail never arrived' is
    undiagnosable — the prod incident)."""
    with caplog.at_level(logging.INFO, logger="src.auth.auth_manager"):
        response = await client.post(
            "/auth/agent/forgot-password", json={"email": "ghost@example.com"}
        )
    assert response.status_code == 200
    assert email_module.outbox == []
    assert any("no matching agent account" in r.message for r in caplog.records)


# --- writes are lowercase ----------------------------------------------------------------


async def test_invitation_email_stored_lowercase(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    response = await client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        json={"email": "New.Member@Example.COM", "role_id": str(system_roles["member"].id)},
    )
    assert response.status_code == 201, response.text
    row = (
        await db_session.execute(
            select(AgentInvitation).where(AgentInvitation.agency_id == admin.agency_id)
        )
    ).scalar_one()
    assert row.email == "new.member@example.com"


async def test_case_principal_email_lowercased_and_deduped(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """A case created with 'CLIENT@X' links the EXISTING 'client@x'
    expat instead of minting a case-sensitive doppelganger."""
    existing = await make_expat_user(email="client@example.com")
    tid = (await client.post("/journeys", headers=agent_headers(admin), json={"name": "T"})).json()[
        "id"
    ]
    response = await client.post(
        "/cases",
        headers=agent_headers(admin),
        json={
            "first_name": "Jean",
            "last_name": "Martin",
            "email": "  CLIENT@Example.com ",
            "origin_country": "FR",
            "dest_country": "PY",
            "journey_template_id": tid,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["principal_expat_user_id"] == str(existing.id)
    emails = list(
        (
            await db_session.execute(
                select(ExpatUser.email).where(ExpatUser.email.ilike("client@example.com"))
            )
        ).scalars()
    )
    assert emails == ["client@example.com"]  # one row, lowercase


async def test_import_pivot_email_lowercased(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_journey_template: MakeJourneyTemplate,
    agent_headers: AuthHeaders,
) -> None:
    template = await make_journey_template(agency_id=admin.agency_id)
    response = await client.post(
        "/imports/cases",
        headers=agent_headers(admin),
        json={
            "journey_template_id": str(template.id),
            "csv_text": "Email,First,Last\nContact@Import-X.COM,Ana,Bo\n",
            "mapping": {"Email": "email", "First": "first_name", "Last": "last_name"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["created_count"] == 1
    row = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "contact@import-x.com"))
    ).scalar_one()
    assert row.email == "contact@import-x.com"


# --- the real send path logs the Resend message id ---------------------------------------


def test_send_email_logs_resend_message_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sent: dict[str, object] = {}

    def fake_send(payload: dict[str, object]) -> dict[str, str]:
        sent.update(payload)
        return {"id": "re_test_123"}

    monkeypatch.setattr(email_module, "_is_mocked", lambda: False)
    monkeypatch.setattr(email_module.resend.Emails, "send", staticmethod(fake_send))
    monkeypatch.setattr(email_module.resend, "api_key", "rk_test", raising=False)
    with caplog.at_level(logging.INFO, logger="src.core.email"):
        email_module.send_email("dest@example.com", "Subject", "body", "<p>body</p>")
    assert sent["to"] == ["dest@example.com"]
    record = next(r for r in caplog.records if "sent via resend" in r.message)
    assert "re_test_123" in record.getMessage()
