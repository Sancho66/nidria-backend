import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.auth.auth_schema import MessageResponse
from src.cases.cases_manager import CasesManager
from src.cases.cases_schema import (
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
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience, CaseStatus
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

router = APIRouter(prefix="/cases", tags=["cases"])

_VIEW = Permission.CASE_VIEW
_EDIT = Permission.CASE_EDIT

BINDINGS = [
    RouteBinding("POST", "/cases", Audience.AGENT, _EDIT),
    RouteBinding("GET", "/cases", Audience.AGENT, _VIEW),
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
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> CaseListResponse:
    filters = CaseFilters(
        status=status,
        origin_country=origin_country,
        dest_country=dest_country,
        owner_agent_id=owner_agent_id,
        preferred_lang=preferred_lang,
        tag=tag,
        q=q,
    )
    return await CasesManager(db).list_cases(agent, filters, page, page_size)


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
