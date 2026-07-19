import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import ProtectedResource, Role
from src.billing.billing_lock import blocking_reason
from src.core.database import get_db
from src.core.dependencies import agent_oauth2_scheme, expat_oauth2_scheme
from src.core.enums import Audience
from src.core.exceptions import ForbiddenError, UnauthorizedError
from src.core.rbac.consent_gate import (
    missing_for_agent,
    missing_for_expat,
    missing_for_external,
)
from src.core.rbac.integrity import INFRA_WHITELIST
from src.core.security import decode_access_token, token_subject

logger = logging.getLogger(__name__)

# The impersonation mask (claim `impersonator_id` present) follows one
# policy PER FACE, both enforced centrally here on the matched route
# template. Security boundary in code — same status as the infra
# whitelist, not product config. Boot check:
# integrity.assert_impersonation_denylist_declared (both constants).
#
# EXPAT face ("Voir comme le client"): STRICT READ-ONLY, a legal
# requirement (point 12). The rule is on the HTTP METHOD, not on a route
# list: any non-GET request is denied by default, so every FUTURE expat
# write endpoint is born locked without developer action. The ONLY
# exceptions live in IMPERSONATION_WRITE_ALLOWLIST.
#
# AGENT face (member debugging, superadmin agency switcher): read-write
# by design ("enter agency" administers the target agency), with
# targeted denials where acting under someone else's name poisons
# attribution: session lifecycle of the target, impersonation chaining,
# and identity/permission structure mutations.

# EXPAT-face write exceptions under impersonation. Keep this to what the
# mode cannot function without; justify every entry.
IMPERSONATION_WRITE_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # Logout: lets the client-space session flow terminate cleanly
        # instead of dying on a 403. Harmless under the mask: the endpoint
        # only revokes a PRESENTED refresh jti bound to its own subject,
        # and an impersonation session holds no refresh token at all
        # (short-lived access token only), so nothing of the client's
        # real session is reachable.
        ("POST", "/auth/expat/logout"),
    }
)

# AGENT-face routes an impersonated token may never reach, whatever the
# target's permissions.
IMPERSONATION_AGENT_DENIED: frozenset[tuple[str, str]] = frozenset(
    {
        # target session lifecycle (changing the TARGET's password under a
        # mask would seize the account — same family as logout)
        ("POST", "/auth/agent/logout"),
        ("POST", "/auth/agent/change-password"),
        ("POST", "/auth/agent/2fa/setup"),
        ("POST", "/auth/agent/2fa/enable"),
        ("POST", "/auth/agent/2fa/disable"),
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
        # consent (point 16): accepting a legal document binds the AGENCY
        # under the accepting agent's name; forged under a mask it would
        # poison the clickwrap trace (the expat face needs no entry: its
        # read-only rule already blocks every write).
        ("POST", "/consents/agent/accept"),
    }
)

# Routes reachable WITHOUT consent (point 16). Everything else on an
# authenticated face is refused until the actor has accepted the latest
# active version of each required document, so any FUTURE endpoint is
# born gated. PUBLIC routes (login, refresh, activate, forgot/reset
# password...) never reach the gate: they resolve no actor. What remains
# open per face: the identity pair (me, logout: know who you are, leave)
# and the consent flow itself (read the documents, accept them).
CONSENT_EXEMPT: frozenset[tuple[str, str]] = frozenset(
    {
        # agent face
        ("GET", "/auth/agent/me"),
        ("POST", "/auth/agent/logout"),
        ("GET", "/consents/agent/pending"),
        ("POST", "/consents/agent/accept"),
        # expat face
        ("GET", "/auth/expat/me"),
        ("POST", "/auth/expat/logout"),
        ("GET", "/consents/expat/pending"),
        ("POST", "/consents/expat/accept"),
        # external (provider) face — reachable BEFORE consent so the
        # provider can read and sign external_terms.
        ("GET", "/consents/external/pending"),
        ("POST", "/consents/external/accept"),
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
        # identity (login/refresh are PUBLIC, so ungated)
        ("GET", "/auth/agent/me"),
        ("POST", "/auth/agent/logout"),
        # provider consent: read + sign external_terms before the portal
        # opens (also in CONSENT_EXEMPT, so the consent gate lets them
        # through).
        ("GET", "/consents/external/pending"),
        ("POST", "/consents/external/accept"),
        # branding: the provider portal shows the agency logo + cover
        # (read-only)
        ("GET", "/agencies/me/logo"),
        ("GET", "/agencies/me/cover"),
        # provider portal (wave B) — each scoped by get_case_for_external
        ("GET", "/external/cases"),
        ("GET", "/external/cases/{case_id}"),
        ("GET", "/external/cases/{case_id}/documents"),
        ("GET", "/external/cases/{case_id}/documents/{document_id}/download"),
        ("POST", "/external/cases/{case_id}/requirements/{requirement_id}/document"),
        # GAP-B: the provider DELIVERS on a step of an assigned case
        # (scoped by get_case_for_external like every portal route).
        ("POST", "/external/cases/{case_id}/documents"),
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


# Agent-face writes that stay open on a BLOCKED agency (billing lock,
# 4th stage). The lock's rule is on the HTTP METHOD — every future agent
# write endpoint is born covered — and this allowlist is the ONLY way
# out; justify every entry.
BILLING_LOCK_WRITE_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # The payment path NEVER locks: it is the exit of the blockage.
        ("POST", "/billing/checkout"),
        ("POST", "/billing/subscription/cancel"),
        ("POST", "/billing/subscription/resume"),
        ("POST", "/billing/payment-method/update"),
        # Session lifecycle & account security never lock.
        ("POST", "/auth/agent/logout"),
        ("POST", "/auth/agent/change-password"),
        ("POST", "/auth/agent/2fa/setup"),
        ("POST", "/auth/agent/2fa/enable"),
        ("POST", "/auth/agent/2fa/disable"),
        # The legal gate stays passable (the consent gate runs first).
        ("POST", "/consents/agent/accept"),
    }
)


async def _enforce_billing_lock(db: AsyncSession, agent: Agent, method: str, path: str) -> None:
    """4th stage of the agent pipeline (after impersonation and consent):
    a BLOCKED agency is READ-ONLY. Same construction as the impersonation
    expat mask: the rule is on the METHOD, not on a route list, so every
    future write endpoint is born covered (fail-closed).

    Out of scope by decision: the superadmin (the human exit), external
    providers and the whole expat face (their démarches, not the agency's
    fault — and a commercial argument: deposits keep landing, read-only
    agents watch them pile up until payment)."""
    if method == "GET":  # HEAD is normalized upstream; OPTIONS never reaches deps
        return
    if (method, path) in BILLING_LOCK_WRITE_ALLOWLIST:
        return
    if agent.is_external:
        return
    role = agent.role
    if role.is_system and role.name == "superadmin":
        return
    agency = await db.get(Agency, agent.agency_id)
    assert agency is not None
    reason = blocking_reason(agency, now=datetime.now(UTC))
    if reason is not None:
        raise ForbiddenError(
            "The trial has ended or the subscription lapsed; "
            "the workspace is read-only until payment.",
            code="billing.subscription_required",
            params={"reason": reason},
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
    # Offboarded (deactivated_at posed): the row is re-read on EVERY
    # request, so a still-valid access token dies here immediately.
    if agent.deactivated_at is not None:
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
    # The activated_at gate protects LOGIN, not impersonation: a non-activated
    # expat may never hold a self-minted token, but an agent MAY "see as" a
    # not-yet-activated principal (its dossier space exists). `impersonator_id`
    # is a SIGNED claim (decode_access_token verified the HMAC), unforgeable
    # without the expat secret; the read-only mask still applies downstream.
    if expat.activated_at is None and payload.get("impersonator_id") is None:
        raise UnauthorizedError("Account not activated.")
    return expat, payload


def _enforce_impersonation(
    request: Request, payload: dict[str, object], audience: Audience, method: str, path: str
) -> None:
    """Apply the per-face impersonation policy (see the constants above).

    `method` is already HEAD→GET normalized by enforce(), so the expat
    read-only rule reduces to method == "GET"."""
    impersonator_id = payload.get("impersonator_id")
    request.state.impersonator_id = impersonator_id
    if impersonator_id is None:
        return
    if audience is Audience.EXPAT:
        if method == "GET" or (method, path) in IMPERSONATION_WRITE_ALLOWLIST:
            return
        # Blocked-attempt trace in the applicative log (the DB
        # impersonation_log stays an issuance journal).
        logger.warning(
            "impersonation write blocked: agent=%s expat=%s %s %s",
            impersonator_id,
            payload.get("sub"),
            method,
            path,
        )
        raise ForbiddenError(
            "Impersonation is read-only: write operations are not allowed.",
            code="impersonation.read_only",
        )
    if (method, path) in IMPERSONATION_AGENT_DENIED:
        raise ForbiddenError(
            "This action is not allowed under impersonation.",
            code="impersonation.denied",
        )


async def _enforce_consent(
    request: Request,
    db: AsyncSession,
    audience: Audience,
    actor: Agent | ExpatUser,
    method: str,
    path: str,
) -> None:
    """Blocking clickwrap (point 16): the actor must have accepted the
    latest ACTIVE version of each document required for their audience
    before anything outside CONSENT_EXEMPT opens up.

    Skipped under impersonation: the agent CONSULTS, they are not the
    client (and acceptance under the mask is impossible anyway: expat
    face read-only, agent accept route in the denylist)."""
    if (method, path) in CONSENT_EXEMPT:
        return
    if request.state.impersonator_id is not None:
        return
    missing: list[dict[str, object]]
    if audience is Audience.AGENT:
        assert isinstance(actor, Agent)
        if actor.is_external:
            # Provider face (audience is AGENT, is_external flag): gated
            # per agency, external_terms, distinct code prefix.
            missing = [
                {"type": doc.type, "version": doc.version, "agency_id": str(agency_id)}
                for agency_id, doc in await missing_for_external(db, actor)
            ]
            if missing:
                raise ForbiddenError(
                    "Consent to the current provider terms is required.",
                    code="external.consent.required",
                    params={"missing": missing},
                )
            return
        missing = [
            {"type": doc.type, "version": doc.version} for doc in await missing_for_agent(db, actor)
        ]
    else:
        missing = [
            {"type": doc.type, "version": doc.version, "agency_id": str(agency_id)}
            for agency_id, doc in await missing_for_expat(db, actor.id)
        ]
    if missing:
        raise ForbiddenError(
            "Consent to the current legal documents is required.",
            code="consent.required",
            params={"missing": missing},
        )


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
        _enforce_impersonation(request, payload, Audience.AGENT, method, path)
        await _enforce_consent(request, db, Audience.AGENT, agent, method, path)
        # 4th stage — billing lock: a blocked agency writes NOTHING
        # (403 billing.subscription_required), before the permission
        # check so the front gets ONE stable code whatever the role.
        await _enforce_billing_lock(db, agent, method, path)
        # NULL permission on an AGENT binding = any authenticated agent
        # (identity endpoints: /me, /logout) — symmetric with EXPAT.
        if binding.permission is not None and binding.permission.key not in effective_permissions(
            agent
        ):
            raise ForbiddenError("Missing permission.")
        request.state.actor = agent
        return

    expat, payload = await _resolve_expat(request, db)
    _enforce_impersonation(request, payload, Audience.EXPAT, method, path)
    await _enforce_consent(request, db, Audience.EXPAT, expat, method, path)
    request.state.actor = expat
