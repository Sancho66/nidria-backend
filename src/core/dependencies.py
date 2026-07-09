from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload, sessionmaker

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core.database import get_db
from src.core.enums import Audience
from src.core.exceptions import UnauthorizedError
from src.core.security import decode_access_token, token_subject

__all__ = [
    "get_agent_token_payload",
    "get_current_agent",
    "get_current_expat",
    "get_db",
    "get_expat_token_payload",
    "get_sync_session_local",
]


def get_sync_session_local(request: Request) -> sessionmaker[Session]:
    """Sync session factory for scheduler-side code triggered from the
    API (manual job trigger). Set in the lifespan; tests override it to
    point at the testcontainer DB."""
    return request.app.state.sync_session_local  # type: ignore[no-any-return]


# Two separate bearer schemes — one per auth flow. `auto_error=False` so a
# missing token raises OUR UnauthorizedError (consistent error body) instead
# of FastAPI's default. The login endpoints themselves arrive in step 6;
# tokenUrl is OpenAPI metadata only.
agent_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/agent/login", auto_error=False)
expat_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/expat/login", auto_error=False)


def get_agent_token_payload(
    token: Annotated[str | None, Depends(agent_oauth2_scheme)],
) -> dict[str, Any]:
    """Validated claims of an AGENT access token (signature + type + audience).

    Step 4 builds `get_current_agent` (ORM load) on top of this.
    """
    if token is None:
        raise UnauthorizedError("Missing authentication token.")
    return decode_access_token(token, Audience.AGENT)


def get_expat_token_payload(
    token: Annotated[str | None, Depends(expat_oauth2_scheme)],
) -> dict[str, Any]:
    """Validated claims of an EXPAT access token (signature + type + audience).

    Step 4 builds `get_current_expat` (ORM load) on top of this.
    """
    if token is None:
        raise UnauthorizedError("Missing authentication token.")
    return decode_access_token(token, Audience.EXPAT)


async def get_current_agent(
    request: Request,
    payload: Annotated[dict[str, Any], Depends(get_agent_token_payload)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Agent:
    """ORM-loaded Agent, stacked on the validated AGENT token payload.

    The RBAC enforcement (global dependency) already resolved and
    stashed the actor in `request.state.actor` — reuse it instead of
    re-querying. The fallback load keeps this dependency usable
    standalone (unit tests, scripts) with the same eager chain
    (roles → permissions) so effective_permissions never lazy-loads.
    """
    actor = getattr(request.state, "actor", None)
    if isinstance(actor, Agent):
        return actor
    agent_id = token_subject(payload)
    stmt = (
        select(Agent)
        .where(Agent.id == agent_id)
        .options(selectinload(Agent.role).selectinload(Role.permissions))
    )
    agent = (await db.execute(stmt)).scalar_one_or_none()
    if agent is None:
        raise UnauthorizedError("Agent not found.")
    return agent


async def get_current_expat(
    request: Request,
    payload: Annotated[dict[str, Any], Depends(get_expat_token_payload)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ExpatUser:
    """ORM-loaded ExpatUser, stacked on the validated EXPAT token payload.

    Same `request.state.actor` reuse as `get_current_agent`. A
    non-activated account is rejected — it cannot hold a token in the
    normal flow — UNLESS the token is an impersonation one (`impersonator_id`,
    a signature-verified claim): an agent may "see as" a not-yet-activated
    principal. Mirrors the same exemption in enforcement._resolve_expat.
    """
    actor = getattr(request.state, "actor", None)
    if isinstance(actor, ExpatUser):
        return actor
    expat = await db.get(ExpatUser, token_subject(payload))
    if expat is None:
        raise UnauthorizedError("User not found.")
    if expat.activated_at is None and payload.get("impersonator_id") is None:
        raise UnauthorizedError("Account not activated.")
    return expat
