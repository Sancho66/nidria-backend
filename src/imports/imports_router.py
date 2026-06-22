"""Endpoints for the CRM import feature.

- BLOC 1: read-only CRM referential (served from memory, no DB).
- BLOC 2: the transactional case import (POST /imports/cases).
- BLOC 3: saved-mapping CRUD (/imports/mappings) — agency-scoped.

All routes are gated by `import.manage` (Audience.AGENT): admin and
case_manager hold it by default, viewer/member do not.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.email import send_email
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.imports.case_import_manager import CaseImportManager
from src.imports.case_import_schema import CaseImportRequest, ImportPreview, ImportReport
from src.imports.imports_manager import ImportsManager
from src.imports.imports_schema import CrmDetailResponse, CrmListResponse
from src.imports.mapping_manager import MappingManager
from src.imports.mapping_schema import MappingListResponse, MappingResponse, MappingUpsertRequest

router = APIRouter(prefix="/imports", tags=["imports"])

BINDINGS = [
    RouteBinding("GET", "/imports/crms", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("GET", "/imports/crms/{slug}", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("POST", "/imports/cases", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("POST", "/imports/cases/preview", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("GET", "/imports/mappings", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("GET", "/imports/mappings/resolve", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("POST", "/imports/mappings", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding(
        "DELETE", "/imports/mappings/{mapping_id}", Audience.AGENT, Permission.IMPORT_MANAGE
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/crms", response_model=CrmListResponse)
async def list_crms() -> CrmListResponse:
    return ImportsManager().list_crms()


@router.get("/crms/{slug}", response_model=CrmDetailResponse)
async def get_crm(slug: str) -> CrmDetailResponse:
    return ImportsManager().get_crm(slug)


@router.post("/cases", response_model=ImportReport)
async def import_cases(
    body: CaseImportRequest,
    agent: AgentDep,
    db: DbDep,
    background: BackgroundTasks,
) -> ImportReport:
    """Create N dossiers from a CSV + mapping. Returns the report
    immediately; invitation emails are dispatched OUT of the request via
    BackgroundTasks (never N synchronous sends in the request path)."""
    report, pending = await CaseImportManager(db).run_import(agent, body)
    for mail in pending:
        background.add_task(send_email, mail.to, mail.subject, mail.text, mail.html)
    return report


@router.post("/cases/preview", response_model=ImportPreview)
async def preview_import_cases(
    body: CaseImportRequest, agent: AgentDep, db: DbDep
) -> ImportPreview:
    """Dry-run: validate the CSV + mapping and report each row's PREDICTED
    outcome WITHOUT creating any dossier or queuing any email — no
    BackgroundTasks, no commit, strictly read-only."""
    return await CaseImportManager(db).preview_import(agent, body)


# --- saved mappings (BLOC 3) ---------------------------------------------------------


@router.get("/mappings", response_model=MappingListResponse)
async def list_mappings(
    agent: AgentDep,
    db: DbDep,
    journey_template_id: uuid.UUID | None = None,
    crm_slug: str | None = None,
) -> MappingListResponse:
    return await MappingManager(db).list(
        agent, journey_template_id=journey_template_id, crm_slug=crm_slug
    )


@router.get("/mappings/resolve", response_model=MappingResponse)
async def resolve_mapping(
    agent: AgentDep,
    db: DbDep,
    journey_template_id: uuid.UUID,
    crm_slug: str,
) -> MappingResponse:
    """The applicable mapping for (parcours, crm) — to pre-fill the import."""
    return await MappingManager(db).resolve(agent, journey_template_id, crm_slug)


@router.post("/mappings", response_model=MappingResponse)
async def upsert_mapping(body: MappingUpsertRequest, agent: AgentDep, db: DbDep) -> MappingResponse:
    return await MappingManager(db).upsert(agent, body)


@router.delete("/mappings/{mapping_id}", status_code=204)
async def delete_mapping(mapping_id: uuid.UUID, agent: AgentDep, db: DbDep) -> None:
    await MappingManager(db).delete(agent, mapping_id)
