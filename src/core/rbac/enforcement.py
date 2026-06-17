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

# Routes an IMPERSONATED token (claim `impersonator_id` present) may
# never reach, whatever the target's permissions. Security boundary in
# code — same status as the infra whitelist, not product config. Three
# families: session lifecycle of the target, impersonation chaining,
# and structure mutations (administering identities/permissions under
# someone else's name poisons attribution), plus expat-side document
# writes (uploaded_by=EXPAT means "the client provided this" in the
# validation flow — the agent has their own correctly-attributed path).
# Boot check: integrity.assert_impersonation_denied_routes_declared.
IMPERSONATION_DENIED: frozenset[tuple[str, str]] = frozenset(
    {
        # target session lifecycle
        ("POST", "/auth/agent/logout"),
        ("POST", "/auth/expat/logout"),
        # chaining
        ("POST", "/agencies/me/members/{agent_id}/impersonate"),
        ("POST", "/expat-users/{expat_user_id}/impersonate"),
        # structure mutations
        ("PATCH", "/agencies/me"),
        ("POST", "/agencies/me/invitations"),
        ("DELETE", "/agencies/me/invitations/{invitation_id}"),
        ("POST", "/agencies/me/roles"),
        ("PATCH", "/agencies/me/roles/{role_id}"),
        ("PUT", "/agencies/me/roles/{role_id}/permissions"),
        ("DELETE", "/agencies/me/roles/{role_id}"),
        ("POST", "/agencies/me/roles/{role_id}/duplicate"),
        ("PUT", "/agencies/me/members/{agent_id}/role"),
        # expat portal is read-only under impersonation
        ("POST", "/expat/cases/{case_id}/documents"),
        ("DELETE", "/expat/cases/{case_id}/documents/{document_id}"),
    }
)


# Routes an EXTERNAL agent (provider) may reach. Everything else (the
# internal /cases/* surface AND the permissionless "any agent" routes
# that would leak staff/journeys/roles) is denied at enforce(), BEFORE
# the permission check and regardless of bindings: fail-closed by
# construction. Wave A: identity only. Wave B: + the dedicated /external
# portal — and EACH of those routes is additionally scoped by assignment
# inside its manager (get_case_for_external), so the allowlist gates
# WHICH routes, the scoping gates WHICH case. A test asserts every
# declared /external route is listed here (no portal route slips in
# unscoped). The internal /cases/* routes are deliberately NOT here.
EXTERNAL_AGENT_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # identity
        ("GET", "/auth/agent/me"),
        ("POST", "/auth/agent/logout"),
        # provider portal (wave B) — each scoped by get_case_for_external
        ("GET", "/external/cases"),
        ("GET", "/external/cases/{case_id}"),
        ("GET", "/external/cases/{case_id}/documents"),
        ("GET", "/external/cases/{case_id}/documents/{document_id}/download"),
        ("POST", "/external/cases/{case_id}/requirements/{requirement_id}/document"),
        (
            "GET",
            "/external/cases/{case_id}/steps/{progress_id}/attachments/{attachment_id}/download",
        ),
        ("POST", "/external/cases/{case_id}/steps/{progress_id}/validate"),
        ("GET", "/external/cases/{case_id}/steps/{progress_id}/comments"),
        ("POST", "/external/cases/{case_id}/steps/{progress_id}/comments"),
        ("PATCH", "/external/cases/{case_id}/steps/{progress_id}/comments/{comment_id}"),
        ("DELETE", "/external/cases/{case_id}/steps/{progress_id}/comments/{comment_id}"),
    }
)


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
    """Permission keys of the agent's SINGLE role (Prism model — no
    union). Pure Python over the eager-loaded role→permissions chain —
    zero queries."""
    return {perm.key for perm in agent.role.permissions}


async def _resolve_agent(request: Request, db: AsyncSession) -> tuple[Agent, dict[str, object]]:
    token = await agent_oauth2_scheme(request)
    if token is None:
        raise UnauthorizedError("Missing authentication token.")
    payload = decode_access_token(token, Audience.AGENT)
    agent_id = token_subject(payload)
    stmt = (
        select(Agent)
        .where(Agent.id == agent_id)
        .options(selectinload(Agent.role).selectinload(Role.permissions))
    )
    agent = (await db.execute(stmt)).scalar_one_or_none()
    if agent is None:
        raise UnauthorizedError("Agent not found.")
    return agent, payload


async def _resolve_expat(request: Request, db: AsyncSession) -> tuple[ExpatUser, dict[str, object]]:
    token = await expat_oauth2_scheme(request)
    if token is None:
        raise UnauthorizedError("Missing authentication token.")
    payload = decode_access_token(token, Audience.EXPAT)
    expat = await db.get(ExpatUser, token_subject(payload))
    if expat is None:
        raise UnauthorizedError("User not found.")
    if expat.activated_at is None:
        raise UnauthorizedError("Account not activated.")
    return expat, payload


def _deny_if_impersonated(
    request: Request, payload: dict[str, object], method: str, path: str
) -> None:
    impersonator_id = payload.get("impersonator_id")
    request.state.impersonator_id = impersonator_id
    if impersonator_id is not None and (method, path) in IMPERSONATION_DENIED:
        raise ForbiddenError("This action is not allowed under impersonation.")


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
        agent, payload = await _resolve_agent(request, db)
        # FAIL-CLOSED for external providers (wave A): denied on every
        # route outside their identity allowlist, BEFORE the permission
        # check. Closes the permissionless "any agent" leaks (members,
        # journeys, roles, …) that zero permissions alone would not.
        if agent.is_external and (method, path) not in EXTERNAL_AGENT_ALLOWLIST:
            raise ForbiddenError("External providers have no access to this resource yet.")
        _deny_if_impersonated(request, payload, method, path)
        # NULL permission on an AGENT binding = any authenticated agent
        # (identity endpoints: /me, /logout) — symmetric with EXPAT.
        if binding.permission is not None and binding.permission.key not in effective_permissions(
            agent
        ):
            raise ForbiddenError("Missing permission.")
        request.state.actor = agent
        return

    expat, payload = await _resolve_expat(request, db)
    _deny_if_impersonated(request, payload, method, path)
    request.state.actor = expat
