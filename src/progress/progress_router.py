import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.i18n import RequestLang
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.progress.progress_manager import ProgressManager
from src.progress.progress_schema import (
    AssignJourneyRequest,
    ResponsibleUpdateRequest,
    StepProgressResponse,
    StepProgressUpdateRequest,
    ValidatorUpdateRequest,
)

router = APIRouter(prefix="/cases", tags=["progress"])

BINDINGS = [
    RouteBinding("POST", "/cases/{case_id}/journey", Audience.AGENT, Permission.CASE_EDIT),
    RouteBinding("GET", "/cases/{case_id}/steps", Audience.AGENT, Permission.CASE_VIEW),
    # PATCH = the work-the-steps surface (transitions + deadline): step.complete.
    RouteBinding(
        "PATCH",
        "/cases/{case_id}/steps/{step_progress_id}",
        Audience.AGENT,
        Permission.STEP_COMPLETE,
    ),
    # Nominal responsible assignment (wave C) is a case EDIT — its own
    # endpoint, gate case.edit. The RGPD grant for an external is already
    # held at wave-B assignment (agent.manage); naming an already-assigned
    # provider responsible is a lower-stakes edit.
    RouteBinding(
        "PUT",
        "/cases/{case_id}/steps/{step_progress_id}/responsible",
        Audience.AGENT,
        Permission.CASE_EDIT,
    ),
    # "Action validée par" — designate the validator on the dossier, same
    # stakes as the responsible assignment (case.edit).
    RouteBinding(
        "PUT",
        "/cases/{case_id}/steps/{step_progress_id}/validator",
        Audience.AGENT,
        Permission.CASE_EDIT,
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.post("/{case_id}/journey", response_model=list[StepProgressResponse], status_code=201)
async def assign_journey(
    case_id: uuid.UUID, body: AssignJourneyRequest, agent: AgentDep, db: DbDep, lang: RequestLang
) -> list[StepProgressResponse]:
    return await ProgressManager(db).assign_journey(agent, case_id, body.journey_template_id, lang)


@router.get("/{case_id}/steps", response_model=list[StepProgressResponse])
async def get_timeline(
    case_id: uuid.UUID, agent: AgentDep, db: DbDep, lang: RequestLang
) -> list[StepProgressResponse]:
    return await ProgressManager(db).get_timeline(agent, case_id, lang)


@router.patch("/{case_id}/steps/{step_progress_id}", response_model=StepProgressResponse)
async def update_step(
    case_id: uuid.UUID,
    step_progress_id: uuid.UUID,
    body: StepProgressUpdateRequest,
    agent: AgentDep,
    db: DbDep,
    lang: RequestLang,
) -> StepProgressResponse:
    return await ProgressManager(db).update_step(agent, case_id, step_progress_id, body, lang)


@router.put("/{case_id}/steps/{step_progress_id}/responsible", response_model=StepProgressResponse)
async def set_responsible(
    case_id: uuid.UUID,
    step_progress_id: uuid.UUID,
    body: ResponsibleUpdateRequest,
    agent: AgentDep,
    db: DbDep,
    lang: RequestLang,
) -> StepProgressResponse:
    return await ProgressManager(db).set_responsible(agent, case_id, step_progress_id, body, lang)


@router.put("/{case_id}/steps/{step_progress_id}/validator", response_model=StepProgressResponse)
async def set_validator(
    case_id: uuid.UUID,
    step_progress_id: uuid.UUID,
    body: ValidatorUpdateRequest,
    agent: AgentDep,
    db: DbDep,
    lang: RequestLang,
) -> StepProgressResponse:
    return await ProgressManager(db).set_validator(agent, case_id, step_progress_id, body, lang)
