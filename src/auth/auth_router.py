import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from src.auth.auth_manager import AuthManager
from src.auth.auth_schema import (
    ActivateRequest,
    ActivateResponse,
    AgentMeResponse,
    ExpatMeResponse,
    ForgotPasswordRequest,
    ImpersonatorInfo,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshRequest,
    ResetPasswordRequest,
    TokenPairResponse,
)
from src.core.dependencies import get_current_agent, get_current_expat, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.enforcement import effective_permissions

router = APIRouter(prefix="/auth", tags=["auth"])

# Audience contract of every route (seeded into protected_resource by
# baseline/seed). AGENT/EXPAT rows without a permission = any
# authenticated actor of that audience (identity endpoints).
BINDINGS = [
    RouteBinding("POST", "/auth/agent/login", Audience.PUBLIC),
    RouteBinding("POST", "/auth/agent/refresh", Audience.PUBLIC),
    RouteBinding("POST", "/auth/agent/logout", Audience.AGENT),
    RouteBinding("GET", "/auth/agent/me", Audience.AGENT),
    RouteBinding("POST", "/auth/agent/forgot-password", Audience.PUBLIC),
    RouteBinding("POST", "/auth/agent/reset-password", Audience.PUBLIC),
    RouteBinding("POST", "/auth/expat/activate", Audience.PUBLIC),
    RouteBinding("POST", "/auth/expat/login", Audience.PUBLIC),
    RouteBinding("POST", "/auth/expat/refresh", Audience.PUBLIC),
    RouteBinding("POST", "/auth/expat/logout", Audience.EXPAT),
    RouteBinding("GET", "/auth/expat/me", Audience.EXPAT),
    RouteBinding("POST", "/auth/expat/forgot-password", Audience.PUBLIC),
    RouteBinding("POST", "/auth/expat/reset-password", Audience.PUBLIC),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]


# --- Agent flow ----------------------------------------------------------------


@router.post("/agent/login", response_model=TokenPairResponse)
async def agent_login(body: LoginRequest, db: DbDep) -> TokenPairResponse:
    return await AuthManager(db).login_agent(body.email, body.password)


@router.post("/agent/refresh", response_model=TokenPairResponse)
async def agent_refresh(body: RefreshRequest, db: DbDep) -> TokenPairResponse:
    return await AuthManager(db).refresh(body.refresh_token, Audience.AGENT)


@router.post("/agent/logout", response_model=MessageResponse)
async def agent_logout(
    body: LogoutRequest,
    agent: Annotated[Agent, Depends(get_current_agent)],
    db: DbDep,
) -> MessageResponse:
    await AuthManager(db).logout(body.refresh_token, Audience.AGENT, agent.id)
    return MessageResponse(detail="Logged out.")


async def _impersonator_info(request: Request, db: AsyncSession) -> ImpersonatorInfo | None:
    """Resolved from the claim the enforcement dependency stashed in
    request.state — no token re-decode."""
    impersonator_id = getattr(request.state, "impersonator_id", None)
    if impersonator_id is None:
        return None
    impersonator = await db.get(Agent, uuid.UUID(str(impersonator_id)))
    if impersonator is None:
        return None
    return ImpersonatorInfo(
        agent_id=impersonator.id,
        first_name=impersonator.first_name,
        last_name=impersonator.last_name,
    )


@router.get("/agent/me", response_model=AgentMeResponse)
async def agent_me(
    request: Request,
    agent: Annotated[Agent, Depends(get_current_agent)],
    db: DbDep,
) -> AgentMeResponse:
    return AgentMeResponse(
        id=agent.id,
        first_name=agent.first_name,
        last_name=agent.last_name,
        email=agent.email,
        agency_id=agent.agency_id,
        roles=sorted(role.name for role in agent.roles),
        effective_permissions=sorted(effective_permissions(agent)),
        impersonator=await _impersonator_info(request, db),
    )


@router.post("/agent/forgot-password", response_model=MessageResponse)
async def agent_forgot_password(body: ForgotPasswordRequest, db: DbDep) -> MessageResponse:
    detail = await AuthManager(db).forgot_password(body.email, Audience.AGENT)
    return MessageResponse(detail=detail)


@router.post("/agent/reset-password", response_model=MessageResponse)
async def agent_reset_password(body: ResetPasswordRequest, db: DbDep) -> MessageResponse:
    await AuthManager(db).reset_password(body.token, body.password, Audience.AGENT)
    return MessageResponse(detail="Password updated.")


# --- Expat flow -----------------------------------------------------------------


@router.post("/expat/activate", response_model=ActivateResponse)
async def expat_activate(body: ActivateRequest, db: DbDep) -> ActivateResponse:
    return await AuthManager(db).activate_expat(body.token, body.password)


@router.post("/expat/login", response_model=TokenPairResponse)
async def expat_login(body: LoginRequest, db: DbDep) -> TokenPairResponse:
    return await AuthManager(db).login_expat(body.email, body.password)


@router.post("/expat/refresh", response_model=TokenPairResponse)
async def expat_refresh(body: RefreshRequest, db: DbDep) -> TokenPairResponse:
    return await AuthManager(db).refresh(body.refresh_token, Audience.EXPAT)


@router.post("/expat/logout", response_model=MessageResponse)
async def expat_logout(
    body: LogoutRequest,
    expat: Annotated[ExpatUser, Depends(get_current_expat)],
    db: DbDep,
) -> MessageResponse:
    await AuthManager(db).logout(body.refresh_token, Audience.EXPAT, expat.id)
    return MessageResponse(detail="Logged out.")


@router.get("/expat/me", response_model=ExpatMeResponse)
async def expat_me(
    request: Request,
    expat: Annotated[ExpatUser, Depends(get_current_expat)],
    db: DbDep,
) -> ExpatMeResponse:
    return ExpatMeResponse(
        id=expat.id,
        first_name=expat.first_name,
        last_name=expat.last_name,
        email=expat.email,
        preferred_lang=expat.preferred_lang,
        impersonator=await _impersonator_info(request, db),
    )


@router.post("/expat/forgot-password", response_model=MessageResponse)
async def expat_forgot_password(body: ForgotPasswordRequest, db: DbDep) -> MessageResponse:
    detail = await AuthManager(db).forgot_password(body.email, Audience.EXPAT)
    return MessageResponse(detail=detail)


@router.post("/expat/reset-password", response_model=MessageResponse)
async def expat_reset_password(body: ResetPasswordRequest, db: DbDep) -> MessageResponse:
    await AuthManager(db).reset_password(body.token, body.password, Audience.EXPAT)
    return MessageResponse(detail="Password updated.")
