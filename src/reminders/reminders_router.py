import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.auth.auth_schema import MessageResponse
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience, ReminderStatus
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.reminders.reminders_manager import RemindersManager
from src.reminders.reminders_schema import (
    MessageTemplateCreateRequest,
    MessageTemplateResponse,
    MessageTemplateUpdateRequest,
    ReminderBulkApproveRequest,
    ReminderBulkApproveResponse,
    ReminderBulkCancelRequest,
    ReminderBulkCancelResponse,
    ReminderCreateRequest,
    ReminderListResponse,
    ReminderPreviewRequest,
    ReminderPreviewResponse,
    ReminderResponse,
    ReminderUpdateRequest,
)

router = APIRouter(tags=["reminders"])

_CREATE = Permission.REMINDER_CREATE

BINDINGS = [
    # Message templates: reads = tenant reference data; writes = the
    # reminder workers craft them.
    RouteBinding("GET", "/message-templates", Audience.AGENT),
    RouteBinding("POST", "/message-templates", Audience.AGENT, _CREATE),
    RouteBinding("PATCH", "/message-templates/{template_id}", Audience.AGENT, _CREATE),
    RouteBinding("DELETE", "/message-templates/{template_id}", Audience.AGENT, _CREATE),
    # Reminders. approve = engaging the agency (reminder.approve);
    # create/edit/cancel/mark-sent = operational (reminder.create).
    RouteBinding("POST", "/cases/{case_id}/reminders", Audience.AGENT, _CREATE),
    RouteBinding("GET", "/reminders", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding("GET", "/reminders/{reminder_id}", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding("PATCH", "/reminders/{reminder_id}", Audience.AGENT, _CREATE),
    RouteBinding(
        "POST", "/reminders/{reminder_id}/approve", Audience.AGENT, Permission.REMINDER_APPROVE
    ),
    RouteBinding("POST", "/reminders/{reminder_id}/cancel", Audience.AGENT, _CREATE),
    # Literal segment declared BEFORE /{reminder_id}/… in the router below, so
    # "bulk-cancel" is never read as a reminder id. Same gate as the unit
    # cancel: cancelling 85 is cancelling, not a new power.
    RouteBinding("POST", "/reminders/bulk-cancel", Audience.AGENT, _CREATE),
    # Same literal-before-{id} rule, and the gate of the UNIT approve, not the
    # cancel one: approving 85 is approving — reminder.approve, never a power
    # an agent could gain just by going through the bulk door.
    RouteBinding("POST", "/reminders/bulk-approve", Audience.AGENT, Permission.REMINDER_APPROVE),
    RouteBinding("POST", "/reminders/{reminder_id}/mark-sent", Audience.AGENT, _CREATE),
    # « Ce que votre client lira » : rend un BROUILLON, n'ecrit rien. Meme
    # regle littéral-avant-{id} que bulk-*, et meme gate que l'ecriture dont
    # il est l'apercu — voir l'apercu des conditions, meme forme.
    RouteBinding("POST", "/reminders/preview", Audience.AGENT, _CREATE),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


# --- message templates ------------------------------------------------------------


@router.get("/message-templates", response_model=list[MessageTemplateResponse])
async def list_message_templates(agent: AgentDep, db: DbDep) -> list[MessageTemplateResponse]:
    templates = await RemindersManager(db).list_message_templates(agent)
    return [MessageTemplateResponse.model_validate(template) for template in templates]


@router.post("/message-templates", response_model=MessageTemplateResponse, status_code=201)
async def create_message_template(
    body: MessageTemplateCreateRequest, agent: AgentDep, db: DbDep
) -> MessageTemplateResponse:
    template = await RemindersManager(db).create_message_template(agent, body)
    return MessageTemplateResponse.model_validate(template)


@router.patch("/message-templates/{template_id}", response_model=MessageTemplateResponse)
async def update_message_template(
    template_id: uuid.UUID,
    body: MessageTemplateUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> MessageTemplateResponse:
    template = await RemindersManager(db).update_message_template(agent, template_id, body)
    return MessageTemplateResponse.model_validate(template)


@router.delete("/message-templates/{template_id}", response_model=MessageResponse)
async def delete_message_template(
    template_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await RemindersManager(db).delete_message_template(agent, template_id)
    return MessageResponse(detail="Message template deleted.")


# --- reminders ----------------------------------------------------------------------


@router.post("/cases/{case_id}/reminders", response_model=ReminderResponse, status_code=201)
async def create_reminder(
    case_id: uuid.UUID, body: ReminderCreateRequest, agent: AgentDep, db: DbDep
) -> ReminderResponse:
    manager = RemindersManager(db)
    reminder = await manager.create_reminder(agent, case_id, body)
    return await manager.to_response(reminder)


@router.get("/reminders", response_model=ReminderListResponse)
async def list_reminders(
    agent: AgentDep,
    db: DbDep,
    status: Annotated[list[ReminderStatus] | None, Query()] = None,
    case_id: Annotated[uuid.UUID | None, Query()] = None,
    scheduled_from: Annotated[datetime | None, Query()] = None,
    scheduled_to: Annotated[datetime | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ReminderListResponse:
    """The agency calendar view: agency-scoped, not per-case."""
    filters = {
        "status": status,
        "case_id": case_id,
        "scheduled_from": scheduled_from,
        "scheduled_to": scheduled_to,
    }
    manager = RemindersManager(db)
    reminders, total = await manager.list_reminders(agent, filters, page, page_size)
    return ReminderListResponse(
        items=await manager.to_responses(reminders),
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/reminders/{reminder_id}", response_model=ReminderResponse)
async def get_reminder(reminder_id: uuid.UUID, agent: AgentDep, db: DbDep) -> ReminderResponse:
    manager = RemindersManager(db)
    reminder = await manager.get_reminder(agent, reminder_id)
    return await manager.to_response(reminder)


@router.patch("/reminders/{reminder_id}", response_model=ReminderResponse)
async def update_reminder(
    reminder_id: uuid.UUID, body: ReminderUpdateRequest, agent: AgentDep, db: DbDep
) -> ReminderResponse:
    manager = RemindersManager(db)
    reminder = await manager.update_reminder(agent, reminder_id, body)
    return await manager.to_response(reminder)


@router.post("/reminders/preview", response_model=ReminderPreviewResponse)
async def preview_reminder(
    body: ReminderPreviewRequest, agent: AgentDep, db: DbDep
) -> ReminderPreviewResponse:
    """« Ce que votre client lira » — le BROUILLON rendu par la MÊME
    résolution que le figeage (variables comprises), plus les jetons inconnus
    (qui seront gelés verbatim) et les non-résolubles (qui feraient lever un
    422 à l'enregistrement). Rien n'est écrit.

    Segment LITTÉRAL, déclaré avec bulk-cancel/bulk-approve et avant les
    POST en /reminders/{reminder_id}/… — « preview » ne peut jamais être lu
    comme un identifiant de rappel."""
    return await RemindersManager(db).preview_reminder(agent, body)


@router.post("/reminders/bulk-cancel", response_model=ReminderBulkCancelResponse)
async def bulk_cancel_reminders(
    body: ReminderBulkCancelRequest, agent: AgentDep, db: DbDep
) -> ReminderBulkCancelResponse:
    """Annuler en masse (jusqu'à 500 ids) — la sortie d'un passif
    d'approbation. Ids d'une autre agence ou rappels déjà envoyés : ignorés,
    `affected` dit ce qui a bougé."""
    examined, affected = await RemindersManager(db).bulk_cancel(agent, body.reminder_ids)
    return ReminderBulkCancelResponse(examined=examined, affected=affected)


@router.post("/reminders/bulk-approve", response_model=ReminderBulkApproveResponse)
async def bulk_approve_reminders(
    body: ReminderBulkApproveRequest, agent: AgentDep, db: DbDep
) -> ReminderBulkApproveResponse:
    """Approuver en masse (jusqu'à 500 ids) — le miroir de bulk-cancel pour
    l'agence qui veut que son passif PARTE. Ids d'une autre agence ou rappels
    qui ne sont plus `to_approve` : ignorés. Un rappel dont l'étape cible est
    TERMINÉE n'est pas approuvé et se compte dans `skipped_step_done` : la
    relance serait fausse, et le lot entier ne doit pas échouer pour ça."""
    examined, affected, skipped = await RemindersManager(db).bulk_approve(agent, body.reminder_ids)
    return ReminderBulkApproveResponse(
        examined=examined, affected=affected, skipped_step_done=skipped
    )


@router.post("/reminders/{reminder_id}/approve", response_model=ReminderResponse)
async def approve_reminder(reminder_id: uuid.UUID, agent: AgentDep, db: DbDep) -> ReminderResponse:
    manager = RemindersManager(db)
    reminder = await manager.approve_reminder(agent, reminder_id)
    return await manager.to_response(reminder)


@router.post("/reminders/{reminder_id}/cancel", response_model=ReminderResponse)
async def cancel_reminder(reminder_id: uuid.UUID, agent: AgentDep, db: DbDep) -> ReminderResponse:
    manager = RemindersManager(db)
    reminder = await manager.cancel_reminder(agent, reminder_id)
    return await manager.to_response(reminder)


@router.post("/reminders/{reminder_id}/mark-sent", response_model=ReminderResponse)
async def mark_reminder_sent(
    reminder_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> ReminderResponse:
    manager = RemindersManager(db)
    reminder = await manager.mark_sent(agent, reminder_id)
    return await manager.to_response(reminder)
