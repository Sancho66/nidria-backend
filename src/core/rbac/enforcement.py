from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import ProtectedResource, Role
from src.core.database import get_db
from src.core.dependencies import agent_oauth2_scheme, expat_oauth2_scheme
from src.core.enums import Audience
from src.core.exceptions import ForbiddenError, UnauthorizedError
from src.core.rbac.integrity import INFRA_WHITELIST
from src.core.security import decode_access_token, token_subject


async def resolve_binding(db: AsyncSession, method: str, route: str) -> ProtectedResource | None:
    """One indexed SELECT per hit on the unique (method, route) — no
    in-memory cache, deliberately: the table is tiny, the lookup is
    sub-millisecond, and a cache would need an invalidation story once
    bindings become runtime-editable (post-MVP Settings)."""
    stmt = select(ProtectedResource).where(
        ProtectedResource.method == method, ProtectedResource.route == route
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def effective_permissions(agent: Agent) -> set[str]:
    """Union of permission keys across the agent's roles. Pure Python
    over the eager-loaded roles→permissions chain — zero queries."""
    return {perm.key for role in agent.roles for perm in role.permissions}


async def _resolve_agent(request: Request, db: AsyncSession) -> Agent:
    token = await agent_oauth2_scheme(request)
    if token is None:
        raise UnauthorizedError("Missing authentication token.")
    payload = decode_access_token(token, Audience.AGENT)
    agent_id = token_subject(payload)
    stmt = (
        select(Agent)
        .where(Agent.id == agent_id)
        .options(selectinload(Agent.roles).selectinload(Role.permissions))
    )
    agent = (await db.execute(stmt)).scalar_one_or_none()
    if agent is None:
        raise UnauthorizedError("Agent not found.")
    return agent


async def _resolve_expat(request: Request, db: AsyncSession) -> ExpatUser:
    token = await expat_oauth2_scheme(request)
    if token is None:
        raise UnauthorizedError("Missing authentication token.")
    payload = decode_access_token(token, Audience.EXPAT)
    expat = await db.get(ExpatUser, token_subject(payload))
    if expat is None:
        raise UnauthorizedError("User not found.")
    if expat.activated_at is None:
        raise UnauthorizedError("Account not activated.")
    return expat


async def enforce(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Global access enforcement — generic, names no permission.

    Runs as an app-level FastAPI dependency (NOT Starlette middleware:
    the matched route template is only in scope after routing). The
    per-request dependency cache makes `db` the SAME session as the
    endpoint's. The resolved actor lands in `request.state.actor`;
    `get_current_agent` / `get_current_expat` read it from there.

    Semantics: infra whitelist passes; unbound route → 403 (deny by
    default); PUBLIC passes with no actor; AGENT → valid agent token
    (401) + permission in matrix (403); EXPAT → valid expat token
    (401), no matrix — ownership is checked in Managers.
    """
    route = request.scope.get("route")
    path: str = getattr(route, "path", request.url.path)
    if path in INFRA_WHITELIST:
        request.state.actor = None
        return

    # Routes declaring HEAD explicitly share their GET binding (FastAPI
    # APIRoutes 405 HEAD otherwise, before routing). CORS preflight
    # OPTIONS never reaches dependencies.
    method = "GET" if request.method == "HEAD" else request.method

    binding = await resolve_binding(db, method, path)
    if binding is None:
        raise ForbiddenError(f"No access binding for {method} {path}.")

    if binding.audience == Audience.PUBLIC:
        request.state.actor = None
        return

    if binding.audience == Audience.AGENT:
        agent = await _resolve_agent(request, db)
        # NULL permission on an AGENT binding = any authenticated agent
        # (identity endpoints: /me, /logout) — symmetric with EXPAT.
        if binding.permission is not None and binding.permission.key not in effective_permissions(
            agent
        ):
            raise ForbiddenError("Missing permission.")
        request.state.actor = agent
        return

    expat = await _resolve_expat(request, db)
    request.state.actor = expat
