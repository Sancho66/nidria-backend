import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.auth.auth_schema import MessageResponse
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.journeys.journeys_manager import JourneysManager
from src.journeys.journeys_schema import (
    JourneyTemplateCreateRequest,
    JourneyTemplateDetailResponse,
    JourneyTemplateResponse,
    JourneyTemplateUpdateRequest,
    StepOrderRequest,
    StepPrerequisitesRequest,
    StepRequirementCreateRequest,
    StepRequirementResponse,
    TemplateStepCreateRequest,
    TemplateStepResponse,
    TemplateStepUpdateRequest,
)

router = APIRouter(prefix="/journeys", tags=["journeys"])

# Reads are AGENT-without-permission: templates are tenant reference
# data (process, not client data) — any agent of the agency may read
# them; the agency scoping happens in the Manager. Writes require
# journey.configure (admin + case_manager in the default matrix).
BINDINGS = [
    RouteBinding("GET", "/journeys", Audience.AGENT),
    RouteBinding("POST", "/journeys", Audience.AGENT, Permission.JOURNEY_CONFIGURE),
    RouteBinding("GET", "/journeys/{template_id}", Audience.AGENT),
    RouteBinding("PATCH", "/journeys/{template_id}", Audience.AGENT, Permission.JOURNEY_CONFIGURE),
    RouteBinding("DELETE", "/journeys/{template_id}", Audience.AGENT, Permission.JOURNEY_CONFIGURE),
    RouteBinding(
        "POST", "/journeys/{template_id}/steps", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "PUT",
        "/journeys/{template_id}/steps/order",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "PATCH",
        "/journeys/{template_id}/steps/{step_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "DELETE",
        "/journeys/{template_id}/steps/{step_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "PUT",
        "/journeys/{template_id}/steps/{step_id}/prerequisites",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "GET",
        "/journeys/{template_id}/steps/{step_id}/requirements",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "POST",
        "/journeys/{template_id}/steps/{step_id}/requirements",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "DELETE",
        "/journeys/{template_id}/steps/{step_id}/requirements/{requirement_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


def _step_response(
    step_id: uuid.UUID, manager_detail: JourneyTemplateDetailResponse
) -> TemplateStepResponse:
    for step in manager_detail.steps:
        if step.id == step_id:
            return step
    raise AssertionError("step disappeared mid-request")


@router.get("", response_model=list[JourneyTemplateResponse])
async def list_templates(agent: AgentDep, db: DbDep) -> list[JourneyTemplateResponse]:
    templates = await JourneysManager(db).list_templates(agent)
    return [JourneyTemplateResponse.model_validate(template) for template in templates]


@router.post("", response_model=JourneyTemplateResponse, status_code=201)
async def create_template(
    body: JourneyTemplateCreateRequest, agent: AgentDep, db: DbDep
) -> JourneyTemplateResponse:
    template = await JourneysManager(db).create_template(agent, body.name)
    return JourneyTemplateResponse.model_validate(template)


@router.get("/{template_id}", response_model=JourneyTemplateDetailResponse)
async def get_template(
    template_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> JourneyTemplateDetailResponse:
    return await JourneysManager(db).get_template_detail(agent, template_id)


@router.patch("/{template_id}", response_model=JourneyTemplateResponse)
async def update_template(
    template_id: uuid.UUID,
    body: JourneyTemplateUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> JourneyTemplateResponse:
    template = await JourneysManager(db).update_template(agent, template_id, body)
    return JourneyTemplateResponse.model_validate(template)


@router.delete("/{template_id}", response_model=MessageResponse)
async def delete_template(template_id: uuid.UUID, agent: AgentDep, db: DbDep) -> MessageResponse:
    await JourneysManager(db).delete_template(agent, template_id)
    return MessageResponse(detail="Template deleted.")


@router.post("/{template_id}/steps", response_model=TemplateStepResponse, status_code=201)
async def add_step(
    template_id: uuid.UUID,
    body: TemplateStepCreateRequest,
    agent: AgentDep,
    db: DbDep,
) -> TemplateStepResponse:
    manager = JourneysManager(db)
    step = await manager.add_step(agent, template_id, body)
    detail = await manager.get_template_detail(agent, template_id)
    return _step_response(step.id, detail)


@router.put("/{template_id}/steps/order", response_model=list[TemplateStepResponse])
async def reorder_steps(
    template_id: uuid.UUID, body: StepOrderRequest, agent: AgentDep, db: DbDep
) -> list[TemplateStepResponse]:
    manager = JourneysManager(db)
    await manager.reorder_steps(agent, template_id, body.step_ids)
    detail = await manager.get_template_detail(agent, template_id)
    return detail.steps


@router.patch("/{template_id}/steps/{step_id}", response_model=TemplateStepResponse)
async def update_step(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    body: TemplateStepUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> TemplateStepResponse:
    manager = JourneysManager(db)
    await manager.update_step(agent, template_id, step_id, body)
    detail = await manager.get_template_detail(agent, template_id)
    return _step_response(step_id, detail)


@router.delete("/{template_id}/steps/{step_id}", response_model=MessageResponse)
async def delete_step(
    template_id: uuid.UUID, step_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await JourneysManager(db).delete_step(agent, template_id, step_id)
    return MessageResponse(detail="Step deleted.")


@router.put(
    "/{template_id}/steps/{step_id}/prerequisites",
    response_model=TemplateStepResponse,
)
async def set_prerequisites(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    body: StepPrerequisitesRequest,
    agent: AgentDep,
    db: DbDep,
) -> TemplateStepResponse:
    manager = JourneysManager(db)
    await manager.set_prerequisites(agent, template_id, step_id, body.prerequisite_step_ids)
    detail = await manager.get_template_detail(agent, template_id)
    return _step_response(step_id, detail)


# --- step requirements (NEW WAVE) ----------------------------------------------------


@router.get(
    "/{template_id}/steps/{step_id}/requirements",
    response_model=list[StepRequirementResponse],
)
async def list_requirements(
    template_id: uuid.UUID, step_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[StepRequirementResponse]:
    rows = await JourneysManager(db).list_requirements(agent, template_id, step_id)
    return [StepRequirementResponse.model_validate(r) for r in rows]


@router.post(
    "/{template_id}/steps/{step_id}/requirements",
    response_model=StepRequirementResponse,
    status_code=201,
)
async def add_requirement(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    body: StepRequirementCreateRequest,
    agent: AgentDep,
    db: DbDep,
) -> StepRequirementResponse:
    requirement = await JourneysManager(db).add_requirement(agent, template_id, step_id, body)
    return StepRequirementResponse.model_validate(requirement)


@router.delete(
    "/{template_id}/steps/{step_id}/requirements/{requirement_id}",
    response_model=MessageResponse,
)
async def delete_requirement(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    requirement_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
) -> MessageResponse:
    await JourneysManager(db).delete_requirement(agent, template_id, step_id, requirement_id)
    return MessageResponse(detail="Requirement removed.")
