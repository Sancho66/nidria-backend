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
    # LOT 6 (point 3) : le « Signé n/m » de la demande — le principal voit
    # « en attente des autres signataires » sans autre appel.
    # Comptes CLIENTS seulement (mini-lot 30/07) : le siège agence n'entre
    # pas dans le n/m — « où en sont les signataires » parle des co-
    # signataires du dossier ; la part agence est une AUTRE nature
    # d'attente, portée par awaiting_agency.
    request_signed_count: int = 0
    request_signer_total: int = 0
    # Toutes les signatures clients posées, seule celle de l'agence manque.
    awaiting_agency: bool = False


class WebhookAckResponse(BaseModel):
    status: str


class SignatureCreditPackResponse(BaseModel):
    """Un pack de la grille (config SIGNATURE_CREDIT_PACKS) — le prix vit
    chez Paddle et le contrat le SERT (extension 30/07 : relu avec cache
    TTL, jamais un montant en dur) ; None = Paddle injoignable, le front
    garde son fallback sans prix."""

    price_id: str
    credits: int
    # Centimes (le format Paddle) + code devise — pour le prix par crédit
    # et les économies calculés côté front sans montant en dur.
    unit_amount: int | None = None
    currency: str | None = None


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
