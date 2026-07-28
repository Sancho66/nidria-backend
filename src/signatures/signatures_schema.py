"""Schémas du domaine signatures (méga-lot 28/07)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ExpatSignatureResponse(BaseModel):
    """UNE tâche de signature de LA personne qui regarde — jamais celle
    d'un autre (le slug est personnel : il ouvre SA session de signature).
    `embed_slug` est la ref opaque provider pour l'embed front."""

    model_config = ConfigDict(from_attributes=True)

    signer_id: uuid.UUID
    request_id: uuid.UUID
    reference: str
    status: str  # SignatureSignerStatus
    request_status: str  # SignatureRequestStatus
    embed_slug: str | None
    expires_at: datetime | None
    signed_at: datetime | None


class WebhookAckResponse(BaseModel):
    status: str
