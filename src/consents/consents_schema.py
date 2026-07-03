import uuid
from datetime import datetime

from pydantic import BaseModel

from src.core.enums import ConsentDocumentType


class PendingDocumentResponse(BaseModel):
    """An active document awaiting acceptance. `content` is content_md
    with the {agency_name} token resolved; `content_hash` covers the RAW
    text (what the acceptance will carry)."""

    type: str
    version: int
    content: str
    content_hash: str


class ExpatAgencyPendingResponse(BaseModel):
    """Pending documents of ONE agency (the expat accepts per agency)."""

    agency_id: uuid.UUID
    agency_name: str
    documents: list[PendingDocumentResponse]


class ConsentAcceptRequest(BaseModel):
    document_type: ConsentDocumentType
    document_version: int
    # Required on the expat face (which agency the acceptance binds);
    # ignored on the agent face (always the agent's own agency).
    agency_id: uuid.UUID | None = None


class ConsentAcceptResponse(BaseModel):
    document_type: str
    document_version: int
    accepted_at: datetime
    # True when this exact acceptance already existed (idempotent no-op).
    already_accepted: bool
