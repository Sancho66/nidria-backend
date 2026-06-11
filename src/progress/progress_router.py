import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.progress.progress_manager import ProgressManager
from src.progress.progress_schema import (
    AssignJourneyRequest,
    StepProgressResponse,
    StepProgressUpdateRequest,
)

router = APIRouter(prefix="/cases", tags=["progress"])

BINDINGS = [
    RouteBinding("POST", "/cases/{case_id}/journey", Audience.AGENT, Permission.CASE_EDIT),
    RouteBinding("GET", "/cases/{case_id}/steps", Audience.AGENT, Permission.CASE_VIEW),
    # step.complete covers the whole PATCH (transitions + responsible):
    # the work-the-steps permission. Finer granularity = one catalogue
    # line + matrix data, the engine already allows it.
    RouteBinding(
        "PATCH",
        "/cases/{case_id}/steps/{step_progress_id}",
        Audience.AGENT,
        Permission.STEP_COMPLETE,
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.post("/{case_id}/journey", response_model=list[StepProgressResponse], status_code=201)
async def assign_journey(
    case_id: uuid.UUID, body: AssignJourneyRequest, agent: AgentDep, db: DbDep
) -> list[StepProgressResponse]:
    return await ProgressManager(db).assign_journey(agent, case_id, body.journey_template_id)


@router.get("/{case_id}/steps", response_model=list[StepProgressResponse])
async def get_timeline(
    case_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[StepProgressResponse]:
    return await ProgressManager(db).get_timeline(agent, case_id)


@router.patch("/{case_id}/steps/{step_progress_id}", response_model=StepProgressResponse)
async def update_step(
    case_id: uuid.UUID,
    step_progress_id: uuid.UUID,
    body: StepProgressUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> StepProgressResponse:
    return await ProgressManager(db).update_step(agent, case_id, step_progress_id, body)
