import uuid

from pydantic import BaseModel, ConfigDict, Field

from src.core.enums import ResponsibleType


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
    # Free labels of the expected pieces — informative at MVP.
    required_documents: list[str] = Field(default_factory=list)


class TemplateStepUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    estimated_days: int | None = Field(default=None, ge=0)
    default_responsible_type: ResponsibleType | None = None
    required_documents: list[str] | None = None


class StepOrderRequest(BaseModel):
    """Full list of the template's step ids in the desired order."""

    step_ids: list[uuid.UUID]


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
    required_documents: list[str]
    prerequisite_step_ids: list[uuid.UUID]


class JourneyTemplateDetailResponse(BaseModel):
    id: uuid.UUID
    name: str
    steps: list[TemplateStepResponse]
