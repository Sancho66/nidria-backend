import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.auth.auth_schema import MessageResponse
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.http import file_download_response
from src.core.i18n import DEFAULT_LANG, Language, RequestLang, resolve_i18n
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.journeys.import_manager import JourneyImportManager
from src.journeys.journeys_manager import JourneysManager
from src.journeys.journeys_schema import (
    CanvasLayoutRequest,
    CanvasNodePosition,
    CaseFieldCreateRequest,
    CaseFieldOrderRequest,
    CaseFieldUpdateRequest,
    JourneyCloneRequest,
    JourneyImportReport,
    JourneyImportRequest,
    JourneySectionResponse,
    JourneyTemplateCreateRequest,
    JourneyTemplateDetailResponse,
    JourneyTemplateResponse,
    JourneyTemplateUpdateRequest,
    JourneyTranslateRequest,
    PlannedCostCreateRequest,
    PlannedCostResponse,
    PlannedCostUpdateRequest,
    RequirementImpactResponse,
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
    TranslateEstimateResponse,
    TranslationJobResponse,
)
from src.journeys.translation_manager import TranslationManager, execute_job, job_response

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
    # AI-JSON import (the agency pastes its own AI's output) - same
    # configure gate as the editor; deterministic, no LLM server-side.
    RouteBinding("POST", "/journeys/import", Audience.AGENT, Permission.JOURNEY_CONFIGURE),
    # Re-generation (Nicolas) : remplacer le contenu d'un template JAMAIS
    # instancie depuis un nouveau JSON — meme permission que l'import.
    RouteBinding(
        "POST", "/journeys/{template_id}/import", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    # Deep clone (source = sample OR own template) into the calling agency.
    RouteBinding(
        "POST", "/journeys/{template_id}/clone", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    # AI translation of the template's 6-language variants (fill-empty-only,
    # monthly points quota, ASYNC with per-language progress) — same
    # configure gate as the editor for the three faces.
    RouteBinding(
        "POST", "/journeys/{template_id}/translate", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "GET",
        "/journeys/{template_id}/translate/estimate",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "GET",
        "/journeys/translate-jobs/{job_id}",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
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
    # Planned costs on a template step — the SAME gate as real costs (point 9):
    # cost.manage to write; reading is embedded in GET /journeys/{template_id}
    # (steps[].planned_costs), gated cost.view IN the manager (an agent with
    # journey.configure but no cost.view never sees the section).
    RouteBinding(
        "POST",
        "/journeys/{template_id}/steps/{step_id}/planned-costs",
        Audience.AGENT,
        Permission.COST_MANAGE,
    ),
    RouteBinding(
        "PATCH",
        "/journeys/{template_id}/planned-costs/{cost_id}",
        Audience.AGENT,
        Permission.COST_MANAGE,
    ),
    RouteBinding(
        "DELETE",
        "/journeys/{template_id}/planned-costs/{cost_id}",
        Audience.AGENT,
        Permission.COST_MANAGE,
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
    # Pre-delete impact counter — same gate as the delete it guards.
    RouteBinding(
        "GET",
        "/journeys/{template_id}/steps/{step_id}/requirements/{requirement_id}/impact",
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
async def list_templates(
    agent: AgentDep, db: DbDep, lang: RequestLang
) -> list[JourneyTemplateResponse]:
    manager = JourneysManager(db)
    templates = await manager.list_templates(agent)
    agency_default = await manager.agency_default(agent.agency_id)
    # i18n: resolve the displayed name (scalar stays the seed anchor + fallback).
    return [
        JourneyTemplateResponse.model_validate(t).model_copy(
            update={"name": resolve_i18n(t.name_i18n, lang, agency_default, t.name)}
        )
        for t in templates
    ]


# Declared BEFORE /{template_id} so the static path is not captured as an id.
@router.get("/library", response_model=list[JourneyTemplateResponse])
async def list_sample_templates(
    agent: AgentDep, db: DbDep, lang: RequestLang
) -> list[JourneyTemplateResponse]:
    """Shared LIBRARY samples (read-only). Distinct from GET /journeys (the
    agency's own templates) — an agency consumes a sample by cloning it."""
    templates = await JourneysManager(db).list_sample_templates()
    # Samples have no agency → the i18n fallback is the platform default "fr".
    return [
        JourneyTemplateResponse.model_validate(t).model_copy(
            update={"name": resolve_i18n(t.name_i18n, lang, DEFAULT_LANG, t.name)}
        )
        for t in templates
    ]


@router.post("", response_model=JourneyTemplateResponse, status_code=201)
async def create_template(
    body: JourneyTemplateCreateRequest, agent: AgentDep, db: DbDep
) -> JourneyTemplateResponse:
    template = await JourneysManager(db).create_template(
        agent,
        body.name,
        body.name_i18n,
        auto_reminder_days_1=body.auto_reminder_days_1,
        auto_reminder_days_2=body.auto_reminder_days_2,
    )
    return JourneyTemplateResponse.model_validate(template)


@router.post("/import", response_model=JourneyImportReport)
async def import_journey(
    agent: AgentDep,
    db: DbDep,
    body: JourneyImportRequest,
    preview: bool = False,
) -> JourneyImportReport:
    """Create a journey template from the JSON the agency's own AI
    produced (deterministic interpreter, zero LLM call here). Partial
    import: an invalid step is rejected with its import_ai.* code while
    coherent steps are created; a globally invalid JSON is a 422.
    ?preview=true validates and reports without writing anything.
    `provider_assignments` resolves external slots: an assigned job
    becomes a real participant and drops out of external_slots."""
    return await JourneyImportManager(db).run(
        agent,
        body.parcours,
        preview=preview,
        provider_assignments=body.provider_assignments,
    )


@router.post("/{template_id}/import", response_model=JourneyImportReport)
async def regenerate_journey(
    template_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    body: JourneyImportRequest,
    preview: bool = False,
) -> JourneyImportReport:
    """Re-generation (demande Nicolas) : REMPLACE le contenu du template
    depuis un nouveau JSON — uniquement s'il n'a AUCUN dossier (actifs +
    archives), garde verifiee en transaction (verrou + re-check). 409
    journey.in_use sinon. Les pertes (pieces jointes du modele,
    traductions) sont annoncees dans les warnings du rapport."""
    return await JourneyImportManager(db).run(
        agent,
        body.parcours,
        preview=preview,
        provider_assignments=body.provider_assignments,
        replace_template_id=template_id,
    )


@router.post("/{template_id}/translate", response_model=TranslationJobResponse, status_code=202)
async def translate_template(
    template_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    background: BackgroundTasks,
    body: JourneyTranslateRequest | None = None,
) -> TranslationJobResponse:
    """START an async AI translation of the EMPTY language variants
    (default), or empty + STALE ones with include_stale=True (a stale
    variant = AI-written, untouched by a human, whose source drifted —
    it gets overwritten; human work never does). retranslate_langs is
    the CONSENTED overwrite: those languages regenerate EVERY field,
    human retouches included — the front confirms explicitly, the back
    never infers it. Quota gated BEFORE launch; answers 202 with
    translation_job_id — the agency keeps working while the front polls
    /journeys/translate-jobs/{id} (one lot = one language = real
    progress grain)."""
    target_langs = body.target_langs if body is not None else None
    langs: list[str] | None = [str(lang) for lang in target_langs] if target_langs else None
    include_stale = body.include_stale if body is not None else False
    retranslate = (
        [str(lang) for lang in body.retranslate_langs]
        if body is not None and body.retranslate_langs
        else None
    )
    job = await TranslationManager(db).start_translation(
        agent, template_id, langs, include_stale=include_stale, retranslate_langs=retranslate
    )
    background.add_task(execute_job, job.id, agent, include_stale, retranslate)
    return job_response(job)


@router.get("/{template_id}/translate/estimate", response_model=TranslateEstimateResponse)
async def translate_estimate(
    template_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    include_stale: bool = False,
    retranslate_langs: Annotated[list[Language] | None, Query()] = None,
) -> TranslateEstimateResponse:
    """The honest pre-launch number for the front's modal. `counts`
    reports the per-language {empty, stale} split in both modes;
    `items`/`estimated_points` follow the requested mode — with
    retranslate_langs, items covers EVERY field of those languages."""
    return await TranslationManager(db).estimate(
        agent,
        template_id,
        None,
        include_stale=include_stale,
        retranslate_langs=[str(lang) for lang in retranslate_langs] if retranslate_langs else None,
    )


@router.get("/translate-jobs/{job_id}", response_model=TranslationJobResponse)
async def get_translate_job(
    job_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> TranslationJobResponse:
    """Polling read: status + progress {done,total} + points_charged."""
    return await TranslationManager(db).get_job(agent, job_id)


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
    template_id: uuid.UUID, agent: AgentDep, db: DbDep, lang: RequestLang
) -> JourneyTemplateDetailResponse:
    return await JourneysManager(db).get_template_detail(agent, template_id, lang)


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


# --- planned costs (template step) — cost.manage; read is embedded in the detail
@router.post(
    "/{template_id}/steps/{step_id}/planned-costs",
    response_model=PlannedCostResponse,
    status_code=201,
)
async def add_planned_cost(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    body: PlannedCostCreateRequest,
    agent: AgentDep,
    db: DbDep,
) -> PlannedCostResponse:
    return await JourneysManager(db).add_planned_cost(agent, template_id, step_id, body)


@router.patch("/{template_id}/planned-costs/{cost_id}", response_model=PlannedCostResponse)
async def update_planned_cost(
    template_id: uuid.UUID,
    cost_id: uuid.UUID,
    body: PlannedCostUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> PlannedCostResponse:
    return await JourneysManager(db).update_planned_cost(agent, template_id, cost_id, body)


@router.delete("/{template_id}/planned-costs/{cost_id}", response_model=MessageResponse)
async def delete_planned_cost(
    template_id: uuid.UUID, cost_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await JourneysManager(db).delete_planned_cost(agent, template_id, cost_id)
    return MessageResponse(detail="Planned cost deleted.")


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


@router.get(
    "/{template_id}/steps/{step_id}/requirements/{requirement_id}/impact",
    response_model=RequirementImpactResponse,
)
async def requirement_impact(
    template_id: uuid.UUID,
    step_id: uuid.UUID,
    requirement_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
) -> RequirementImpactResponse:
    """Read-only pre-delete impact: how many cases already carry a client
    response for this requirement, so the front confirms strongly before
    the destructive delete."""
    return await JourneysManager(db).requirement_impact(agent, template_id, step_id, requirement_id)


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
