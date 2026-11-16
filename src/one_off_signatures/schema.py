import uuid
from datetime import datetime

from pydantic import BaseModel


class OneOffSignatureResponse(BaseModel):
    id: uuid.UUID
    case_id: uuid.UUID
    progress_id: uuid.UUID
    person_id: uuid.UUID
    name: str
    filename: str
    fields_configured: bool
    submitted_at: datetime | None
    request_id: uuid.UUID | None
    status: str
