import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.expat_user import ExpatUser
from src.core.dependencies import get_current_expat, get_db
from src.core.enums import Audience
from src.core.http import file_download_response
from src.core.i18n import RequestLang
from src.core.rbac.baseline import RouteBinding
from src.expat.expat_manager import ExpatPortalManager
from src.expat.expat_schema import (
    ExpatCaseDetailResponse,
    ExpatCaseSummaryResponse,
    ExpatNotificationResponse,
    RequirementValueRequest,
)

router = APIRouter(prefix="/expat", tags=["expat-portal"])

BINDINGS = [
    RouteBinding("GET", "/expat/cases", Audience.EXPAT),
    # Branding: the logo of an agency holding one of MY cases (scoped in
    # the manager, like every expat read).
    RouteBinding("GET", "/expat/agencies/{agency_id}/logo", Audience.EXPAT),
    RouteBinding("GET", "/expat/agencies/{agency_id}/cover", Audience.EXPAT),
    RouteBinding("GET", "/expat/cases/{case_id}", Audience.EXPAT),
    RouteBinding("GET", "/expat/cases/{case_id}/notifications", Audience.EXPAT),
    RouteBinding("PUT", "/expat/cases/{case_id}/requirements/{requirement_id}", Audience.EXPAT),
    RouteBinding(
        "POST",
        "/expat/cases/{case_id}/requirements/{requirement_id}/document",
        Audience.EXPAT,
    ),
    # Case-level requirement fulfillment (sections chantier, vague C2) —
    # bounded write to a client_case column (the declared case_field).
    RouteBinding(
        "PUT",
        "/expat/cases/{case_id}/case-requirements/{case_requirement_id}",
        Audience.EXPAT,
    ),
    # Feature 2: download an agency step attachment (always, on own dossier).
    RouteBinding(
        "GET",
        "/expat/cases/{case_id}/steps/{progress_id}/attachments/{attachment_id}/download",
        Audience.EXPAT,
    ),
    # "Action validée par" = client: the principal validates a step.
    RouteBinding(
        "POST",
        "/expat/cases/{case_id}/steps/{progress_id}/validate",
        Audience.EXPAT,
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
ExpatDep = Annotated[ExpatUser, Depends(get_current_expat)]


@router.get("/agencies/{agency_id}/logo")
async def expat_agency_logo(agency_id: uuid.UUID, expat: ExpatDep, db: DbDep) -> Response:
    content, media_type = await ExpatPortalManager(db).agency_logo(expat, agency_id)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/agencies/{agency_id}/cover")
async def expat_agency_cover(agency_id: uuid.UUID, expat: ExpatDep, db: DbDep) -> Response:
    content, media_type = await ExpatPortalManager(db).agency_cover(expat, agency_id)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/cases", response_model=list[ExpatCaseSummaryResponse])
async def list_my_cases(expat: ExpatDep, db: DbDep) -> list[ExpatCaseSummaryResponse]:
    return await ExpatPortalManager(db).list_my_cases(expat)


@router.get("/cases/{case_id}", response_model=ExpatCaseDetailResponse)
async def get_my_case(
    case_id: uuid.UUID, expat: ExpatDep, db: DbDep, lang: RequestLang
) -> ExpatCaseDetailResponse:
    return await ExpatPortalManager(db).get_my_case(expat, case_id, lang)


@router.put(
    "/cases/{case_id}/requirements/{requirement_id}", response_model=ExpatCaseDetailResponse
)
async def fulfill_requirement_value(
    case_id: uuid.UUID,
    requirement_id: uuid.UUID,
    body: RequirementValueRequest,
    expat: ExpatDep,
    db: DbDep,
) -> ExpatCaseDetailResponse:
    return await ExpatPortalManager(db).fulfill_value(expat, case_id, requirement_id, body)


@router.put(
    "/cases/{case_id}/case-requirements/{case_requirement_id}",
    response_model=ExpatCaseDetailResponse,
)
async def fulfill_case_requirement_value(
    case_id: uuid.UUID,
    case_requirement_id: uuid.UUID,
    body: RequirementValueRequest,
    expat: ExpatDep,
    db: DbDep,
) -> ExpatCaseDetailResponse:
    return await ExpatPortalManager(db).fulfill_case_value(
        expat, case_id, case_requirement_id, body
    )


@router.post(
    "/cases/{case_id}/requirements/{requirement_id}/document",
    response_model=ExpatCaseDetailResponse,
)
async def fulfill_requirement_document(
    case_id: uuid.UUID,
    requirement_id: uuid.UUID,
    expat: ExpatDep,
    db: DbDep,
    file: Annotated[UploadFile, File()],
) -> ExpatCaseDetailResponse:
    return await ExpatPortalManager(db).fulfill_document(expat, case_id, requirement_id, file)


@router.post(
    "/cases/{case_id}/steps/{progress_id}/validate", response_model=ExpatCaseDetailResponse
)
async def validate_step(
    case_id: uuid.UUID, progress_id: uuid.UUID, expat: ExpatDep, db: DbDep
) -> ExpatCaseDetailResponse:
    return await ExpatPortalManager(db).validate_step(expat, case_id, progress_id)


@router.get("/cases/{case_id}/steps/{progress_id}/attachments/{attachment_id}/download")
async def download_step_attachment(
    case_id: uuid.UUID,
    progress_id: uuid.UUID,
    attachment_id: uuid.UUID,
    expat: ExpatDep,
    db: DbDep,
) -> Response:
    filename, content = await ExpatPortalManager(db).download_step_attachment(
        expat, case_id, progress_id, attachment_id
    )
    return file_download_response(filename, content)


@router.get("/cases/{case_id}/notifications", response_model=list[ExpatNotificationResponse])
async def list_notifications(
    case_id: uuid.UUID, expat: ExpatDep, db: DbDep
) -> list[ExpatNotificationResponse]:
    return await ExpatPortalManager(db).list_notifications(expat, case_id)
