import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ActivityLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_type: str
    actor_id: uuid.UUID | None
    action_type: str
    details: dict[str, Any]
    created_at: datetime


class ActivityListResponse(BaseModel):
    items: list[ActivityLogResponse]
    total: int
    page: int
    page_size: int


class KpiBlockResponse(BaseModel):
    """Les 4 KPI v1 de travail accompli (extraction 30/07) — dérivés à la
    lecture (colonnes + activity_log), jamais une instrumentation neuve."""

    steps_completed: int
    documents_validated: int
    cases_closed: int
    manual_reminders: int


class ActivityStatsResponse(BaseModel):
    """GET /agencies/me/activity-stats — l'agrégat AGENCE et le « moi » de
    l'agent connecté dans la même réponse. Bornes CALENDAIRES UTC (verdict :
    l'agence n'a pas de fuseau au modèle — le poser serait un lot ; le jour
    où il existe, seule la fonction de bornes change)."""

    period: str  # today | week
    since: datetime
    agency: KpiBlockResponse
    me: KpiBlockResponse
    time_saved: "TimeSavedResponse"
    # Tendance (lot 31/07) : servis pour period=week SEULEMENT (None en
    # today — la tendance d'un jour n'existe pas). Additions pures.
    daily: "list[DailyPointResponse] | None" = None
    previous_period: "PreviousPeriodResponse | None" = None


class TimeSavedItemResponse(BaseModel):
    """Une ligne du détail — le tooltip front affiche geste par geste :
    combien de fois, combien de minutes chacune, le sous-total."""

    kind: str
    count: int
    minutes_each: int
    minutes_total: int


class TimeSavedBlockResponse(BaseModel):
    items: list[TimeSavedItemResponse]
    total_minutes: int


class TimeSavedResponse(BaseModel):
    """« Temps gagné » (barème transparent, config KPI_TIME_SAVED_MINUTES) :
    la période demandée + le CUMUL depuis toujours (c'est lui qui
    impressionne), pour l'AGENCE et « pour vos clients » (signatures en
    ligne + collecte en ligne — les gestes que le client n'a pas faits au
    papier)."""

    period: TimeSavedBlockResponse
    all_time: TimeSavedBlockResponse
    # Le MOIS civil en cours : la maille d'un dirigeant. La semaine parle
    # à l'agent, le mois parle au comité — et c'est lui qui porte
    # l'équivalent temps plein et la projection annuelle côté écran.
    month: TimeSavedBlockResponse
    clients_period: TimeSavedBlockResponse
    clients_all_time: TimeSavedBlockResponse


class DailyPointResponse(BaseModel):
    """Un jour de la semaine courante — niveau AGENCE (la tendance du
    bandeau), zéro-rempli pour les jours sans activité."""

    day: date
    steps_completed: int
    documents_validated: int
    cases_closed: int
    manual_reminders: int
    time_saved_minutes: int


class PreviousPeriodResponse(BaseModel):
    """La semaine PRÉCÉDENTE (mêmes totaux, mêmes règles) — le delta front."""

    since: datetime
    until: datetime
    agency: KpiBlockResponse
    me: KpiBlockResponse
    time_saved_minutes: int
    clients_time_saved_minutes: int
