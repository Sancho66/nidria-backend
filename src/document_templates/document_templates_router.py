"""Endpoints de la bibliothèque de modèles de documents (méga-lot 29/07).

Tout est gaté flag effectif (le manager) + `journey.configure` (les
bindings) : configurer les modèles à signer EST de la configuration de
parcours. Le builder embeddé du provider s'ouvre avec un jeton court
scoped au modèle ; le front appelle builder-sync sur l'événement save."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.document_templates.document_templates_manager import DocumentTemplatesManager
from src.document_templates.document_templates_schema import (
    BuilderTokenResponse,
    DocumentTemplateResponse,
    DocumentTemplateUpdateRequest,
)

router = APIRouter(prefix="/document-templates", tags=["document-templates"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]

BINDINGS = [
    RouteBinding("GET", "/document-templates", Audience.AGENT, Permission.JOURNEY_CONFIGURE),
    RouteBinding("POST", "/document-templates", Audience.AGENT, Permission.JOURNEY_CONFIGURE),
    RouteBinding(
        "GET", "/document-templates/{template_id}", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "PATCH", "/document-templates/{template_id}", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "DELETE", "/document-templates/{template_id}", Audience.AGENT, Permission.JOURNEY_CONFIGURE
    ),
    RouteBinding(
        "POST",
        "/document-templates/{template_id}/builder-token",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
    RouteBinding(
        "POST",
        "/document-templates/{template_id}/builder-sync",
        Audience.AGENT,
        Permission.JOURNEY_CONFIGURE,
    ),
]


@router.get("", response_model=list[DocumentTemplateResponse])
async def list_document_templates(agent: AgentDep, db: DbDep) -> list[DocumentTemplateResponse]:
    templates = await DocumentTemplatesManager(db).list(agent)
    return [DocumentTemplateResponse.model_validate(t) for t in templates]


@router.post("", response_model=DocumentTemplateResponse, status_code=201)
async def create_document_template(
    name: Annotated[str, Form(min_length=1, max_length=200)],
    file: UploadFile,
    agent: AgentDep,
    db: DbDep,
    agency_countersigns: Annotated[bool, Form()] = False,
) -> DocumentTemplateResponse:
    """Le PDF source (stocké chez nous) + le template provider naissent
    ensemble — sans zones : elles sont l'affaire du builder."""
    template = await DocumentTemplatesManager(db).create(
        agent,
        name=name,
        filename=file.filename or "document.pdf",
        content=await file.read(),
        content_type=file.content_type,
        agency_countersigns=agency_countersigns,
    )
    return DocumentTemplateResponse.model_validate(template)


@router.get("/{template_id}", response_model=DocumentTemplateResponse)
async def get_document_template(
    template_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> DocumentTemplateResponse:
    template = await DocumentTemplatesManager(db).get(agent, template_id)
    return DocumentTemplateResponse.model_validate(template)


@router.patch("/{template_id}", response_model=DocumentTemplateResponse)
async def update_document_template(
    template_id: uuid.UUID,
    payload: DocumentTemplateUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> DocumentTemplateResponse:
    template = await DocumentTemplatesManager(db).rename(
        agent, template_id, payload.name, agency_countersigns=payload.agency_countersigns
    )
    return DocumentTemplateResponse.model_validate(template)


@router.delete("/{template_id}", status_code=204)
async def delete_document_template(template_id: uuid.UUID, agent: AgentDep, db: DbDep) -> None:
    """409 nommé (références listées) si une exigence signable — définition
    de parcours ou ligne pendante d'un dossier — pointe encore le modèle."""
    await DocumentTemplatesManager(db).delete(agent, template_id)


@router.post("/{template_id}/builder-token", response_model=BuilderTokenResponse)
async def create_builder_token(
    template_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> BuilderTokenResponse:
    manager = DocumentTemplatesManager(db)
    token = await manager.builder_token(agent, template_id)
    template = await manager.get(agent, template_id)
    return BuilderTokenResponse(token=token, provider=template.provider)


@router.post("/{template_id}/builder-sync", response_model=DocumentTemplateResponse)
async def sync_after_builder_save(
    template_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> DocumentTemplateResponse:
    """À appeler sur l'événement save du composant builder : constate les
    zones/rôles posés chez le provider (fields_configured, roles_count)."""
    template = await DocumentTemplatesManager(db).builder_sync(agent, template_id)
    return DocumentTemplateResponse.model_validate(template)
