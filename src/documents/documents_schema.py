import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.core.enums import DocValidationStatus


class DocumentResponse(BaseModel):
    """Agent-facing document. `storage_path` is deliberately NOT exposed —
    the path is technical, the (original) filename is the display data.
    The enrichment fields (default empty so a bare `model_validate(document)`
    on the single-upload responses still works) carry the aggregated-view
    context: resolved step name, the requirement this doc answers (if any),
    and the linked-vs-free classifier `is_requirement`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    case_id: uuid.UUID
    step_progress_id: uuid.UUID | None
    filename: str
    uploaded_by_type: str
    uploaded_by_id: uuid.UUID
    validation_status: str | None
    expires_at: datetime | None
    created_at: datetime
    # Aggregated-view context (populated by the list path).
    step_name: str | None = None
    requirement_reference: str | None = None
    is_requirement: bool = False


class ExpatDocumentResponse(BaseModel):
    """Client-facing document — the exclusion contract: NO internal UUID
    (no uploaded_by_id). The uploader is conveyed by `uploaded_by_type`
    + `is_mine` (the front renders "Vous" / "Votre conseiller"). Same
    aggregated-view context as the agent face."""

    id: uuid.UUID
    case_id: uuid.UUID
    filename: str
    uploaded_by_type: str  # agent | expat
    is_mine: bool
    validation_status: str | None
    expires_at: datetime | None
    created_at: datetime
    step_name: str | None
    requirement_reference: str | None
    is_requirement: bool


class DocumentValidationRequest(BaseModel):
    validation_status: DocValidationStatus
    expires_at: datetime | None = None
