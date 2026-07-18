"""Préférences notifications (audit §5, lot 2026-07-18) : l'agence règle
ce que SES clients reçoivent, chaque agent règle les siens ; le critique
part toujours ; la migration douce du flag legacy ; validation stricte."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core import email
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="prefs-client@example.com")


async def _case_with_thread(
    client: AsyncClient, headers: dict[str, str], admin: Agent, expat, make_client_case
):
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    sid = (await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S"})).json()[
        "id"
    ]
    await client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=headers,
        json={"kind": "document", "reference": "Piece", "scope": "principal"},
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    return case, steps[0]["id"]


async def _set_client_prefs(db: AsyncSession, agency_id, prefs: dict) -> None:
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    agency.settings = {**(agency.settings or {}), "notification_prefs": {"client": prefs}}
    await db.commit()


async def test_each_pref_cuts_its_own_email_only(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """requirement_request=off coupe le kickoff/activation MAIS pas les
    commentaires (et reciproquement rien d'autre)."""
    ah = agent_headers(admin)
    await _set_client_prefs(db_session, admin.agency_id, {"requirement_request": "off"})
    email.outbox.clear()
    case, pid = await _case_with_thread(client, ah, admin, expat, make_client_case)
    assert [m for m in email.outbox if m.to == expat.email] == []  # pas de kickoff
    # ... mais le commentaire, lui, part (pref comments par defaut: grouped)
    email.outbox.clear()
    await client.post(f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "hi"})
    assert len([m for m in email.outbox if m.to == expat.email]) == 1


async def test_critical_always_leaves_even_all_off(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Tout a off -> l'invitation d'agent (CRITIQUE) part quand meme : le
    critique n'apparait pas dans le modele, donc rien ne peut le couper."""
    await _set_client_prefs(
        db_session,
        admin.agency_id,
        {"requirement_request": "off", "comments": "off", "reminders": "off"},
    )
    member_role_id = str(system_roles["member"].id)
    email.outbox.clear()
    r = await client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        json={"email": "nouvel-agent@example.com", "role_id": member_role_id},
    )
    assert r.status_code in (200, 201), r.text
    assert any(m.to == "nouvel-agent@example.com" for m in email.outbox)


async def test_comments_grouped_vs_on_vs_off(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """off = jamais ; grouped = fenetre 30 min (2e absorbe) ; on = fenetre
    courte (le 2e part une fois la fenetre de 5 min expiree)."""
    from shared.models.notification_window import NotificationWindow

    ah = agent_headers(admin)
    case, pid = await _case_with_thread(client, ah, admin, expat, make_client_case)

    # off : rien
    await _set_client_prefs(db_session, admin.agency_id, {"comments": "off"})
    email.outbox.clear()
    await client.post(f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "1"})
    assert [m for m in email.outbox if m.to == expat.email] == []

    # grouped : le 1er part, le 2e est absorbe meme 6 minutes plus tard
    await _set_client_prefs(db_session, admin.agency_id, {"comments": "grouped"})
    email.outbox.clear()
    await client.post(f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "2"})
    await db_session.execute(
        update(NotificationWindow)
        .where(NotificationWindow.case_id == case.id)
        .values(last_sent_at=datetime.now(UTC) - timedelta(minutes=6))
    )
    await db_session.commit()
    await client.post(f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "3"})
    assert len([m for m in email.outbox if m.to == expat.email]) == 1

    # on : la fenetre est de 5 min -> a 6 minutes, le suivant PART
    await _set_client_prefs(db_session, admin.agency_id, {"comments": "on"})
    await db_session.execute(
        update(NotificationWindow)
        .where(NotificationWindow.case_id == case.id)
        .values(last_sent_at=datetime.now(UTC) - timedelta(minutes=6))
    )
    await db_session.commit()
    email.outbox.clear()
    await client.post(f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "4"})
    assert len([m for m in email.outbox if m.to == expat.email]) == 1


async def test_agent_pref_cuts_ready_to_validate_and_his_comments(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """L'owner coupe SES mails : le commentaire client ne le notifie plus
    — le client, lui, continue de recevoir les siens."""
    ah = agent_headers(admin)
    case, pid = await _case_with_thread(client, ah, admin, expat, make_client_case)
    await db_session.execute(
        update(Agent)
        .where(Agent.id == admin.id)
        .values(notification_prefs={"comments": "off", "ready_to_validate": "off"})
    )
    await db_session.commit()
    email.outbox.clear()
    r = await client.post(
        f"/expat/cases/{case.id}/steps/{pid}/comments",
        headers=expat_headers(expat),
        json={"body": "bonjour"},
    )
    assert r.status_code == 201, r.text
    assert [m for m in email.outbox if m.to == admin.email] == []  # sa pref le tait
    # et l'agent -> client marche toujours (la pref du CLIENT est intacte)
    email.outbox.clear()
    await client.post(f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "re"})
    assert len([m for m in email.outbox if m.to == expat.email]) == 1


async def test_soft_migration_false_becomes_off(db_session: AsyncSession, admin: Agent) -> None:
    """La migration douce : le flag legacy false -> prefs client a off,
    la cle legacy supprimee (rejoue le SQL de la migration sur la base)."""
    aid = admin.agency_id
    agency = await db_session.get(Agency, aid)
    assert agency is not None
    agency.settings = {**(agency.settings or {}), "step_notifications_enabled": False}
    await db_session.commit()
    sql = (
        "UPDATE agency SET settings = (settings - 'step_notifications_enabled') "
        "|| jsonb_build_object('notification_prefs', jsonb_build_object('client', "
        "jsonb_build_object('requirement_request', 'off', 'comments', 'off', "
        "'progress_digest', 'off'))) "
        "WHERE settings ? 'step_notifications_enabled' "
        "AND (settings ->> 'step_notifications_enabled')::boolean IS FALSE"
    )
    await db_session.execute(text(sql))
    await db_session.commit()
    db_session.expire_all()
    agency = await db_session.get(Agency, aid)
    assert agency is not None
    prefs = agency.settings["notification_prefs"]["client"]
    assert prefs["requirement_request"] == "off" and prefs["comments"] == "off"
    assert "step_notifications_enabled" not in agency.settings
    # reminders N'EST PAS coupe : le flag ne les a jamais gouvernes
    assert "reminders" not in prefs


async def test_patch_contracts_and_strict_422(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    aid, admin_id = admin.agency_id, admin.id  # avant tout expire_all
    other = await make_agent(
        role=system_roles["member"], agency_id=aid, email="collegue@example.com"
    )
    other_headers = agent_headers(other)  # avant tout expire_all
    # agence : merge partiel type
    r = await client.patch(
        "/agencies/me", headers=ah, json={"notification_prefs": {"comments": "off"}}
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    agency = await db_session.get(Agency, aid)
    assert agency is not None
    assert agency.settings["notification_prefs"]["client"]["comments"] == "off"
    # 422 : valeur hors enum, cle inconnue
    assert (
        await client.patch(
            "/agencies/me", headers=ah, json={"notification_prefs": {"comments": "loud"}}
        )
    ).status_code == 422
    assert (
        await client.patch(
            "/agencies/me", headers=ah, json={"notification_prefs": {"unknown_key": "on"}}
        )
    ).status_code == 422
    # agent : le PATCH dedie, pour lui-meme, et le /me l'expose
    r = await client.patch(
        "/profile/agent/notification-prefs", headers=ah, json={"ready_to_validate": "off"}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"comments": "grouped", "ready_to_validate": "off"}
    me = (await client.get("/auth/agent/me", headers=ah)).json()
    assert me["notification_prefs"]["ready_to_validate"] == "off"
    assert (
        await client.patch("/profile/agent/notification-prefs", headers=ah, json={"comments": "x"})
    ).status_code == 422
    # un agent ne patche pas les prefs d'un autre : la route ne vise que
    # l'acteur du token — le PATCH d'un collegue ne touche PAS admin.
    await client.patch(
        "/profile/agent/notification-prefs",
        headers=other_headers,
        json={"ready_to_validate": "on"},
    )
    db_session.expire_all()
    reloaded = await db_session.get(Agent, admin_id)
    assert reloaded is not None
    assert (reloaded.notification_prefs or {}).get("ready_to_validate") == "off"  # intact
