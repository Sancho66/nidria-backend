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
    # La ligne d'exigence de la personne (TEMPS 2, point 5) : le front range
    # « À signer » dans le groupe de SA personne — résorbe la déviation
    # case-level. None si la ligne a disparu (SET NULL défensif).
    requirement_id: uuid.UUID | None
    reference: str
    status: str  # SignatureSignerStatus
    request_status: str  # SignatureRequestStatus
    embed_slug: str | None
    expires_at: datetime | None
    signed_at: datetime | None


class WebhookAckResponse(BaseModel):
    status: str


class SignatureCreditPackResponse(BaseModel):
    """Un pack de la grille (config SIGNATURE_CREDIT_PACKS) — le prix vit
    chez Paddle, on expose price_id + crédits."""

    price_id: str
    credits: int


class SignatureCreditsResponse(BaseModel):
    """GET /agencies/me/signature-credits — lisible par TOUT agent :
    l'activation d'une étape signable peut échouer sur le solde, chacun
    doit pouvoir le lire (précédent trial_ends_at)."""

    available: int
    reserved: int
    low_threshold: int
    packs: list[SignatureCreditPackResponse]


class SignatureCreditEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str  # purchase | reserve | consume | release
    amount: int
    signature_request_id: uuid.UUID | None
    created_at: datetime
    details: dict[str, object]


class SignatureCreditEntriesResponse(BaseModel):
    items: list[SignatureCreditEntryResponse]
    total: int
    page: int
    page_size: int
