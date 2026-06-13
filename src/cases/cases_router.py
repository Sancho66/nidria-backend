import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.auth.auth_schema import MessageResponse
from src.cases.cases_manager import CasesManager, parse_sorts
from src.cases.cases_schema import (
    BulkActionRequest,
    BulkActionResponse,
    BulkDeleteRequest,
    CaseCreateRequest,
    CaseDetailResponse,
    CaseFilters,
    CaseListResponse,
    CaseNoteCreateRequest,
    CaseNoteResponse,
    CaseNoteUpdateRequest,
    CaseResponse,
    CaseUpdateRequest,
    ExternalContactCreateRequest,
    ExternalContactResponse,
    ExternalContactUpdateRequest,
    FamilyMemberRequest,
    FamilyMemberResponse,
)
from src.cases.filter_schema import AdvancedFilters
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience, CaseStatus
from src.core.exceptions import ValidationError
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

router = APIRouter(prefix="/cases", tags=["cases"])


def _parse_advanced_filters(raw: str | None) -> AdvancedFilters | None:
    """Decode the JSON-encoded `filters` query param into the validated
    AdvancedFilters tree (Prism). Malformed JSON or shape → 422."""
    if raw is None or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"`filters` is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("`filters` must be a JSON object")
    try:
        return AdvancedFilters(**payload)
    except PydanticValidationError as exc:
        raise ValidationError(f"Invalid filter tree: {exc}") from exc


_VIEW = Permission.CASE_VIEW
_EDIT = Permission.CASE_EDIT
_DELETE = Permission.CASE_DELETE

BINDINGS = [
    RouteBinding("POST", "/cases", Audience.AGENT, _EDIT),
    RouteBinding("GET", "/cases", Audience.AGENT, _VIEW),
    # Bulk: edit-actions gate case.edit, soft delete gates case.delete —
    # one binding per route, the engine does the gating (Prism splits
    # bulk-action / bulk-delete the same way).
    RouteBinding("POST", "/cases/bulk-action", Audience.AGENT, _EDIT),
    RouteBinding("POST", "/cases/bulk-delete", Audience.AGENT, _DELETE),
    RouteBinding("GET", "/cases/{case_id}", Audience.AGENT, _VIEW),
    RouteBinding("PATCH", "/cases/{case_id}", Audience.AGENT, _EDIT),
    RouteBinding("GET", "/cases/{case_id}/export", Audience.AGENT, _VIEW),
    RouteBinding("POST", "/cases/{case_id}/family", Audience.AGENT, _EDIT),
    RouteBinding("PATCH", "/cases/{case_id}/family/{member_id}", Audience.AGENT, _EDIT),
    RouteBinding("DELETE", "/cases/{case_id}/family/{member_id}", Audience.AGENT, _EDIT),
    RouteBinding("POST", "/cases/{case_id}/external-contacts", Audience.AGENT, _EDIT),
    RouteBinding("PATCH", "/cases/{case_id}/external-contacts/{contact_id}", Audience.AGENT, _EDIT),
    RouteBinding(
        "DELETE", "/cases/{case_id}/external-contacts/{contact_id}", Audience.AGENT, _EDIT
    ),
    RouteBinding("GET", "/cases/{case_id}/notes", Audience.AGENT, _VIEW),
    RouteBinding("POST", "/cases/{case_id}/notes", Audience.AGENT, _EDIT),
    RouteBinding("PATCH", "/cases/{case_id}/notes/{note_id}", Audience.AGENT, _EDIT),
    RouteBinding("DELETE", "/cases/{case_id}/notes/{note_id}", Audience.AGENT, _EDIT),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


# --- cases ----------------------------------------------------------------------


@router.post("", response_model=CaseResponse, status_code=201)
async def create_case(body: CaseCreateRequest, agent: AgentDep, db: DbDep) -> CaseResponse:
    case = await CasesManager(db).create_case(agent, body)
    return CaseResponse.model_validate(case)


# --- bulk actions (literal segments: declared before /{case_id}) ----------------


@router.post("/bulk-action", response_model=BulkActionResponse)
async def bulk_action(body: BulkActionRequest, agent: AgentDep, db: DbDep) -> BulkActionResponse:
    """Edit-actions on a selection of cases (gate case.edit). The
    `action` discriminator routes to set_status / set_owner / add_tags /
    remove_tags. Cross-agency ids are silently ignored; affected_ids
    lets the frontend refresh and deselect."""
    manager = CasesManager(db)
    if body.action == "set_status":
        return await manager.bulk_set_status(agent, body.case_ids, body.status.value)
    if body.action == "set_owner":
        return await manager.bulk_set_owner(agent, body.case_ids, body.owner_agent_id)
    if body.action == "add_tags":
        return await manager.bulk_add_tags(agent, body.case_ids, body.tags)
    return await manager.bulk_remove_tags(agent, body.case_ids, body.tags)


@router.post("/bulk-delete", response_model=BulkActionResponse)
async def bulk_delete(body: BulkDeleteRequest, agent: AgentDep, db: DbDep) -> BulkActionResponse:
    """Soft-delete a selection of cases (gate case.delete). Re-deleting
    an already-deleted case is a no-op."""
    return await CasesManager(db).bulk_delete(agent, body.case_ids)


@router.get("", response_model=CaseListResponse)
async def list_cases(
    agent: AgentDep,
    db: DbDep,
    status: Annotated[list[CaseStatus] | None, Query()] = None,
    origin_country: Annotated[str | None, Query(pattern=r"^[A-Z]{2}$")] = None,
    dest_country: Annotated[str | None, Query(pattern=r"^[A-Z]{2}$")] = None,
    owner_agent_id: Annotated[uuid.UUID | None, Query()] = None,
    preferred_lang: Annotated[str | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    filters: Annotated[
        str | None,
        Query(
            description=(
                "JSON-encoded AdvancedFilters tree from the filter-bar UI. "
                "Shape: {conditions: [...], groups: [{logic, conditions: [...]}]}. "
                "AND-combined with the per-field query params."
            ),
        ),
    ] = None,
    sort_by: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated sortable field keys (e.g. `status,created_at`), "
                "paired 1-to-1 with `order`. Omit both for the default ordering "
                "(created_at desc, id desc)."
            ),
        ),
    ] = None,
    order: Annotated[
        str | None,
        Query(description="Comma-separated directions matching `sort_by` (`asc`/`desc`)."),
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> CaseListResponse:
    # Multi-sort parse + validate (Prism): ValueError → 422 with the
    # standard error body, same path as the filter-tree errors.
    try:
        sorts = parse_sorts(sort_by, order)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    case_filters = CaseFilters(
        status=status,
        origin_country=origin_country,
        dest_country=dest_country,
        owner_agent_id=owner_agent_id,
        preferred_lang=preferred_lang,
        tag=tag,
        q=q,
        advanced=_parse_advanced_filters(filters),
    )
    return await CasesManager(db).list_cases(agent, case_filters, page, page_size, sorts=sorts)


@router.get("/{case_id}", response_model=CaseDetailResponse)
async def get_case(case_id: uuid.UUID, agent: AgentDep, db: DbDep) -> CaseDetailResponse:
    return await CasesManager(db).get_case_detail(agent, case_id)


@router.patch("/{case_id}", response_model=CaseResponse)
async def update_case(
    case_id: uuid.UUID, body: CaseUpdateRequest, agent: AgentDep, db: DbDep
) -> CaseResponse:
    case = await CasesManager(db).update_case(agent, case_id, body)
    return CaseResponse.model_validate(case)


@router.get("/{case_id}/export")
async def export_case(case_id: uuid.UUID, agent: AgentDep, db: DbDep) -> Response:
    pdf_bytes = await CasesManager(db).export_pdf(agent, case_id)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="case-{case_id}.pdf"'},
    )


# --- family members -----------------------------------------------------------------


@router.post("/{case_id}/family", response_model=FamilyMemberResponse, status_code=201)
async def add_family_member(
    case_id: uuid.UUID, body: FamilyMemberRequest, agent: AgentDep, db: DbDep
) -> FamilyMemberResponse:
    member = await CasesManager(db).add_family_member(agent, case_id, body)
    return FamilyMemberResponse.model_validate(member)


@router.patch("/{case_id}/family/{member_id}", response_model=FamilyMemberResponse)
async def update_family_member(
    case_id: uuid.UUID,
    member_id: uuid.UUID,
    body: FamilyMemberRequest,
    agent: AgentDep,
    db: DbDep,
) -> FamilyMemberResponse:
    member = await CasesManager(db).update_family_member(agent, case_id, member_id, body)
    return FamilyMemberResponse.model_validate(member)


@router.delete("/{case_id}/family/{member_id}", response_model=MessageResponse)
async def delete_family_member(
    case_id: uuid.UUID, member_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await CasesManager(db).delete_family_member(agent, case_id, member_id)
    return MessageResponse(detail="Family member removed.")


# --- external contacts -----------------------------------------------------------------


@router.post(
    "/{case_id}/external-contacts", response_model=ExternalContactResponse, status_code=201
)
async def add_external_contact(
    case_id: uuid.UUID, body: ExternalContactCreateRequest, agent: AgentDep, db: DbDep
) -> ExternalContactResponse:
    contact = await CasesManager(db).add_external_contact(agent, case_id, body)
    return ExternalContactResponse.model_validate(contact)


@router.patch("/{case_id}/external-contacts/{contact_id}", response_model=ExternalContactResponse)
async def update_external_contact(
    case_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: ExternalContactUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> ExternalContactResponse:
    contact = await CasesManager(db).update_external_contact(agent, case_id, contact_id, body)
    return ExternalContactResponse.model_validate(contact)


@router.delete("/{case_id}/external-contacts/{contact_id}", response_model=MessageResponse)
async def delete_external_contact(
    case_id: uuid.UUID, contact_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await CasesManager(db).delete_external_contact(agent, case_id, contact_id)
    return MessageResponse(detail="External contact removed.")


# --- notes ----------------------------------------------------------------------------------


@router.get("/{case_id}/notes", response_model=list[CaseNoteResponse])
async def list_notes(case_id: uuid.UUID, agent: AgentDep, db: DbDep) -> list[CaseNoteResponse]:
    notes = await CasesManager(db).list_notes(agent, case_id)
    return [CaseNoteResponse.model_validate(note) for note in notes]


@router.post("/{case_id}/notes", response_model=CaseNoteResponse, status_code=201)
async def create_note(
    case_id: uuid.UUID, body: CaseNoteCreateRequest, agent: AgentDep, db: DbDep
) -> CaseNoteResponse:
    note = await CasesManager(db).create_note(agent, case_id, body)
    return CaseNoteResponse.model_validate(note)


@router.patch("/{case_id}/notes/{note_id}", response_model=CaseNoteResponse)
async def update_note(
    case_id: uuid.UUID,
    note_id: uuid.UUID,
    body: CaseNoteUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> CaseNoteResponse:
    note = await CasesManager(db).update_note(agent, case_id, note_id, body)
    return CaseNoteResponse.model_validate(note)


@router.delete("/{case_id}/notes/{note_id}", response_model=MessageResponse)
async def delete_note(
    case_id: uuid.UUID, note_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await CasesManager(db).delete_note(agent, case_id, note_id)
    return MessageResponse(detail="Note removed.")
