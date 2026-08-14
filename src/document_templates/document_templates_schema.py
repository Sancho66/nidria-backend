"""Schémas de la bibliothèque de modèles de documents (méga-lot 29/07)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.core.enums import DocumentTemplateState


class DocumentTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    filename: str
    provider: str
    # Le builder a-t-il été sauvegardé au moins une fois ? (constat du
    # dernier sync) — false : le modèle n'est PAS envoyable (422 au send).
    fields_configured: bool
    # roles_count = rôles CLIENTS seulement ; le contreseing agence est
    # porté à part (rôle provider « Agence », jamais compté ici).
    agency_countersigns: bool
    roles_count: int
    # Dans la bibliothèque, ou brouillon jamais commencé (lot 14/08). La
    # LISTE ne sert que des `active` — ce champ est là pour le POST de
    # création et le builder-sync, qui rendent tous deux un modèle que le
    # front tient en main sans qu'il soit encore dans la liste. C'est lui qui
    # dit au sélecteur d'étape s'il doit délier une ligne quand le builder se
    # ferme sans qu'aucune zone n'ait été posée.
    state: DocumentTemplateState
    created_at: datetime
    updated_at: datetime


class DocumentTemplateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    agency_countersigns: bool | None = None


class BuilderTokenResponse(BaseModel):
    """Jeton court pour le builder embeddé du provider — scoped au modèle
    (external_id = l'UUID du modèle, la clé find-or-create constatée)."""

    token: str
    provider: str
