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
    """Volet 2 (31/07) : les 4 KPI v1, agence + « moi », en DEUX requêtes
    agrégées (témoin de comptage au test) — lecture directe des colonnes
    (étapes) et du log (validations, clôtures, relances), comme l'extraction
    l'a établi. Flag KPI_ENABLED maître : rien ne se sert éteint.

    Bornes CALENDAIRES UTC : today = minuit UTC, week = lundi 00:00 UTC —
    verdict argumenté : l'agence n'a pas de fuseau au modèle (le poser =
    modèle + migration + réglage, un lot à part), et des bornes calendaires
    collent au modèle mental « accompli aujourd'hui/cette semaine » là où
    un glissant 24h/7j double-compte d'un affichage à l'autre. Le jour où
    l'agence gagne un fuseau, seule cette fonction de bornes change."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import String, cast, func, select
    from sqlalchemy.dialects.postgresql import UUID as PgUUID

    from shared.models.activity import ActivityLog
    from shared.models.case_step_progress import CaseStepProgress
    from shared.models.client_case import ClientCase
    from shared.models.reminder import Reminder
    from src.activity.activity_schema import KpiBlockResponse
    from src.core.config import get_settings
    from src.core.exceptions import ConflictError

    if not get_settings().kpi_enabled:
        raise ConflictError("Work KPIs are not enabled.", code="kpi.disabled")
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight if period == "today" else midnight - timedelta(days=midnight.weekday())

    # Requête 1 — étapes franchies : la colonne dédiée, l'auto (NULL) exclu.
    me_filter = CaseStepProgress.completed_by_agent_id == agent.id
    steps_row = (
        await db.execute(
            select(
                func.count().label("agency_n"),
                func.count().filter(me_filter).label("me_n"),
            )
            .select_from(CaseStepProgress)
            .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                CaseStepProgress.completed_at >= since,
                CaseStepProgress.completed_by_agent_id.is_not(None),
            )
        )
    ).one()

    # Requête 2 — le log : validations, clôtures, relances approuvées
    # (jointure reminder pour exclure les AUTOS — auto_threshold_days posé).
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
    log_row = (
        await db.execute(
            select(
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
                ActivityLog.created_at >= since,
                ActivityLog.actor_type == "agent",
                ActivityLog.action_type.in_(
                    ("document.validated", "case.status_changed", "reminder.approved")
                ),
            )
        )
    ).one()

    # --- « Temps gagné » (lot 31/07) — 4 sources dérivées, ZÉRO
    # instrumentation neuve (verdicts au rapport), chaque table interrogée
    # UNE fois avec un FILTER période vs cumul :
    # 1. relances AUTO envoyées : log reminder.sent/escalated (SYSTEM) +
    #    jointure reminder (auto_threshold_days NOT NULL) ;
    # 2. documents client collectés : document uploaded_by_type='expat' ;
    # 3. signatures complétées : signature_request.completed_at ;
    # 4. dossiers créés depuis modèle : client_case.journey_template_id
    #    NOT NULL à la création (approximation nommée : la date
    #    d'ASSIGNATION n'est pas stockée — on compte le dossier à sa
    #    création s'il porte un modèle aujourd'hui).
    from shared.models.document import Document
    from shared.models.signature import SignatureRequest
    from src.activity.activity_schema import (
        TimeSavedBlockResponse,
        TimeSavedItemResponse,
        TimeSavedResponse,
    )

    in_period = ActivityLog.created_at >= since
    auto_sent = (
        await db.execute(
            select(
                func.count().label("all_time"),
                func.count().filter(in_period).label("period"),
            )
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
        )
    ).one()
    collected = (
        await db.execute(
            select(
                func.count().label("all_time"),
                func.count().filter(Document.created_at >= since).label("period"),
            )
            .select_from(Document)
            .join(ClientCase, ClientCase.id == Document.case_id)
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                Document.uploaded_by_type == "expat",
            )
        )
    ).one()
    signed = (
        await db.execute(
            select(
                func.count().label("all_time"),
                func.count().filter(SignatureRequest.completed_at >= since).label("period"),
            )
            .select_from(SignatureRequest)
            .join(ClientCase, ClientCase.id == SignatureRequest.case_id)
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                SignatureRequest.completed_at.is_not(None),
            )
        )
    ).one()
    templated = (
        await db.execute(
            select(
                func.count().label("all_time"),
                func.count().filter(ClientCase.created_at >= since).label("period"),
            )
            .select_from(ClientCase)
            .where(
                ClientCase.agency_id == agent.agency_id,
                ClientCase.deleted_at.is_(None),
                ClientCase.journey_template_id.is_not(None),
            )
        )
    ).one()

    scale = get_settings().kpi_time_saved_minutes
    counts = {
        "auto_reminder_sent": (int(auto_sent.period), int(auto_sent.all_time)),
        "client_document_collected": (int(collected.period), int(collected.all_time)),
        "signature_completed": (int(signed.period), int(signed.all_time)),
        "case_created_from_template": (int(templated.period), int(templated.all_time)),
    }
    client_kinds = ("signature_completed", "client_document_collected")

    def block(kinds: tuple[str, ...], index: int) -> TimeSavedBlockResponse:
        items = [
            TimeSavedItemResponse(
                kind=kind,
                count=counts[kind][index],
                minutes_each=int(scale.get(kind, 0)),
                minutes_total=counts[kind][index] * int(scale.get(kind, 0)),
            )
            for kind in kinds
        ]
        return TimeSavedBlockResponse(
            items=items, total_minutes=sum(item.minutes_total for item in items)
        )

    all_kinds = tuple(counts)
    time_saved = TimeSavedResponse(
        period=block(all_kinds, 0),
        all_time=block(all_kinds, 1),
        clients_period=block(client_kinds, 0),
        clients_all_time=block(client_kinds, 1),
    )

    return ActivityStatsResponse(
        period=period,
        since=since,
        time_saved=time_saved,
        agency=KpiBlockResponse(
            steps_completed=int(steps_row.agency_n),
            documents_validated=int(log_row.val_a),
            cases_closed=int(log_row.clo_a),
            manual_reminders=int(log_row.rem_a),
        ),
        me=KpiBlockResponse(
            steps_completed=int(steps_row.me_n),
            documents_validated=int(log_row.val_m),
            cases_closed=int(log_row.clo_m),
            manual_reminders=int(log_row.rem_m),
        ),
    )
