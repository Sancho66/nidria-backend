"""Expose only reviewed production GET handlers in the isolated agency sandbox."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from src.activity.activity_router import router as activity_router
from src.activity.activity_router import stats_router
from src.agencies.agencies_router import AgentDep, DbDep, get_my_agency
from src.agencies.agencies_router import router as agencies_router
from src.agencies.agencies_schema import AgencyResponse
from src.agencies.profile_sections_router import router as sections_router
from src.auth.auth_router import router as auth_router
from src.cases.cases_router import router as cases_router
from src.client_profiles.client_profiles_router import router as profiles_router
from src.comments.comments_router import agent_router as comments_router
from src.company_profiles.company_profiles_router import router as companies_router
from src.consents.consents_router import router as consents_router
from src.core.database import get_db
from src.core.dependencies import get_agent_token_payload
from src.core.rbac.enforcement import enforce
from src.costs.costs_router import router as costs_router
from src.custom_fields.custom_fields_router import router as fields_router
from src.custom_fields.custom_fields_router import universe_router
from src.dashboard.dashboard_router import router as dashboard_router
from src.demo_sandbox.agency_manager import AgencyDemoManager
from src.demo_sandbox.catalog import DemoRecord
from src.document_templates.document_templates_router import router as templates_router
from src.documents.documents_router import agent_router as documents_router
from src.email_campaigns.email_router import router as email_router
from src.email_campaigns.email_schema import EmailSenderOption, EmailSendersRead
from src.external.external_router import agency_router as providers_router
from src.journeys.import_manager import JourneyImportManager
from src.journeys.journeys_router import router as journeys_router
from src.journeys.journeys_schema import JourneyImportReport
from src.progress.progress_router import router as progress_router
from src.reminders.reminders_router import router as reminders_router
from src.roles.roles_router import router as roles_router
from src.signatures.signatures_router import router as signatures_router
from src.views.views_router import router as views_router

# No export, download, billing, impersonation, admin, external-service sync,
# webhook, password, upload or write handler can enter through this list.
AGENT_READ_ROUTES = frozenset(
    {
        "/auth/agent/me",
        "/consents/agent/pending",
        "/agencies/me/logo",
        "/agencies/me/cover",
        "/agencies/me/onboarding",
        "/agencies/me/ai-usage",
        "/agencies/me/members",
        "/agencies/me/roles",
        "/agencies/me/external-roles",
        "/agencies/me/external-members",
        "/agencies/me/external-contacts",
        "/agencies/me/invitations",
        "/agencies/me/profile-sections",
        "/agencies/me/custom-fields",
        "/agencies/me/field-universe",
        "/agencies/me/activity-stats",
        "/agencies/me/roles/{role_id}",
        "/agencies/me/signature-credits",
        "/agencies/me/signature-credits/entries",
        "/permissions",
        "/cases",
        "/cases/columns",
        "/cases/{case_id}",
        "/cases/{case_id}/notes",
        "/cases/{case_id}/activity",
        "/cases/{case_id}/documents",
        "/cases/{case_id}/documents/{document_id}/download",
        "/cases/{case_id}/steps",
        "/cases/{case_id}/costs",
        "/cases/{case_id}/external-assignments",
        "/cases/{case_id}/steps/{progress_id}/comments",
        "/currencies",
        "/client-profiles",
        "/client-profiles/{profile_id}",
        "/client-profiles/{profile_id}/completeness",
        "/client-profiles/{profile_id}/notes",
        "/client-profiles/{profile_id}/activity",
        "/company-profiles",
        "/company-profiles/{company_id}",
        "/views",
        "/views/default-all",
        "/dashboard",
        "/dashboard/me",
        "/dashboard/worklist",
        "/dashboard/activity",
        "/journeys",
        "/journeys/library",
        "/journeys/{template_id}",
        "/journeys/{template_id}/steps/{step_id}/attachments",
        "/journeys/{template_id}/steps/{step_id}/participants",
        "/journeys/{template_id}/steps/{step_id}/requirements",
        "/journeys/{template_id}/steps/{step_id}/case-requirements",
        "/journeys/{template_id}/fields",
        "/journeys/{template_id}/case-fields",
        "/journeys/{template_id}/sections",
        "/document-templates",
        "/document-templates/{template_id}",
        "/reminders",
        "/reminders/{reminder_id}",
        "/message-templates",
        "/email/meta",
        "/email/campaigns",
        "/email/campaigns/{campaign_id}",
        "/email/campaigns/{campaign_id}/steps",
        "/email/campaigns/{campaign_id}/performance",
        "/email/outreach",
        "/email/members",
        "/email/members/{member_id}",
        "/email/client-profiles/{client_profile_id}",
    }
)


async def agency_scope(
    request: Request,
    payload: Annotated[dict[str, Any], Depends(get_agent_token_payload)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    # A normal agent JWT, even issued with the sandbox secret, is not a demo
    # capability. Real production credentials are signed with another secret.
    manager = AgencyDemoManager(db)
    identity = await manager.verify_agent(payload)
    requested_case = request.path_params.get("case_id")
    if requested_case and any(str(case.id) == str(requested_case) for case in identity.cases):
        manager.load_documents(identity, uuid.UUID(str(requested_case)))


def agency_read_router() -> APIRouter:
    router = APIRouter(dependencies=[Depends(agency_scope), Depends(enforce)])
    for source in (
        activity_router,
        stats_router,
        agencies_router,
        sections_router,
        auth_router,
        cases_router,
        profiles_router,
        comments_router,
        companies_router,
        consents_router,
        costs_router,
        fields_router,
        universe_router,
        dashboard_router,
        templates_router,
        documents_router,
        email_router,
        providers_router,
        journeys_router,
        progress_router,
        reminders_router,
        roles_router,
        signatures_router,
        views_router,
    ):
        selected = APIRouter()
        selected.routes.extend(
            route
            for route in source.routes
            if isinstance(route, APIRoute)
            and route.methods == {"GET"}
            and route.path in AGENT_READ_ROUTES
        )
        router.include_router(selected)
    actual = {route.path for route in router.routes if isinstance(route, APIRoute)}
    if actual != AGENT_READ_ROUTES:
        raise RuntimeError(
            f"The reviewed agency contract has changed: {sorted(AGENT_READ_ROUTES - actual)}"
        )

    @router.get("/agencies/me", response_model=AgencyResponse)
    async def agency_identity(agent: AgentDep, db: DbDep) -> AgencyResponse:
        response = await get_my_agency(agent, db)
        # Management metadata contains the prospect email and opaque access code;
        # it belongs only in the authenticated platform operator's demo tool.
        response.settings = {
            key: value for key, value in response.settings.items() if not key.startswith("sandbox_")
        }
        return response

    @router.get("/demo/ai-preview", response_model=JourneyImportReport)
    async def recorded_preview(
        payload: Annotated[dict[str, Any], Depends(get_agent_token_payload)], db: DbDep
    ) -> JourneyImportReport:
        identity = await AgencyDemoManager(db).verify_agent(payload)
        record = DemoRecord.model_validate(identity.agency.settings["sandbox_dynamic_entry"])
        # The same deterministic production preview, from the six approved
        # sentences. No input, AI service, commit or creation on this route.
        return await JourneyImportManager(db).run(
            identity.agent, record.journey_import["parcours"], preview=True
        )

    @router.get("/email/senders", response_model=EmailSendersRead)
    async def fictional_sender(agent: AgentDep, db: DbDep) -> EmailSendersRead:
        agency = await get_my_agency(agent, db)
        # A local fixture, not even the provider adapter: this read can never
        # acquire credentials or make a network call to Lemlist.
        return EmailSendersRead(
            external=False,
            senders=[
                EmailSenderOption(
                    user_id="fictional-sender", email=agency.contact_email, campaigns=2
                )
            ],
        )

    # Separate production routers can register /cases/{case_id} before
    # /cases/columns. Preserve static route precedence in this merged router.
    router.routes.sort(key=lambda route: route.path.count("{"))  # type: ignore[attr-defined]
    return router
