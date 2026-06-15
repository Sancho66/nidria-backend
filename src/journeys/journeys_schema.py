import uuid

from pydantic import BaseModel, ConfigDict, Field

from src.core.enums import (
    CompletionMode,
    ResponsibleType,
    StepRequirementKind,
    StepRequirementScope,
)


class JourneyTemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class JourneyTemplateUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class TemplateStepCreateRequest(BaseModel):
    """No `position`: new steps are APPENDED; ordering is managed only
    through the declarative reorder endpoint."""

    name: str = Field(min_length=1, max_length=200)
    estimated_days: int | None = Field(default=None, ge=0)
    default_responsible_type: ResponsibleType | None = None
    # Wave C: a named default responsible — a precise INTERNAL agent only
    # (validated in the manager; externals exist only at the case level).
    default_responsible_agent_id: uuid.UUID | None = None
    completion_mode: CompletionMode = CompletionMode.AGENCY_VALIDATION


class TemplateStepUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    estimated_days: int | None = Field(default=None, ge=0)
    default_responsible_type: ResponsibleType | None = None
    default_responsible_agent_id: uuid.UUID | None = None
    completion_mode: CompletionMode | None = None


class StepRequirementCreateRequest(BaseModel):
    kind: StepRequirementKind
    reference: str = Field(min_length=1, max_length=100)
    scope: StepRequirementScope
    position: int = 0


class StepRequirementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_id: uuid.UUID
    kind: str
    reference: str
    scope: str
    position: int


class StepOrderRequest(BaseModel):
    """Full list of the template's step ids in the desired order."""

    step_ids: list[uuid.UUID]


class StepRequirementOrderRequest(BaseModel):
    """Full list of the step's requirement ids in the desired order
    (same convention as StepOrderRequest, one level down)."""

    requirement_ids: list[uuid.UUID]


class StepPrerequisitesRequest(BaseModel):
    """Declarative: replaces the step's full prerequisite set — the
    whole template graph is re-validated on every mutation."""

    prerequisite_step_ids: list[uuid.UUID]


# --- per-template field collection (NEW WAVE) ----------------------------------------


class TemplateFieldCreateRequest(BaseModel):
    """Attach a field to a template's creation form. `kind` is base_field
    or custom_field (document is a requirement, not a creation field —
    rejected in the manager)."""

    kind: StepRequirementKind
    reference: str = Field(min_length=1, max_length=100)
    required_at_creation: bool = False
    position: int = 0


class TemplateFieldResponse(BaseModel):
    """A template's creation field with its RESOLVED render metadata
    (label/field_type/options for a custom field, batched at read; base
    fields carry none — the frontend knows the civil-status set). A
    custom field whose definition was archived after attachment stays in
    the list, flagged `is_archived` (mirrors requirements)."""

    id: uuid.UUID
    template_id: uuid.UUID
    kind: str
    reference: str
    position: int
    required_at_creation: bool
    label: str | None
    field_type: str | None
    options: list[str] | None
    is_archived: bool


class TemplateFieldOrderRequest(BaseModel):
    """Full list of the template's field ids in the desired order (same
    convention as StepOrderRequest / StepRequirementOrderRequest)."""

    field_ids: list[uuid.UUID]


class JourneyTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class TemplateStepResponse(BaseModel):
    id: uuid.UUID
    name: str
    position: int
    estimated_days: int | None
    default_responsible_type: str | None
    default_responsible_agent_id: uuid.UUID | None
    completion_mode: str
    prerequisite_step_ids: list[uuid.UUID]


class JourneyTemplateDetailResponse(BaseModel):
    id: uuid.UUID
    name: str
    steps: list[TemplateStepResponse]
    # Fields collected at case creation (NEW WAVE) — embedded like steps.
    fields: list[TemplateFieldResponse]
