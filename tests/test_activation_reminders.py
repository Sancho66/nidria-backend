"""LOT B point 2 — la relance d'activation J+3 / J+7 (14/08).

Aucune n'existait : un client invité qui n'activait pas n'entendait plus
jamais parler de son espace, et le lien mourait à 14 jours en silence.
Ici : deux rappels, puis plus rien (on ne harcèle pas), désactivables par
agence, et jamais vers un lien déjà mort."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agent import Agent
from shared.models.invitation import CaseInvitation
from shared.models.rbac import Role
from src.cases.activation_jobs import send_activation_reminders
from src.core import email
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeCaseInvitation, MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _run(session_local: sessionmaker[Session], *, dry_run: bool = False) -> dict:
    with session_local() as db:
        return send_activation_reminders(db, log=lambda _: None, dry_run=dry_run)


async def _invited(
    db_session: AsyncSession,
    agency_id,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
    *,
    days_ago: float,
    activated: bool = False,
    email_address: str | None = None,
) -> CaseInvitation:
    expat = await make_expat_user(
        activated=activated, **({"email": email_address} if email_address else {})
    )
    case = await make_client_case(agency_id=agency_id, principal_expat_user_id=expat.id)
    return await make_case_invitation(
        case=case,
        email=expat.email,
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
        expires_at=datetime.now(UTC) + timedelta(days=14 - days_ago),
    )


async def test_j3_then_j7_then_nothing(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> None:
    """Deux rappels, pas trois : un troisième serait du harcèlement."""
    invitation = await _invited(
        db_session,
        admin.agency_id,
        make_expat_user,
        make_client_case,
        make_case_invitation,
        days_ago=3.1,
    )
    assert _run(sync_session_local)["sent"] == 1
    await db_session.refresh(invitation)
    assert invitation.activation_reminder_stage == 3
    assert len(email.outbox) == 1
    assert "espace vous attend toujours" in email.outbox[0].subject
    # L'agence devant dans le From (point 4).
    assert email.outbox[0].sender.startswith('"Test Agency"')

    # Le même jour : rien de plus (idempotence par palier).
    assert _run(sync_session_local)["sent"] == 0
    assert len(email.outbox) == 1

    # J+7 : le second, et le dernier.
    invitation.created_at = datetime.now(UTC) - timedelta(days=7.1)
    await db_session.commit()
    assert _run(sync_session_local)["sent"] == 1
    await db_session.refresh(invitation)
    assert invitation.activation_reminder_stage == 7

    invitation.created_at = datetime.now(UTC) - timedelta(days=30)
    await db_session.commit()
    assert _run(sync_session_local)["sent"] == 0  # plus jamais
    assert len(email.outbox) == 2


async def test_never_on_a_dead_link_nor_an_active_account(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> None:
    """Relancer vers un lien mort enverrait le client dans le mur (c'est le
    geste public qui répare ça) ; relancer un compte actif n'a aucun sens."""
    expat = await make_expat_user(activated=False, email="mort@example.com")
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    await make_case_invitation(
        case=case,
        email=expat.email,
        created_at=datetime.now(UTC) - timedelta(days=38),
        expires_at=datetime.now(UTC) - timedelta(days=24),  # mort
    )
    await _invited(
        db_session,
        admin.agency_id,
        make_expat_user,
        make_client_case,
        make_case_invitation,
        days_ago=5,
        activated=True,
        email_address="actif@example.com",
    )

    assert _run(sync_session_local)["sent"] == 0
    assert email.outbox == []


async def test_an_agency_can_turn_them_off(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> None:
    agency = await make_agency(settings={"activation_reminders_enabled": False})
    await make_agent(agency_id=agency.id, role=system_roles["admin"])
    await _invited(
        db_session,
        agency.id,
        make_expat_user,
        make_client_case,
        make_case_invitation,
        days_ago=4,
    )
    stats = _run(sync_session_local)
    assert stats["due"] == 1 and stats["sent"] == 0 and stats["skipped_disabled"] == 1
    assert email.outbox == []


async def test_the_setting_is_served_and_written(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    assert (await client.get("/agencies/me", headers=headers)).json()[
        "activation_reminders_enabled"
    ] is True  # défaut : on veut que les clients entrent
    patched = await client.patch(
        "/agencies/me", headers=headers, json={"activation_reminders_enabled": False}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["activation_reminders_enabled"] is False
    assert (await client.get("/agencies/me", headers=headers)).json()[
        "activation_reminders_enabled"
    ] is False


async def test_dry_run_sends_nothing_and_marks_nothing(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> None:
    invitation = await _invited(
        db_session,
        admin.agency_id,
        make_expat_user,
        make_client_case,
        make_case_invitation,
        days_ago=8,
    )
    stats = _run(sync_session_local, dry_run=True)
    assert stats["sent"] == 1 and stats["dry_run"] is True
    assert email.outbox == []
    await db_session.refresh(invitation)
    assert invitation.activation_reminder_stage == 0
