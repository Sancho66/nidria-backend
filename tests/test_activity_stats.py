"""Volet 2 (31/07) — KPI de travail accompli, derrière KPI_ENABLED.

Les 4 KPI v1 dérivés à la lecture (extraction 30/07) : étapes franchies
(colonne, l'auto exclu via completed_by NULL), pièces validées (log),
dossiers clôturés (log, new=closed), relances manuelles approuvées (log +
jointure reminder — les autos exclues par auto_threshold_days). Agence +
« moi » dans la même réponse, bornes CALENDAIRES UTC, DEUX requêtes
(témoin de comptage), flag off → 409."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest.fixture
def kpi_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KPI_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _log(case_id, actor_id, action_type, details, created_at) -> ActivityLog:
    row = ActivityLog(
        case_id=case_id,
        actor_type="agent",
        actor_id=actor_id,
        action_type=action_type,
        details=details,
    )
    row.created_at = created_at
    return row


async def _seed(
    db: AsyncSession,
    admin: Agent,
    other: Agent,
    make_client_case,
    make_expat_user,
) -> uuid.UUID:
    """Le jeu synthétique : aujourd'hui, hier (dans la semaine), il y a 10
    jours (hors semaine) — pour CHAQUE KPI, du « moi » (admin), de l'autre
    agent, et de l'exclu (auto/système)."""
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    old = now - timedelta(days=10)
    principal = await make_expat_user(activated=True, email="kpi-p@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    case_id = case.id
    journey = JourneyTemplate(agency_id=admin.agency_id, name="KPI")
    db.add(journey)
    await db.flush()

    def step(position: int) -> JourneyTemplateStep:
        row = JourneyTemplateStep(template_id=journey.id, name=f"S{position}", position=position)
        db.add(row)
        return row

    steps = [step(i) for i in range(5)]
    await db.flush()
    # Étapes : admin aujourd'hui, admin hier, l'autre agent aujourd'hui,
    # une AUTO (completed_by NULL) aujourd'hui — exclue, une vieille.
    completions = [
        (steps[0], admin.id, now),
        (steps[1], admin.id, yesterday),
        (steps[2], other.id, now),
        (steps[3], None, now),
        (steps[4], admin.id, old),
    ]
    for template_step, agent_id, at in completions:
        db.add(
            CaseStepProgress(
                case_id=case_id,
                template_step_id=template_step.id,
                status="done",
                completed_at=at,
                completed_by_agent_id=agent_id,
            )
        )
    # Log : validations (admin ×2 aujourd'hui, autre ×1 hier), clôture
    # (admin aujourd'hui + un status_changed non-closed exclu), relances
    # (admin approuve une MANUELLE aujourd'hui + une AUTO aujourd'hui —
    # exclue, l'autre agent une manuelle hier).
    manual = Reminder(
        case_id=case_id,
        channel="mail",
        scheduled_at=now,
        status="approved",
        recipient_type="expat",
        message_body="m",
        approved_by_agent_id=admin.id,
    )
    auto = Reminder(
        case_id=case_id,
        channel="mail",
        scheduled_at=now,
        status="approved",
        recipient_type="expat",
        message_body="a",
        approved_by_agent_id=admin.id,
        auto_threshold_days=20,
    )
    manual_other = Reminder(
        case_id=case_id,
        channel="mail",
        scheduled_at=now,
        status="approved",
        recipient_type="expat",
        message_body="o",
        approved_by_agent_id=other.id,
    )
    db.add_all([manual, auto, manual_other])
    await db.flush()
    db.add_all(
        [
            _log(case_id, admin.id, "document.validated", {"new": "ok"}, now),
            _log(case_id, admin.id, "document.validated", {"new": "to_fix"}, now),
            _log(case_id, other.id, "document.validated", {"new": "ok"}, yesterday),
            _log(case_id, admin.id, "document.validated", {"new": "ok"}, old),
            _log(
                case_id,
                admin.id,
                "case.status_changed",
                {"old": "in_progress", "new": "closed"},
                now,
            ),
            _log(
                case_id,
                admin.id,
                "case.status_changed",
                {"old": "prospect", "new": "in_progress"},
                now,
            ),
            _log(case_id, admin.id, "reminder.approved", {"reminder_id": str(manual.id)}, now),
            _log(case_id, admin.id, "reminder.approved", {"reminder_id": str(auto.id)}, now),
            _log(
                case_id,
                other.id,
                "reminder.approved",
                {"reminder_id": str(manual_other.id)},
                yesterday,
            ),
        ]
    )
    await db.commit()
    return case_id


async def test_the_four_kpis_me_vs_agency_and_period_bounds(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    kpi_enabled,
) -> None:
    other = await make_agent(agency_id=admin.agency_id, role=system_roles["admin"])
    await _seed(db_session, admin, other, make_client_case, make_expat_user)
    headers = agent_headers(admin)

    today = (await client.get("/agencies/me/activity-stats?period=today", headers=headers)).json()
    # Aujourd'hui : étapes — admin 1 + autre 1 (l'AUTO exclue) ; validations
    # admin 2 (les deux verdicts comptent : le geste de revue) ; clôture 1
    # (le status_changed non-closed exclu) ; relances — la MANUELLE admin
    # seulement (l'auto approuvée exclue).
    assert today["agency"] == {
        "steps_completed": 2,
        "documents_validated": 2,
        "cases_closed": 1,
        "manual_reminders": 1,
    }
    assert today["me"] == {
        "steps_completed": 1,
        "documents_validated": 2,
        "cases_closed": 1,
        "manual_reminders": 1,
    }

    week = (await client.get("/agencies/me/activity-stats?period=week", headers=headers)).json()
    # La semaine ajoute « hier » (dans les bornes lundi-UTC si hier est de
    # la même semaine) et JAMAIS le vieux (10 jours). Selon le jour du run,
    # hier peut tomber la semaine précédente — les bornes restent exactes :
    # on assert la fourchette structurelle.
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - timedelta(days=midnight.weekday())
    yesterday_in_week = (now - timedelta(days=1)) >= monday
    expected_steps = 3 if yesterday_in_week else 2
    expected_val = 3 if yesterday_in_week else 2
    expected_rem = 2 if yesterday_in_week else 1
    assert week["agency"] == {
        "steps_completed": expected_steps,
        "documents_validated": expected_val,
        "cases_closed": 1,
        "manual_reminders": expected_rem,
    }
    assert week["me"]["steps_completed"] == (2 if yesterday_in_week else 1)
    assert week["since"].startswith(monday.date().isoformat())


async def test_flag_off_serves_nothing(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    r = await client.get("/agencies/me/activity-stats", headers=agent_headers(admin))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "kpi.disabled"


async def test_two_queries_flat(
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le témoin : DEUX requêtes, quel que soit le volume."""
    from src.activity.activity_manager import activity_stats

    monkeypatch.setenv("KPI_ENABLED", "true")
    get_settings.cache_clear()
    other = await make_agent(agency_id=admin.agency_id, role=system_roles["admin"])
    await _seed(db_session, admin, other, make_client_case, make_expat_user)
    admin_row = admin
    engine = db_session.get_bind()
    counter = {"n": 0}

    def _count(*_a: object, **_k: object) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        await activity_stats(db_session, admin_row, "week")
    finally:
        event.remove(engine, "before_cursor_execute", _count)
        get_settings.cache_clear()
    assert counter["n"] == 2
