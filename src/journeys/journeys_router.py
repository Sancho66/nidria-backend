import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.auth.auth_schema import MessageResponse
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.http import file_download_response
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.journeys.journeys_manager import JourneysManager
from src.journeys.journeys_schema import (
    CanvasLayoutRequest,
    CanvasNodePosition,
    CaseFieldCreateRequest,
    CaseFieldOrderRequest,
    CaseFieldUpdateRequest,
    JourneyCloneRequest,
    JourneySectionResponse,
    JourneyTemplateCreateRequest,
    JourneyTemplateDetailResponse,
    JourneyTemplateResponse,
    JourneyTemplateUpdateRequest,
    SectionCreateRequest,
    SectionOrderRequest,
    SectionUpdateRequest,
    StepAttachmentResponse,
    StepCaseRequirementCreateRequest,
    StepCaseRequirementOrderRequest,
    StepCaseRequirementResponse,
    StepOrderRequest,
    StepParticipantCreateRequest,
    StepPrerequisitesRequest,
    StepRequirementCreateRequest,
    StepRequirementOrderRequest,
    StepRequirementResponse,
    TemplateCaseFieldResponse,
    TemplateFieldCreateRequest,
    TemplateFieldOrderRequest,
    TemplateFieldResponse,
    TemplateFieldUpdateRequest,
    TemplateStepCreateRequest,
    TemplateStepParticipantResponse,
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
    # Library samples (read-only) — any agent reads; gated like GET /journeys.
    RouteBinding("GET", "/journeys/library", Audience.AGENT),
    RouteBinding("POST", "/journeys", Audience.AGENT, Permission.JOURNEY_CONFIGURE),
    # Deep clone (source = sample OR own template) into the calling agency.
    RouteBinding(
        "POST", "/journeys/{template_id}/clone", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding("GET", "/journeys/{template_id}", Audience.AGENT),
    RouteBinding("PATCH", "/journeys/{template_id}", Audience.AGENT, Permission.JOURNEY_CONFIGURE),
    RouteBinding("DELETE", "/journeys/{template_id}", Audience.AGENT, Permission.JOURNEY_CONFIGURE),
    RouteBinding(
        "PUT",
        "/journeys/{template_id}/canvas-layout",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
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
        "PUT",
        "/journeys/{template_id}/steps/{step_id}/requirements/order",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "DELETE",
        "/journeys/{template_id}/steps/{step_id}/requirements/{requirement_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    # Step CASE requirements (sections chantier, vague C) — calque.
    RouteBinding(
        "GET",
        "/journeys/{template_id}/steps/{step_id}/case-requirements",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "POST",
        "/journeys/{template_id}/steps/{step_id}/case-requirements",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "PUT",
        "/journeys/{template_id}/steps/{step_id}/case-requirements/order",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "DELETE",
        "/journeys/{template_id}/steps/{step_id}/case-requirements/{case_requirement_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    # Step content attachments (Feature 2 — descending agency content).
    RouteBinding(
        "GET",
        "/journeys/{template_id}/steps/{step_id}/attachments",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "POST",
        "/journeys/{template_id}/steps/{step_id}/attachments",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "GET",
        "/journeys/{template_id}/steps/{step_id}/attachments/{attachment_id}/download",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "DELETE",
        "/journeys/{template_id}/steps/{step_id}/attachments/{attachment_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    # "Action à réaliser par" — template participants (responsible refonte).
    RouteBinding(
        "GET",
        "/journeys/{template_id}/steps/{step_id}/participants",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "POST",
        "/journeys/{template_id}/steps/{step_id}/participants",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "DELETE",
        "/journeys/{template_id}/steps/{step_id}/participants/{participant_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    # Per-template field collection (NEW WAVE) — calque of requirements.
    RouteBinding(
        "GET", "/journeys/{template_id}/fields", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "POST", "/journeys/{template_id}/fields", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "PUT", "/journeys/{template_id}/fields/order", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "PATCH",
        "/journeys/{template_id}/fields/{field_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "DELETE",
        "/journeys/{template_id}/fields/{field_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    # Per-template CASE-field collection (option b) — countries. Separate
    # mechanism, same gate; the UI unifies them with /fields for display.
    RouteBinding(
        "GET", "/journeys/{template_id}/case-fields", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "POST", "/journeys/{template_id}/case-fields", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "PUT",
        "/journeys/{template_id}/case-fields/order",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "PATCH",
        "/journeys/{template_id}/case-fields/{case_field_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "DELETE",
        "/journeys/{template_id}/case-fields/{case_field_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    # Sections (sections chantier, vague A) — additive socle, same gate.
    RouteBinding(
        "GET", "/journeys/{template_id}/sections", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "POST", "/journeys/{template_id}/sections", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "PUT",
        "/journeys/{template_id}/sections/order",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "PATCH",
        "/journeys/{template_id}/sections/{section_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "DELETE",
        "/journeys/{template_id}/sections/{section_id}",
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


# Declared BEFORE /{template_id} so the static path is not captured as an id.
@router.get("/library", response_model=list[JourneyTemplateResponse])
async def list_sample_templates(agent: AgentDep, db: DbDep) -> list[JourneyTemplateResponse]:
    """Shared LIBRARY samples (read-only). Distinct from GET /journeys (the
    agency's own templates) — an agency consumes a sample by cloning it."""
    templates = await JourneysManager(db).list_sample_templates()
    return [JourneyTemplateResponse.model_validate(template) for template in templates]


@router.post("", response_model=JourneyTemplateResponse, status_code=201)
async def create_template(
    body: JourneyTemplateCreateRequest, agent: AgentDep, db: DbDep
) -> JourneyTemplateResponse:
    template = await JourneysManager(db).create_template(agent, body.name)
    return JourneyTemplateResponse.model_validate(template)


@router.post("/{template_id}/clone", response_model=JourneyTemplateResponse, status_code=201)
async def clone_template(
    template_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    body: JourneyCloneRequest | None = None,
) -> JourneyTemplateResponse:
    """Deep-clone a sample or own template into the calling agency. Attachments
    are not cloned (the file stays on the source). The body is OPTIONAL: no
    body / {} → default name "{source} (copie)"; {name} → that name."""
    name = body.name if body is not None else None
    template = await JourneysManager(db).clone_template(agent, template_id, name)
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


@router.put("/{template_id}/canvas-layout", response_model=dict[str, CanvasNodePosition])
async def set_canvas_layout(
    template_id: uuid.UUID, body: CanvasLayoutRequest, agent: AgentDep, db: DbDep
) -> dict[str, CanvasNodePosition]:
    return await JourneysManager(db).set_canvas_layout(agent, template_id, body)


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


# --- step content attachments (Feature 2 — descending agency content) ----------------


@router.get(
    "/{template_id}/steps/{step_id}/attachments",
    response_model=list[StepAttachmentResponse],
)
async def list_step_attachments(
    template_id: uuid.UUID, step_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[StepAttachmentResponse]:
    return await JourneysManager(db).list_step_attachments(agent, template_id, step_id)


@router.post(
    "/{template_id}/steps/{step_id}/attachments",
    response_model=StepAttachmentResponse,
    status_code=201,
)
async def add_step_attachment(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    file: UploadFile,
    agent: AgentDep,
    db: DbDep,
) -> StepAttachmentResponse:
    return await JourneysManager(db).add_step_attachment(agent, template_id, step_id, file)


@router.get("/{template_id}/steps/{step_id}/attachments/{attachment_id}/download")
async def download_step_attachment(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    attachment_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
) -> Response:
    filename, content = await JourneysManager(db).download_step_attachment(
        agent, template_id, step_id, attachment_id
    )
    return file_download_response(filename, content)


@router.delete(
    "/{template_id}/steps/{step_id}/attachments/{attachment_id}",
    response_model=MessageResponse,
)
async def delete_step_attachment(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    attachment_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
) -> MessageResponse:
    await JourneysManager(db).delete_step_attachment(agent, template_id, step_id, attachment_id)
    return MessageResponse(detail="Attachment removed.")


# --- step participants ("Action à réaliser par", N) ----------------------------------


@router.get(
    "/{template_id}/steps/{step_id}/participants",
    response_model=list[TemplateStepParticipantResponse],
)
async def list_step_participants(
    template_id: uuid.UUID, step_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[TemplateStepParticipantResponse]:
    return await JourneysManager(db).list_step_participants(agent, template_id, step_id)


@router.post(
    "/{template_id}/steps/{step_id}/participants",
    response_model=TemplateStepParticipantResponse,
    status_code=201,
)
async def add_step_participant(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    body: StepParticipantCreateRequest,
    agent: AgentDep,
    db: DbDep,
) -> TemplateStepParticipantResponse:
    return await JourneysManager(db).add_step_participant(agent, template_id, step_id, body)


@router.delete(
    "/{template_id}/steps/{step_id}/participants/{participant_id}",
    response_model=MessageResponse,
)
async def delete_step_participant(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    participant_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
) -> MessageResponse:
    await JourneysManager(db).delete_step_participant(agent, template_id, step_id, participant_id)
    return MessageResponse(detail="Participant removed.")


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


@router.put(
    "/{template_id}/steps/{step_id}/requirements/order",
    response_model=list[StepRequirementResponse],
)
async def reorder_requirements(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    body: StepRequirementOrderRequest,
    agent: AgentDep,
    db: DbDep,
) -> list[StepRequirementResponse]:
    rows = await JourneysManager(db).reorder_requirements(
        agent, template_id, step_id, body.requirement_ids
    )
    return [StepRequirementResponse.model_validate(r) for r in rows]


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


# --- step CASE requirements (sections chantier, vague C) -----------------------------


@router.get(
    "/{template_id}/steps/{step_id}/case-requirements",
    response_model=list[StepCaseRequirementResponse],
)
async def list_step_case_requirements(
    template_id: uuid.UUID, step_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[StepCaseRequirementResponse]:
    rows = await JourneysManager(db).list_step_case_requirements(agent, template_id, step_id)
    return [StepCaseRequirementResponse.model_validate(r) for r in rows]


@router.post(
    "/{template_id}/steps/{step_id}/case-requirements",
    response_model=StepCaseRequirementResponse,
    status_code=201,
)
async def add_step_case_requirement(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    body: StepCaseRequirementCreateRequest,
    agent: AgentDep,
    db: DbDep,
) -> StepCaseRequirementResponse:
    row = await JourneysManager(db).add_step_case_requirement(agent, template_id, step_id, body)
    return StepCaseRequirementResponse.model_validate(row)


@router.put(
    "/{template_id}/steps/{step_id}/case-requirements/order",
    response_model=list[StepCaseRequirementResponse],
)
async def reorder_step_case_requirements(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    body: StepCaseRequirementOrderRequest,
    agent: AgentDep,
    db: DbDep,
) -> list[StepCaseRequirementResponse]:
    rows = await JourneysManager(db).reorder_step_case_requirements(
        agent, template_id, step_id, body.case_requirement_ids
    )
    return [StepCaseRequirementResponse.model_validate(r) for r in rows]


@router.delete(
    "/{template_id}/steps/{step_id}/case-requirements/{case_requirement_id}",
    response_model=MessageResponse,
)
async def delete_step_case_requirement(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    case_requirement_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
) -> MessageResponse:
    await JourneysManager(db).delete_step_case_requirement(
        agent, template_id, step_id, case_requirement_id
    )
    return MessageResponse(detail="Case requirement removed.")


# --- per-template field collection (NEW WAVE) ----------------------------------------


@router.get("/{template_id}/fields", response_model=list[TemplateFieldResponse])
async def list_fields(
    template_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[TemplateFieldResponse]:
    return await JourneysManager(db).list_fields(agent, template_id)


@router.post("/{template_id}/fields", response_model=TemplateFieldResponse, status_code=201)
async def add_field(
    template_id: uuid.UUID, body: TemplateFieldCreateRequest, agent: AgentDep, db: DbDep
) -> TemplateFieldResponse:
    return await JourneysManager(db).add_field(agent, template_id, body)


@router.put("/{template_id}/fields/order", response_model=list[TemplateFieldResponse])
async def reorder_fields(
    template_id: uuid.UUID, body: TemplateFieldOrderRequest, agent: AgentDep, db: DbDep
) -> list[TemplateFieldResponse]:
    return await JourneysManager(db).reorder_fields(agent, template_id, body.field_ids)


@router.patch("/{template_id}/fields/{field_id}", response_model=TemplateFieldResponse)
async def update_field(
    template_id: uuid.UUID,
    field_id: uuid.UUID,
    body: TemplateFieldUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> TemplateFieldResponse:
    return await JourneysManager(db).update_field(agent, template_id, field_id, body)


@router.delete("/{template_id}/fields/{field_id}", response_model=MessageResponse)
async def delete_field(
    template_id: uuid.UUID, field_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await JourneysManager(db).delete_field(agent, template_id, field_id)
    return MessageResponse(detail="Field removed.")


# --- per-template CASE-field collection (option b) — countries -----------------------


@router.get("/{template_id}/case-fields", response_model=list[TemplateCaseFieldResponse])
async def list_case_fields(
    template_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[TemplateCaseFieldResponse]:
    return await JourneysManager(db).list_case_fields(agent, template_id)


@router.post(
    "/{template_id}/case-fields", response_model=TemplateCaseFieldResponse, status_code=201
)
async def add_case_field(
    template_id: uuid.UUID, body: CaseFieldCreateRequest, agent: AgentDep, db: DbDep
) -> TemplateCaseFieldResponse:
    return await JourneysManager(db).add_case_field(agent, template_id, body)


@router.put("/{template_id}/case-fields/order", response_model=list[TemplateCaseFieldResponse])
async def reorder_case_fields(
    template_id: uuid.UUID, body: CaseFieldOrderRequest, agent: AgentDep, db: DbDep
) -> list[TemplateCaseFieldResponse]:
    return await JourneysManager(db).reorder_case_fields(agent, template_id, body.case_field_ids)


@router.patch(
    "/{template_id}/case-fields/{case_field_id}", response_model=TemplateCaseFieldResponse
)
async def update_case_field(
    template_id: uuid.UUID,
    case_field_id: uuid.UUID,
    body: CaseFieldUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> TemplateCaseFieldResponse:
    return await JourneysManager(db).update_case_field(agent, template_id, case_field_id, body)


@router.delete("/{template_id}/case-fields/{case_field_id}", response_model=MessageResponse)
async def delete_case_field(
    template_id: uuid.UUID, case_field_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await JourneysManager(db).delete_case_field(agent, template_id, case_field_id)
    return MessageResponse(detail="Case field removed.")


# --- sections (sections chantier, vague A) -------------------------------------------


@router.get("/{template_id}/sections", response_model=list[JourneySectionResponse])
async def list_sections(
    template_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[JourneySectionResponse]:
    return await JourneysManager(db).list_sections(agent, template_id)


@router.post("/{template_id}/sections", response_model=JourneySectionResponse, status_code=201)
async def add_section(
    template_id: uuid.UUID, body: SectionCreateRequest, agent: AgentDep, db: DbDep
) -> JourneySectionResponse:
    return await JourneysManager(db).add_section(agent, template_id, body)


@router.put("/{template_id}/sections/order", response_model=list[JourneySectionResponse])
async def reorder_sections(
    template_id: uuid.UUID, body: SectionOrderRequest, agent: AgentDep, db: DbDep
) -> list[JourneySectionResponse]:
    return await JourneysManager(db).reorder_sections(agent, template_id, body.section_ids)


@router.patch("/{template_id}/sections/{section_id}", response_model=JourneySectionResponse)
async def update_section(
    template_id: uuid.UUID,
    section_id: uuid.UUID,
    body: SectionUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> JourneySectionResponse:
    return await JourneysManager(db).update_section(agent, template_id, section_id, body)


@router.delete("/{template_id}/sections/{section_id}", response_model=MessageResponse)
async def delete_section(
    template_id: uuid.UUID, section_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await JourneysManager(db).delete_section(agent, template_id, section_id)
    return MessageResponse(detail="Section removed.")
