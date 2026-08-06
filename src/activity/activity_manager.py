import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from src.activity.activity_repository import ActivityRepository
from src.activity.activity_schema import (
    ActivityListResponse,
    ActivityLogResponse,
    ActivityStatsResponse,
)
from src.core.enums import ActorType
from src.core.exceptions import NotFoundError


class ActivityManager:
    """Audit trail writer, consumed by the domain managers.

    `log_action` only does `db.add` — NO commit: the calling manager
    commits, so the log row and the mutation it describes land in the
    SAME transaction (atomic: no mutation without its trace, no trace
    of a rolled-back mutation). Endpoints over the log arrive at
    step 13.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ActivityRepository(db)

    async def list_case_activity(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        action_types: list[str] | None,
        page: int,
        page_size: int,
    ) -> ActivityListResponse:
        """Agency-side journal (the projected timeline is the client
        view). No manual POST: the journal records facts only."""
        case = await self.repo.get_case_in_agency(agent.agency_id, case_id)
        if case is None:
            raise NotFoundError("Case not found.")
        rows, total = await self.repo.list_case_activity(case.id, action_types, page, page_size)
        return ActivityListResponse(
            items=[ActivityLogResponse.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def log_action(
        self,
        *,
        case_id: uuid.UUID,
        actor_type: ActorType,
        actor_id: uuid.UUID | None,
        action_type: str,
        details: dict[str, Any] | None = None,
    ) -> ActivityLog:
        row = ActivityLog(
            case_id=case_id,
            actor_type=actor_type.value,
            actor_id=actor_id,
            action_type=action_type,
            details=details or {},
        )
        self.db.add(row)
        return row


async def activity_stats(db: AsyncSession, agent: Agent, period: str) -> ActivityStatsResponse:
    """Volet 2 (31/07) + tendance : les 4 KPI v1, agence + « moi », le
    « temps gagné » (barème config), et pour period=week le DÉTAIL
    JOURNALIER + la SEMAINE PRÉCÉDENTE (le delta front) — le tout en SIX
    requêtes agrégées, témoin de comptage au test. Verdict de coût (lot
    tendance) : l'agrégat par jour tient dans les MÊMES requêtes — chaque
    select gagne un GROUP BY day (fenêtre élargie à 2 semaines pour les
    gestes ; les sources temps-gagné groupent TOUT l'historique par jour,
    le cumul devient une somme de lignes-jours — volume borné par les
    jours d'activité réels d'une agence). Flag KPI_ENABLED maître.

    Bornes CALENDAIRES UTC (verdict inchangé : l'agence n'a pas de fuseau
    au modèle — le jour où il existe, seules les bornes changent)."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import Integer, String, cast, func, select
    from sqlalchemy.dialects.postgresql import UUID as PgUUID

    from shared.models.activity import ActivityLog
    from shared.models.case_step_progress import CaseStepProgress
    from shared.models.client_case import ClientCase
    from shared.models.document import Document
    from shared.models.reminder import Reminder
    from shared.models.signature import SignatureRequest
    from shared.models.usage import UsageEvent
    from src.activity.activity_schema import (
        ActivityStatsResponse,
        DailyPointResponse,
        KpiBlockResponse,
        PreviousPeriodResponse,
        TimeSavedBlockResponse,
        TimeSavedItemResponse,
        TimeSavedResponse,
    )
    from src.core.config import get_settings
    from src.core.exceptions import ConflictError

    if not get_settings().kpi_enabled:
        raise ConflictError("Work KPIs are not enabled.", code="kpi.disabled")
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - timedelta(days=midnight.weekday())
    since = midnight if period == "today" else monday
    is_week = period == "week"
    prev_since = monday - timedelta(days=7)
    window_start = prev_since if is_week else since
    # LA MAILLE QUI PARLE : « 20 min cette semaine » n'impressionne
    # personne, « 14 h ce mois » se discute en comité de direction. Le mois
    # CIVIL en cours (pas 30 jours glissants) — c'est la maille dont un
    # dirigeant dispose déjà pour tout le reste.
    month_start = midnight.replace(day=1)

    def in_cur(day: datetime) -> bool:
        return day >= since

    def in_prev(day: datetime) -> bool:
        return prev_since <= day < monday

    def in_month(day: datetime) -> bool:
        return day >= month_start

    # Requête 1 — étapes franchies par JOUR (l'auto exclue), fenêtre 2 sem.
    me_filter = CaseStepProgress.completed_by_agent_id == agent.id
    step_day = func.date_trunc("day", CaseStepProgress.completed_at).label("day")
    step_rows = (
        await db.execute(
            select(
                step_day,
                func.count().label("agency_n"),
                func.count().filter(me_filter).label("me_n"),
            )
            .select_from(CaseStepProgress)
            .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                CaseStepProgress.completed_at >= window_start,
                CaseStepProgress.completed_by_agent_id.is_not(None),
            )
            .group_by(step_day)
        )
    ).all()

    # Requête 2 — le log par JOUR (validations, clôtures, relances manuelles).
    is_validated = ActivityLog.action_type == "document.validated"
    is_closed = (ActivityLog.action_type == "case.status_changed") & (
        ActivityLog.details["new"].astext == "closed"
    )
    is_manual_reminder = (
        (ActivityLog.action_type == "reminder.approved")
        & (Reminder.id.is_not(None))
        & Reminder.auto_threshold_days.is_(None)
    )
    mine = ActivityLog.actor_id == agent.id
    log_day = func.date_trunc("day", ActivityLog.created_at).label("day")
    log_rows = (
        await db.execute(
            select(
                log_day,
                func.count().filter(is_validated).label("val_a"),
                func.count().filter(is_validated & mine).label("val_m"),
                func.count().filter(is_closed).label("clo_a"),
                func.count().filter(is_closed & mine).label("clo_m"),
                func.count().filter(is_manual_reminder).label("rem_a"),
                func.count().filter(is_manual_reminder & mine).label("rem_m"),
            )
            .select_from(ActivityLog)
            .join(ClientCase, ClientCase.id == ActivityLog.case_id)
            .outerjoin(
                Reminder,
                (ActivityLog.action_type == "reminder.approved")
                & (
                    Reminder.id
                    == cast(cast(ActivityLog.details["reminder_id"].astext, String), PgUUID)
                ),
            )
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                ActivityLog.created_at >= window_start,
                ActivityLog.actor_type == "agent",
                ActivityLog.action_type.in_(
                    ("document.validated", "case.status_changed", "reminder.approved")
                ),
            )
            .group_by(log_day)
        )
    ).all()

    # Requêtes 3-6 — temps gagné : TOUT l'historique groupé par jour (le
    # cumul = la somme des lignes-jours), une requête par table source.
    auto_day = func.date_trunc("day", ActivityLog.created_at).label("day")
    auto_rows = (
        await db.execute(
            select(auto_day, func.count().label("n"))
            .select_from(ActivityLog)
            .join(ClientCase, ClientCase.id == ActivityLog.case_id)
            .join(
                Reminder,
                Reminder.id
                == cast(cast(ActivityLog.details["reminder_id"].astext, String), PgUUID),
            )
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                ActivityLog.action_type.in_(("reminder.sent", "reminder.escalated")),
                Reminder.auto_threshold_days.is_not(None),
            )
            .group_by(auto_day)
        )
    ).all()
    doc_day = func.date_trunc("day", Document.created_at).label("day")
    collected_rows = (
        await db.execute(
            select(doc_day, func.count().label("n"))
            .select_from(Document)
            .join(ClientCase, ClientCase.id == Document.case_id)
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                Document.uploaded_by_type == "expat",
            )
            .group_by(doc_day)
        )
    ).all()
    sig_day = func.date_trunc("day", SignatureRequest.completed_at).label("day")
    signed_rows = (
        await db.execute(
            select(sig_day, func.count().label("n"))
            .select_from(SignatureRequest)
            .join(ClientCase, ClientCase.id == SignatureRequest.case_id)
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                SignatureRequest.completed_at.is_not(None),
            )
            .group_by(sig_day)
        )
    ).all()
    case_day = func.date_trunc("day", ClientCase.created_at).label("day")
    templated_rows = (
        await db.execute(
            select(case_day, func.count().label("n"))
            .select_from(ClientCase)
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                ClientCase.journey_template_id.is_not(None),
            )
            .group_by(case_day)
        )
    ).all()
    # Étapes franchies — TOUT l'historique (la requête 1 est fenêtrée sur
    # deux semaines pour le bandeau ; le temps gagné, lui, cumule depuis
    # toujours).
    #
    # L'AUTO-COMPLÉTION COMPTE — arbitrage Alexandre du 06/08, pris en
    # connaissance de l'adjacence : une étape à validateur `none` se ferme
    # seule quand ses exigences sont remplies, donc un dépôt de pièce peut
    # valoir 8 (la pièce) + 5 (l'étape). Ce ne sont pas deux comptes du
    # même geste mais DEUX EFFETS DISTINCTS : la pièce est arrivée sans
    # relance, et l'étape a avancé sans que personne ne vérifie ni ne
    # clique. Ne pas « corriger » ceci sans le redemander.
    done_day = func.date_trunc("day", CaseStepProgress.completed_at).label("day")
    steps_done_rows = (
        await db.execute(
            select(done_day, func.count().label("n"))
            .select_from(CaseStepProgress)
            .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                CaseStepProgress.completed_at.is_not(None),
            )
            .group_by(done_day)
        )
    ).all()
    # Dossiers clos — l'ÉVÉNEMENT daté (le log), pas l'état courant : un
    # dossier rouvert puis reclos a bien coûté deux vérifications.
    closed_day = func.date_trunc("day", ActivityLog.created_at).label("day")
    closed_rows = (
        await db.execute(
            select(closed_day, func.count().label("n"))
            .select_from(ActivityLog)
            .join(ClientCase, ClientCase.id == ActivityLog.case_id)
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                ActivityLog.action_type == "case.status_changed",
                ActivityLog.details["new"].astext == "closed",
            )
            .group_by(closed_day)
        )
    ).all()
    # Fiches importées — la SEULE source sans marqueur en base : une fiche
    # née d'un import ne se distingue d'aucune autre (rien ne le note, et
    # `source` appartient au métier de l'agence). On compte donc
    # l'ÉVÉNEMENT d'import, émis depuis ce lot : l'historique de ce geste
    # démarre au déploiement, contrairement aux six autres.
    imp_day = func.date_trunc("day", UsageEvent.created_at).label("day")
    imported_rows = (
        await db.execute(
            select(
                imp_day,
                func.coalesce(
                    func.sum(cast(UsageEvent.details["created"].astext, Integer)), 0
                ).label("n"),
            )
            .select_from(UsageEvent)
            .where(
                UsageEvent.agency_id == agent.agency_id,
                UsageEvent.event_type == "agency.profiles_imported",
            )
            .group_by(imp_day)
        )
    ).all()

    # --- plis Python -------------------------------------------------------
    def fold_pairs(
        rows: Any, cols: tuple[str, ...]
    ) -> tuple[list[int], list[int], dict[Any, list[int]]]:
        cur = [0] * len(cols)
        prev = [0] * len(cols)
        daily: dict[Any, list[int]] = {}
        for row in rows:
            values = [int(getattr(row, c)) for c in cols]
            if in_cur(row.day):
                cur = [a + b for a, b in zip(cur, values, strict=True)]
                daily[row.day.date()] = [
                    a + b
                    for a, b in zip(daily.get(row.day.date(), [0] * len(cols)), values, strict=True)
                ]
            elif in_prev(row.day):
                prev = [a + b for a, b in zip(prev, values, strict=True)]
        return cur, prev, daily

    steps_cur, steps_prev, steps_daily = fold_pairs(step_rows, ("agency_n", "me_n"))
    log_cur, log_prev, log_daily = fold_pairs(
        log_rows, ("val_a", "val_m", "clo_a", "clo_m", "rem_a", "rem_m")
    )

    def fold_source(rows: Any) -> tuple[int, int, int, int, dict[Any, int]]:
        all_time = period_n = prev_n = month_n = 0
        daily: dict[Any, int] = {}
        for row in rows:
            n = int(row.n)
            all_time += n
            # Le mois est une maille À PART : il chevauche la période
            # courante ET la précédente (une semaine à cheval sur deux
            # mois), d'où un test indépendant, jamais un `elif`.
            if in_month(row.day):
                month_n += n
            if in_cur(row.day):
                period_n += n
                daily[row.day.date()] = daily.get(row.day.date(), 0) + n
            elif in_prev(row.day):
                prev_n += n
        return all_time, period_n, prev_n, month_n, daily

    folded = {
        "case_created_from_template": fold_source(templated_rows),
        "step_completed": fold_source(steps_done_rows),
        "client_document_collected": fold_source(collected_rows),
        "auto_reminder_sent": fold_source(auto_rows),
        "signature_completed": fold_source(signed_rows),
        "case_closed": fold_source(closed_rows),
        "profile_imported": fold_source(imported_rows),
    }
    # (all_time, period, prev, month) en ints purs + le par-jour à part —
    # mypy et lecteur y gagnent le même contrat.
    source_counts: dict[str, tuple[int, int, int, int]] = {
        k: (v[0], v[1], v[2], v[3]) for k, v in folded.items()
    }
    source_daily: dict[str, dict[Any, int]] = {k: v[4] for k, v in folded.items()}
    scale = get_settings().kpi_time_saved_minutes
    client_kinds = ("signature_completed", "client_document_collected")
    all_kinds = tuple(source_counts)

    def ts_block(kinds: tuple[str, ...], index: int) -> TimeSavedBlockResponse:
        items = [
            TimeSavedItemResponse(
                kind=kind,
                count=source_counts[kind][index],
                minutes_each=int(scale.get(kind, 0)),
                minutes_total=source_counts[kind][index] * int(scale.get(kind, 0)),
            )
            for kind in kinds
        ]
        return TimeSavedBlockResponse(
            items=items, total_minutes=sum(item.minutes_total for item in items)
        )

    time_saved = TimeSavedResponse(
        period=ts_block(all_kinds, 1),
        all_time=ts_block(all_kinds, 0),
        month=ts_block(all_kinds, 3),
        clients_period=ts_block(client_kinds, 1),
        clients_all_time=ts_block(client_kinds, 0),
    )

    daily_points = None
    previous_period = None
    if is_week:
        daily_points = []
        for offset in range(7):
            day = (monday + timedelta(days=offset)).date()
            log_day_vals = log_daily.get(day, [0] * 6)
            minutes = sum(
                source_daily[kind].get(day, 0) * int(scale.get(kind, 0)) for kind in all_kinds
            )
            daily_points.append(
                DailyPointResponse(
                    day=day,
                    steps_completed=steps_daily.get(day, [0, 0])[0],
                    documents_validated=log_day_vals[0],
                    cases_closed=log_day_vals[2],
                    manual_reminders=log_day_vals[4],
                    time_saved_minutes=minutes,
                )
            )
        previous_period = PreviousPeriodResponse(
            since=prev_since,
            until=monday,
            agency=KpiBlockResponse(
                steps_completed=steps_prev[0],
                documents_validated=log_prev[0],
                cases_closed=log_prev[2],
                manual_reminders=log_prev[4],
            ),
            me=KpiBlockResponse(
                steps_completed=steps_prev[1],
                documents_validated=log_prev[1],
                cases_closed=log_prev[3],
                manual_reminders=log_prev[5],
            ),
            time_saved_minutes=sum(
                source_counts[kind][2] * int(scale.get(kind, 0)) for kind in all_kinds
            ),
            clients_time_saved_minutes=sum(
                source_counts[kind][2] * int(scale.get(kind, 0)) for kind in client_kinds
            ),
        )

    return ActivityStatsResponse(
        period=period,
        since=since,
        time_saved=time_saved,
        daily=daily_points,
        previous_period=previous_period,
        agency=KpiBlockResponse(
            steps_completed=steps_cur[0],
            documents_validated=log_cur[0],
            cases_closed=log_cur[2],
            manual_reminders=log_cur[4],
        ),
        me=KpiBlockResponse(
            steps_completed=steps_cur[1],
            documents_validated=log_cur[1],
            cases_closed=log_cur[3],
            manual_reminders=log_cur[5],
        ),
    )
