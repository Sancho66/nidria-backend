import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from src.core.dependencies import get_current_agent, get_current_expat, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.profile.profile_manager import ProfileManager
from src.profile.profile_schema import ProfileResponse, ProfileUpdateRequest

router = APIRouter(prefix="/profile", tags=["profile"])

# Own-profile routes carry no permission (identity level, like /me);
# the CLIENT-avatar read is gated case.view (it belongs to the dossier
# surface). Expat writes stay under the point-12 read-only mask when
# impersonated — deliberate, an avatar forged under a mask would be
# attribution poisoning.
BINDINGS = [
    RouteBinding("PATCH", "/profile/agent", Audience.AGENT),
    RouteBinding("POST", "/profile/agent/avatar", Audience.AGENT),
    RouteBinding("DELETE", "/profile/agent/avatar", Audience.AGENT),
    RouteBinding("GET", "/profile/agent/avatar/{agent_id}", Audience.AGENT),
    RouteBinding(
        "GET", "/profile/clients/{expat_user_id}/avatar", Audience.AGENT, Permission.CASE_VIEW
    ),
    RouteBinding("PATCH", "/profile/expat", Audience.EXPAT),
    RouteBinding("POST", "/profile/expat/avatar", Audience.EXPAT),
    RouteBinding("DELETE", "/profile/expat/avatar", Audience.EXPAT),
    RouteBinding("GET", "/profile/expat/avatar", Audience.EXPAT),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]
ExpatDep = Annotated[ExpatUser, Depends(get_current_expat)]


def _image_response(content: bytes) -> Response:
    # Private content served by us: cache locally, never shared caches.
    return Response(
        content=content,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=300"},
    )


# --- agent face ---------------------------------------------------------------------


@router.patch("/agent", response_model=ProfileResponse)
async def update_agent_profile(
    body: ProfileUpdateRequest, agent: AgentDep, db: DbDep
) -> ProfileResponse:
    return await ProfileManager(db).update_names(agent, body)


@router.post("/agent/avatar", response_model=ProfileResponse)
async def upload_agent_avatar(file: UploadFile, agent: AgentDep, db: DbDep) -> ProfileResponse:
    return await ProfileManager(db).upload_avatar(agent, file.content_type, await file.read())


@router.delete("/agent/avatar", response_model=ProfileResponse)
async def delete_agent_avatar(agent: AgentDep, db: DbDep) -> ProfileResponse:
    return await ProfileManager(db).delete_avatar(agent)


@router.get("/agent/avatar/{agent_id}")
async def get_agent_avatar(agent_id: uuid.UUID, agent: AgentDep, db: DbDep) -> Response:
    """Any member of the SAME agency (own avatar included)."""
    return _image_response(await ProfileManager(db).agent_avatar(agent, agent_id))


@router.get("/clients/{expat_user_id}/avatar")
async def get_client_avatar(expat_user_id: uuid.UUID, agent: AgentDep, db: DbDep) -> Response:
    """The client's avatar, visible like their name: agencies holding a
    live case only."""
    return _image_response(await ProfileManager(db).client_avatar(agent, expat_user_id))


# --- expat face ---------------------------------------------------------------------


@router.patch("/expat", response_model=ProfileResponse)
async def update_expat_profile(
    body: ProfileUpdateRequest, expat: ExpatDep, db: DbDep
) -> ProfileResponse:
    return await ProfileManager(db).update_names(expat, body)


@router.post("/expat/avatar", response_model=ProfileResponse)
async def upload_expat_avatar(file: UploadFile, expat: ExpatDep, db: DbDep) -> ProfileResponse:
    return await ProfileManager(db).upload_avatar(expat, file.content_type, await file.read())


@router.delete("/expat/avatar", response_model=ProfileResponse)
async def delete_expat_avatar(expat: ExpatDep, db: DbDep) -> ProfileResponse:
    return await ProfileManager(db).delete_avatar(expat)


@router.get("/expat/avatar")
async def get_own_expat_avatar(expat: ExpatDep, db: DbDep) -> Response:
    return _image_response(await ProfileManager(db).own_expat_avatar(expat))
