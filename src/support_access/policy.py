"""Fail-closed support boundary, independent of the impersonated role.

Platform operators never inherit client-data access by switching agency.
Only reviewed metadata routes and the dedicated, audited consultation can
pass. Old impersonation tokens are covered by resolving their original actor.
An operator's original session keeps normal agency access, scoped to the
operator's own agency by the existing managers, just like other staff.
"""

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from src.core.exceptions import ForbiddenError, UnauthorizedError

SUPPORT_METADATA: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/auth/agent/me"),
        ("GET", "/agencies/me"),
        ("GET", "/agencies/me/logo"),
        ("GET", "/agencies/me/cover"),
        ("GET", "/agencies/me/members"),
        ("GET", "/agencies/me/roles"),
        ("GET", "/agencies/me/roles/{role_id}"),
        ("GET", "/agencies/me/external-roles"),
        ("GET", "/agencies/me/external-members"),
        ("GET", "/agencies/me/invitations"),
        ("GET", "/agencies/me/ai-usage"),
        ("GET", "/agencies/me/onboarding"),
        ("GET", "/agencies/me/referrals"),
        ("GET", "/agencies/me/signature-credits"),
        ("GET", "/billing/subscription"),
        ("GET", "/journeys"),
        ("GET", "/journeys/library"),
        ("GET", "/journeys/{template_id}"),
        ("GET", "/journeys/{template_id}/steps/{step_id}/requirements"),
        ("GET", "/journeys/{template_id}/steps/{step_id}/case-requirements"),
        ("GET", "/journeys/{template_id}/steps/{step_id}/participants"),
        ("GET", "/journeys/{template_id}/steps/{step_id}/attachments"),
        ("GET", "/journeys/{template_id}/steps/{step_id}/attachments/{attachment_id}/download"),
        ("GET", "/journeys/{template_id}/fields"),
        ("GET", "/journeys/{template_id}/case-fields"),
        ("GET", "/journeys/{template_id}/sections"),
        ("GET", "/message-templates"),
        ("GET", "/document-templates"),
        ("GET", "/document-templates/{template_id}"),
        ("GET", "/agencies/me/custom-fields"),
        ("GET", "/agencies/me/field-universe"),
        ("GET", "/agencies/me/profile-sections"),
        ("GET", "/support/cases"),
        ("GET", "/support/cases/{case_id}/content"),
        ("GET", "/support/cases/{case_id}/documents/{document_id}/download"),
        ("GET", "/consents/agent/pending"),
    }
)

PLATFORM_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/auth/agent/login"),
        ("POST", "/auth/agent/refresh"),
        ("POST", "/auth/agent/change-password"),
        ("GET", "/auth/agent/2fa"),
        ("POST", "/auth/agent/2fa/setup"),
        ("POST", "/auth/agent/2fa/enable"),
        ("POST", "/auth/agent/2fa/verify"),
        ("POST", "/auth/agent/2fa/disable"),
        ("POST", "/auth/agent/logout"),
        ("GET", "/auth/agent/me"),
        ("POST", "/auth/agent/forgot-password"),
        ("POST", "/auth/agent/reset-password"),
        ("GET", "/admin/agencies"),
        ("GET", "/admin/tasks"),
        ("POST", "/admin/tasks"),
        ("GET", "/admin/tasks/summary"),
        ("PATCH", "/admin/tasks/{task_id}"),
        ("DELETE", "/admin/tasks/{task_id}"),
        ("POST", "/admin/tasks/{task_id}/complete"),
        ("POST", "/admin/tasks/{task_id}/reopen"),
        ("GET", "/admin/operators"),
        ("GET", "/admin/tasks/{task_id}/calendar-link"),
        ("POST", "/admin/tasks/{task_id}/attachments"),
        ("GET", "/admin/tasks/{task_id}/attachments"),
        ("GET", "/admin/tasks/{task_id}/attachments/{attachment_id}/download"),
        ("DELETE", "/admin/tasks/{task_id}/attachments/{attachment_id}"),
        ("GET", "/admin/tasks/{task_id}/comments"),
        ("POST", "/admin/tasks/{task_id}/comments"),
        ("POST", "/admin/tasks/{task_id}/comments/{comment_id}/attachments"),
        ("DELETE", "/admin/tasks/{task_id}/comments/{comment_id}"),
        ("GET", "/admin/email-usage"),
        ("GET", "/admin/demos"),
        ("POST", "/admin/demos"),
        ("GET", "/admin/demos/adoption"),
        ("GET", "/admin/demos/stats"),
        ("GET", "/admin/demos/imports"),
        ("POST", "/admin/demos/imports"),
        ("GET", "/admin/demos/imports/batch-export"),
        ("GET", "/admin/demos/imports/{job_id}"),
        ("PATCH", "/admin/demos/imports/{job_id}"),
        ("GET", "/admin/demos/imports/{job_id}/export"),
        ("POST", "/admin/demos/export"),
        ("POST", "/admin/demos/bulk"),
        ("GET", "/admin/demos/archive"),
        ("GET", "/admin/demos/templates"),
        ("POST", "/admin/demos/agency-batch"),
        ("POST", "/admin/demos/preview"),
        ("POST", "/admin/demos/{code}/disable"),
        ("GET", "/agencies"),
        ("POST", "/agencies"),
        ("DELETE", "/agencies/{agency_id}"),
        ("PATCH", "/agencies/{agency_id}/subscription"),
        ("PATCH", "/agencies/{agency_id}/trial"),
        ("PATCH", "/agencies/{agency_id}/lifetime-access"),
        ("POST", "/agencies/{agency_id}/signature-credits/grant"),
        ("GET", "/agencies/{agency_id}/email-module"),
        ("PATCH", "/agencies/{agency_id}/email-module"),
        ("POST", "/agencies/{agency_id}/email-module/pause-all"),
        ("PUT", "/agencies/{agency_id}/email-module/team"),
        ("DELETE", "/agencies/{agency_id}/email-module/team"),
        ("GET", "/agencies/{agency_id}/email-module/roles"),
        ("PUT", "/agencies/{agency_id}/email-module/roles/{role_id}"),
        ("GET", "/admin/email/provider"),
        ("POST", "/admin/email/provider/webhook"),
        # Job responses expose only reviewed aggregate counters. Historical
        # logs, provider payloads and exception strings are redacted by schema.
        ("GET", "/jobs"),
        ("PATCH", "/jobs/{job_id}"),
        ("POST", "/jobs/{job_id}/pause"),
        ("POST", "/jobs/{job_id}/resume"),
        ("POST", "/jobs/{job_id}/trigger"),
        ("GET", "/jobs/{job_id}/runs"),
        ("GET", "/jobs/{job_id}/runs/{run_id}"),
        ("POST", "/agencies/{agency_id}/enter"),
    }
)


async def support_operator(
    db: AsyncSession, agent: Agent | None, payload: dict[str, Any]
) -> uuid.UUID | None:
    from src.core.rbac.delegation import is_platform_role

    original = payload.get("impersonator_id")
    if original is None:
        return None
    try:
        original_id = uuid.UUID(str(original))
    except ValueError as exc:
        raise UnauthorizedError("Invalid support session.", code="auth.session_expired") from exc
    operator = await db.scalar(
        select(Agent).where(Agent.id == original_id).options(selectinload(Agent.role))
    )
    if operator is None or operator.deactivated_at is not None:
        raise UnauthorizedError("Support session expired.", code="auth.session_expired")
    if payload.get("support_operator_id") is not None or is_platform_role(operator.role):
        if not is_platform_role(operator.role):
            raise UnauthorizedError("Support session expired.", code="auth.session_expired")
        # An ordinary colleague preview in the operator's own agency does
        # not enter a customer's workspace. Check live membership, never
        # a caller-supplied agency id or the target role alone.
        if (
            agent is not None
            and agent.agency_id == operator.agency_id
            and payload.get("impersonation_scope") == "agency"
        ):
            return None
        return operator.id
    # Legacy masks have no immutable origin scope. Do not let a demoted
    # former platform operator regain agency powers via an old token.
    if payload.get("impersonation_scope") != "agency" or (
        agent is not None and operator.agency_id != agent.agency_id
    ):
        raise UnauthorizedError("Please reopen the preview session.", code="auth.session_expired")
    return None


async def enforce_support(
    request: Request,
    db: AsyncSession,
    agent: Agent | None,
    payload: dict[str, Any],
    method: str,
    path: str,
) -> None:
    from src.core.rbac.delegation import is_platform_role

    operator_id = await support_operator(db, agent, payload)
    request.state.support_operator_id = operator_id
    if operator_id is None:
        if agent is not None and is_platform_role(agent.role):
            # Platform-wide surfaces still require the reviewed allowlist:
            # help search logs can contain customer data from other agencies.
            # Client previews also remain closed because an expat identity
            # may expose cases belonging to several agencies.
            platform_surface = (
                path == "/agencies"
                or path == "/jobs"
                or path.startswith(("/admin/", "/jobs/", "/agencies/{agency_id}"))
                or path == "/expat-users/{expat_user_id}/impersonate"
            )
            if platform_surface and (method, path) not in PLATFORM_OPERATIONS:
                raise ForbiddenError(
                    "Client data is private. Use agency-authorized support access for one case.",
                    code="support.restricted",
                )
        return
    if agent is not None and (method, path) in SUPPORT_METADATA:
        return
    # A support mask never inherits platform administration or the target's
    # case, export, signature, profile, import or storage capabilities.
    raise ForbiddenError(
        "Client data is private. Use agency-authorized support access for one case.",
        code="support.restricted",
    )
