import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.core.enums import DocValidationStatus


class DocumentResponse(BaseModel):
    """`storage_path` is deliberately NOT exposed — the path is
    technical, the (original) filename is the display data."""

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


class DocumentValidationRequest(BaseModel):
    validation_status: DocValidationStatus
    expires_at: datetime | None = None
