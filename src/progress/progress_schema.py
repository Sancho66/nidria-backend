import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.core.enums import ResponsibleType, StepStatus


class AssignJourneyRequest(BaseModel):
    journey_template_id: uuid.UUID


class BlockingStep(BaseModel):
    template_step_id: uuid.UUID
    name: str


class RequirementStateResponse(BaseModel):
    """A concrete requirement on an active step (NEW WAVE, read-only).
    `status` is derived from the live person value for base/custom
    fields, explicit for documents. `is_archived` flags a custom-field
    whose definition the agency has archived."""

    id: uuid.UUID
    person_id: uuid.UUID
    # Resolved display name of the concerned person — PRINCIPAL via the
    # shared expat_user (its full_name column is NULL), FAMILY via its
    # own full_name. Never empty for a person that exists.
    person_label: str
    kind: str
    reference: str
    scope: str
    status: str  # pending / provided (computed)
    # Live value at the source (case_person column / custom_fields JSONB)
    # for base/custom fields; None for documents and when pending.
    value: Any = None
    is_archived: bool
    document_id: uuid.UUID | None


class StepProgressResponse(BaseModel):
    id: uuid.UUID
    template_step_id: uuid.UUID
    name: str
    position: int
    estimated_days: int | None
    status: str  # PROJECTED: todo / in_progress / done / blocked
    responsible_type: str | None
    responsible_agent_id: uuid.UUID | None
    responsible_external_id: uuid.UUID | None
    completed_at: datetime | None
    completed_by_agent_id: uuid.UUID | None
    # Unfinished prerequisites (ids + names, front-displayable). Drives
    # the BLOCKED projection on TODO steps; informative on IN_PROGRESS.
    blocked_by: list[BlockingStep]
    # Step requirements (NEW WAVE). `completion_mode` from the template;
    # `requirements` are the concrete materialized requirements (empty
    # until the step has been activated); `all_requirements_met` is the
    # aggregate (vacuously true when there are none).
    completion_mode: str
    requirements: list[RequirementStateResponse]
    all_requirements_met: bool
    # VAGUE 5: non-deleted comment count for a "X messages" badge without
    # listing the thread (batched COUNT, no N+1).
    comment_count: int


class StepProgressUpdateRequest(BaseModel):
    """Status transitions and/or responsible assignment. Unset fields
    are untouched (model_fields_set semantics); `responsible_type=None`
    explicitly CLEARS the responsible."""

    status: StepStatus | None = None
    responsible_type: ResponsibleType | None = None
    responsible_agent_id: uuid.UUID | None = None
    responsible_external_id: uuid.UUID | None = None
