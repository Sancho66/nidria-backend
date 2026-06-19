import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.i18n import RequestLang, resolve_i18n
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.custom_fields.custom_fields_manager import CustomFieldsManager
from src.custom_fields.custom_fields_schema import (
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionResponse,
    CustomFieldDefinitionUpdate,
)

router = APIRouter(prefix="/agencies/me/custom-fields", tags=["custom-fields"])

# Read = case.view (every agent rendering the person form needs the
# definitions); mutations = field.manage (admin config). Same
# read/manage split as roles.
BINDINGS = [
    RouteBinding("GET", "/agencies/me/custom-fields", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding("POST", "/agencies/me/custom-fields", Audience.AGENT, Permission.FIELD_MANAGE),
    RouteBinding(
        "PATCH", "/agencies/me/custom-fields/{field_id}", Audience.AGENT, Permission.FIELD_MANAGE
    ),
    RouteBinding(
        "POST",
        "/agencies/me/custom-fields/{field_id}/archive",
        Audience.AGENT,
        Permission.FIELD_MANAGE,
    ),
    RouteBinding(
        "POST",
        "/agencies/me/custom-fields/{field_id}/unarchive",
        Audience.AGENT,
        Permission.FIELD_MANAGE,
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("", response_model=list[CustomFieldDefinitionResponse])
async def list_custom_fields(
    agent: AgentDep,
    db: DbDep,
    lang: RequestLang,
    include_archived: Annotated[bool, Query()] = False,
) -> list[CustomFieldDefinitionResponse]:
    mgr = CustomFieldsManager(db)
    definitions = await mgr.list_definitions(agent, include_archived=include_archived)
    agency_default = await mgr.agency_default(agent.agency_id)
    # i18n: resolve the LABEL for the display language (the `key` stays raw).
    return [
        CustomFieldDefinitionResponse.model_validate(d).model_copy(
            update={"label": resolve_i18n(d.label_i18n, lang, agency_default, d.label)}
        )
        for d in definitions
    ]


@router.post("", response_model=CustomFieldDefinitionResponse, status_code=201)
async def create_custom_field(
    body: CustomFieldDefinitionCreate, agent: AgentDep, db: DbDep
) -> CustomFieldDefinitionResponse:
    definition = await CustomFieldsManager(db).create(agent, body)
    return CustomFieldDefinitionResponse.model_validate(definition)


@router.patch("/{field_id}", response_model=CustomFieldDefinitionResponse)
async def update_custom_field(
    field_id: uuid.UUID, body: CustomFieldDefinitionUpdate, agent: AgentDep, db: DbDep
) -> CustomFieldDefinitionResponse:
    """key and field_type are immutable — archive + recreate to change
    a type."""
    definition = await CustomFieldsManager(db).update(agent, field_id, body)
    return CustomFieldDefinitionResponse.model_validate(definition)


@router.post("/{field_id}/archive", response_model=CustomFieldDefinitionResponse)
async def archive_custom_field(
    field_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> CustomFieldDefinitionResponse:
    """Soft archive (the only removal). Saved values are kept."""
    definition = await CustomFieldsManager(db).archive(agent, field_id)
    return CustomFieldDefinitionResponse.model_validate(definition)


@router.post("/{field_id}/unarchive", response_model=CustomFieldDefinitionResponse)
async def unarchive_custom_field(
    field_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> CustomFieldDefinitionResponse:
    """Resurrect an archived field — it reappears in forms and its kept
    JSONB values become exposed/validable again. Idempotent."""
    definition = await CustomFieldsManager(db).unarchive(agent, field_id)
    return CustomFieldDefinitionResponse.model_validate(definition)
