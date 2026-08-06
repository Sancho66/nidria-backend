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
from sqlalchemy import event, select
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
        actor_type="agent" if actor_id is not None else "system",
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
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - timedelta(days=midnight.weekday())
    prev = monday - timedelta(days=3)  # TOUJOURS la semaine précédente
    old = monday - timedelta(days=10)  # TOUJOURS avant la semaine précédente
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
    steps.append(step(5))
    await db.flush()
    completions.append((steps[5], admin.id, prev))  # semaine PRÉCÉDENTE
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
    # Temps gagné : une relance AUTO envoyée aujourd'hui + une vieille (le
    # cumul les compte toutes), un document client déposé aujourd'hui + un
    # vieux, une signature complétée aujourd'hui + une vieille — et le
    # dossier du seed porte un modèle (journey_template_id) → compté
    # « créé depuis modèle » (créé aujourd'hui).
    from shared.models.client_case import ClientCase as ClientCaseRow
    from shared.models.document import Document as DocumentRow
    from shared.models.signature import SignatureRequest

    case_obj = await db.get(ClientCaseRow, case_id)
    case_obj.journey_template_id = journey.id
    for at in (now, old):
        db.add_all(
            [
                _log(case_id, None, "reminder.sent", {"reminder_id": str(auto.id)}, at),
                DocumentRow(
                    case_id=case_id,
                    filename="piece.pdf",
                    storage_path=f"x/{uuid.uuid4()}",
                    uploaded_by_type="expat",
                    uploaded_by_id=uuid.uuid4(),
                    created_at=at,
                ),
            ]
        )
        request = SignatureRequest(
            case_id=case_id,
            case_step_progress_id=(
                await db.execute(
                    select(CaseStepProgress.id).where(CaseStepProgress.case_id == case_id).limit(1)
                )
            ).scalar_one(),
            reference="TS",
            provider="docuseal",
            level="ses",
            status="completed",
            completed_at=at,
        )
        db.add(request)
    db.add(
        DocumentRow(
            case_id=case_id,
            filename="prev.pdf",
            storage_path=f"x/{uuid.uuid4()}",
            uploaded_by_type="expat",
            uploaded_by_id=uuid.uuid4(),
            created_at=prev,
        )
    )
    # Une relance MANUELLE envoyée (exclue du temps gagné : pas auto).
    db.add(_log(case_id, None, "reminder.sent", {"reminder_id": str(manual.id)}, now))
    db.add_all(
        [
            _log(case_id, admin.id, "document.validated", {"new": "ok"}, now),
            _log(case_id, admin.id, "document.validated", {"new": "to_fix"}, now),
            _log(case_id, other.id, "document.validated", {"new": "ok"}, yesterday),
            _log(case_id, admin.id, "document.validated", {"new": "ok"}, old),
            _log(case_id, admin.id, "document.validated", {"new": "ok"}, prev),
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

    # --- Temps gagné (barème config par défaut) -----------------------------
    ts = today["time_saved"]
    by_kind = {i["kind"]: i for i in ts["period"]["items"]}
    # Aujourd'hui : 1 auto envoyée (la manuelle envoyée EXCLUE), 1 doc
    # client, 1 signature, 1 dossier à modèle.
    assert by_kind["auto_reminder_sent"]["count"] == 1
    assert by_kind["auto_reminder_sent"]["minutes_total"] == 10
    assert by_kind["client_document_collected"] == {
        "kind": "client_document_collected",
        "count": 1,
        "minutes_each": 8,
        "minutes_total": 8,
    }
    assert by_kind["signature_completed"]["minutes_total"] == 25
    assert by_kind["case_created_from_template"]["minutes_total"] == 20
    # Les trois gestes ajoutés par l'élargissement du barème : les étapes
    # du jour (admin, l'autre agent, ET l'auto — une étape que le système
    # ferme est du temps que personne n'a passé), la clôture du jour,
    # aucune fiche importée dans ce scénario.
    assert by_kind["step_completed"]["count"] == 3
    assert by_kind["step_completed"]["minutes_total"] == 15
    assert by_kind["case_closed"]["minutes_total"] == 10
    assert by_kind["profile_imported"]["count"] == 0
    assert ts["period"]["total_minutes"] == 10 + 8 + 25 + 20 + 15 + 10
    # Le CUMUL compte AUSSI les vieux (2 autos, 3 docs, 2 signatures,
    # 6 étapes dont la vieille et celle de la semaine passée).
    all_by_kind = {i["kind"]: i for i in ts["all_time"]["items"]}
    assert all_by_kind["auto_reminder_sent"]["count"] == 2
    assert all_by_kind["client_document_collected"]["count"] == 3  # + celui de la semaine passée
    assert all_by_kind["signature_completed"]["count"] == 2
    assert all_by_kind["step_completed"]["count"] == 6
    assert ts["all_time"]["total_minutes"] == 20 + 24 + 50 + 20 + 30 + 10
    # « Pour vos clients » : signatures + collecte SEULEMENT.
    assert {i["kind"] for i in ts["clients_period"]["items"]} == {
        "signature_completed",
        "client_document_collected",
    }
    assert ts["clients_all_time"]["total_minutes"] == 3 * 8 + 2 * 25

    # --- Tendance (lot 31/07) ----------------------------------------------
    # today : pas de tendance (la tendance d'un jour n'existe pas).
    assert today["daily"] is None
    assert today["previous_period"] is None
    # week : 7 points, la SOMME des jours == le total de la semaine.
    daily = week["daily"]
    assert len(daily) == 7
    for field, total_key in (
        ("steps_completed", "steps_completed"),
        ("documents_validated", "documents_validated"),
        ("cases_closed", "cases_closed"),
        ("manual_reminders", "manual_reminders"),
    ):
        assert sum(d[field] for d in daily) == week["agency"][total_key], field
    assert (
        sum(d["time_saved_minutes"] for d in daily) == week["time_saved"]["period"]["total_minutes"]
    )
    # La semaine PRÉCÉDENTE, bornée JUSTE : l'événement lundi-3j compté,
    # le lundi-10j (ancien) JAMAIS.
    prev_block = week["previous_period"]
    # L'ARÊTE DU LUNDI (rouge CI du 03/08) : un lundi, « hier » tombe dans
    # la semaine PRÉCÉDENTE — ses trois événements (étape admin, doc de
    # l'autre, relance manuelle de l'autre) s'y comptent. Aucun n'entre au
    # barème temps gagné (10 inchangé).
    prev_extra = 0 if yesterday_in_week else 1
    assert prev_block["agency"]["steps_completed"] == 1 + prev_extra
    assert prev_block["me"]["steps_completed"] == 1 + prev_extra
    assert prev_block["agency"]["documents_validated"] == 1 + prev_extra
    assert prev_block["agency"]["manual_reminders"] == 0 + prev_extra
    # Le doc client de lundi-3j (8) ET l'étape franchie ce jour-là (5) —
    # l'élargissement du barème fait entrer la seconde. « Pour vos
    # clients », lui, ne retient que la pièce : une étape est un geste
    # d'agence.
    assert prev_block["time_saved_minutes"] == 8 + 5
    assert prev_block["clients_time_saved_minutes"] == 8
    assert prev_block["until"].startswith(monday.date().isoformat())


async def test_flag_off_serves_nothing(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, monkeypatch: pytest.MonkeyPatch
) -> None:
    # setenv false explicite (pas « absent ») : hermétique au .env local,
    # que pydantic-settings fusionne — leçon du lot packs, réapprise ici
    # (KPI_ENABLED=true est apparu au .env local pour le front).
    monkeypatch.setenv("KPI_ENABLED", "false")
    get_settings.cache_clear()
    try:
        r = await client.get("/agencies/me/activity-stats", headers=agent_headers(admin))
    finally:
        get_settings.cache_clear()
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
    """LE TÉMOIN QUI COMPTE : un nombre de requêtes CONSTANT, quel que
    soit le volume. Il monte quand le barème s'élargit (une source = une
    requête), jamais quand l'agence grossit — c'est cette seconde
    propriété qu'il garde."""
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
    # 2 (KPI gestes) + 7 (temps gagné : UNE par geste du barème ; période,
    # mois et cumul sortent du MÊME select, repliés en Python). Le barème
    # est passé de 4 à 7 gestes, donc 6 → 9. Le volume, lui, n'y change
    # toujours rien : c'est ce que ce test garde.
    assert counter["n"] == 9
