import uuid
from datetime import datetime

from pydantic import BaseModel

from src.core.enums import ResponsibleType, StepStatus


class AssignJourneyRequest(BaseModel):
    journey_template_id: uuid.UUID


class BlockingStep(BaseModel):
    template_step_id: uuid.UUID
    name: str


class StepProgressResponse(BaseModel):
    id: uuid.UUID
    template_step_id: uuid.UUID
    name: str
    position: int
    estimated_days: int | None
    # Read from the template step (informative at MVP — the lock stays
    # prerequisites only).
    required_documents: list[str]
    status: str  # PROJECTED: todo / in_progress / done / blocked
    responsible_type: str | None
    responsible_agent_id: uuid.UUID | None
    responsible_external_id: uuid.UUID | None
    completed_at: datetime | None
    completed_by_agent_id: uuid.UUID | None
    # Unfinished prerequisites (ids + names, front-displayable). Drives
    # the BLOCKED projection on TODO steps; informative on IN_PROGRESS.
    blocked_by: list[BlockingStep]


class StepProgressUpdateRequest(BaseModel):
    """Status transitions and/or responsible assignment. Unset fields
    are untouched (model_fields_set semantics); `responsible_type=None`
    explicitly CLEARS the responsible."""

    status: StepStatus | None = None
    responsible_type: ResponsibleType | None = None
    responsible_agent_id: uuid.UUID | None = None
    responsible_external_id: uuid.UUID | None = None
