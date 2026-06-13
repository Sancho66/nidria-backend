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
    completion_mode: CompletionMode = CompletionMode.AGENCY_VALIDATION


class TemplateStepUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    estimated_days: int | None = Field(default=None, ge=0)
    default_responsible_type: ResponsibleType | None = None
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
    completion_mode: str
    prerequisite_step_ids: list[uuid.UUID]


class JourneyTemplateDetailResponse(BaseModel):
    id: uuid.UUID
    name: str
    steps: list[TemplateStepResponse]
