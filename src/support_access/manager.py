"""Support consultations require the agency's own administrator to consent."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.support_access import SupportAccessGrant
from src.core.exceptions import ForbiddenError, NotFoundError
from src.core.rbac.consent_gate import is_agency_admin
from src.support_access.repository import SupportAccessRepository
from src.support_access.schema import (
    SupportAccessResponse,
    SupportCaseContentResponse,
    SupportCaseResponse,
    SupportCasesResponse,
    SupportDocumentResponse,
    SupportEventResponse,
    SupportGrantCreate,
    SupportGrantResponse,
    SupportOperatorResponse,
    SupportPersonResponse,
    SupportStepResponse,
    SupportTextResponse,
)


class SupportAccessManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = SupportAccessRepository(db)

    async def require_admin(self, agent: Agent, impersonated: bool) -> None:
        from src.core.rbac.delegation import is_platform_role

        if (
            impersonated
            or agent.is_external
            or is_platform_role(agent.role)
            or not await is_agency_admin(self.db, agent)
        ):
            raise ForbiddenError(
                "Only the agency's own administrator can authorize support.",
                code="support.agency_admin_required",
            )

    async def operators(self, agent: Agent, impersonated: bool) -> list[SupportOperatorResponse]:
        await self.require_admin(agent, impersonated)
        return [
            SupportOperatorResponse(id=a.id, name=f"{a.first_name} {a.last_name}".strip())
            for a in await self.repo.operators()
        ]

    async def _case(self, agent: Agent, case_id: uuid.UUID) -> None:
        if await self.repo.case(agent.agency_id, case_id) is None:
            raise NotFoundError("Case not found.", code="case.not_found")

    async def grant(
        self, agent: Agent, case_id: uuid.UUID, body: SupportGrantCreate, impersonated: bool
    ) -> SupportGrantResponse:
        await self.require_admin(agent, impersonated)
        await self._case(agent, case_id)
        operators = {a.id: a for a in await self.repo.operators()}
        if body.operator_id not in operators:
            raise NotFoundError("Support operator not found.", code="support.operator_not_found")
        now = datetime.now(UTC)
        grant = SupportAccessGrant(
            agency_id=agent.agency_id,
            case_id=case_id,
            operator_id=body.operator_id,
            granted_by_id=agent.id,
            created_at=now,
            expires_at=now + timedelta(minutes=body.duration_minutes),
        )
        self.db.add(grant)
        await self.db.flush()
        self.repo.record(grant, agent.id, "granted")
        await self.db.commit()
        operator = operators[body.operator_id]
        return self._grant_response(
            grant, f"{operator.first_name} {operator.last_name}".strip(), now
        )

    @staticmethod
    def _grant_response(
        grant: SupportAccessGrant, name: str, now: datetime
    ) -> SupportGrantResponse:
        return SupportGrantResponse(
            id=grant.id,
            operator_id=grant.operator_id,
            operator_name=name,
            created_at=grant.created_at,
            expires_at=grant.expires_at,
            revoked_at=grant.revoked_at,
            active=grant.revoked_at is None and grant.expires_at > now,
        )

    async def revoke(
        self, agent: Agent, case_id: uuid.UUID, grant_id: uuid.UUID, impersonated: bool
    ) -> None:
        await self.require_admin(agent, impersonated)
        await self._case(agent, case_id)
        grant = await self.db.get(SupportAccessGrant, grant_id)
        if grant is None or grant.case_id != case_id or grant.agency_id != agent.agency_id:
            raise NotFoundError("Support authorization not found.", code="support.grant_not_found")
        if grant.revoked_at is None:
            grant.revoked_at = datetime.now(UTC)
            grant.revoked_by_id = agent.id
            self.repo.record(grant, agent.id, "revoked")
            await self.db.commit()

    async def journal(
        self, agent: Agent, case_id: uuid.UUID, impersonated: bool, page: int, page_size: int
    ) -> SupportAccessResponse:
        await self.require_admin(agent, impersonated)
        await self._case(agent, case_id)
        grants = await self.repo.grants(agent.agency_id, case_id)
        events, total = await self.repo.events(agent.agency_id, case_id, page, page_size)
        ids = {g.operator_id for g in grants} | {e.actor_id for e in events if e.actor_id}
        names = await self.repo.actor_names(ids)
        now = datetime.now(UTC)
        return SupportAccessResponse(
            grants=[self._grant_response(g, names.get(g.operator_id, ""), now) for g in grants],
            events=[
                SupportEventResponse(
                    id=e.id,
                    actor_name=names.get(e.actor_id, "") if e.actor_id else "",
                    action=e.action,
                    resource_id=e.resource_id,
                    created_at=e.created_at,
                )
                for e in events
            ],
            events_total=total,
            page=page,
            page_size=page_size,
        )

    async def overview(
        self, agent: Agent, operator_id: uuid.UUID | None, page: int, page_size: int
    ) -> SupportCasesResponse:
        if operator_id is None:
            raise ForbiddenError(
                "This view is reserved for restricted support sessions.", code="support.restricted"
            )
        cases, total = await self.repo.case_page(agent.agency_id, page, page_size)
        ids = [c.id for c in cases]
        progress = await self.repo.progress(ids)
        by_case: dict[uuid.UUID, list[SupportStepResponse]] = {key: [] for key in ids}
        for row in progress:
            by_case[row.case_id].append(
                SupportStepResponse(id=row.id, position=row.position, status=row.status)
            )
        grants = await self.repo.active_grants(agent.agency_id, ids, operator_id, datetime.now(UTC))
        return SupportCasesResponse(
            items=[
                SupportCaseResponse(
                    id=c.id,
                    status=c.status,
                    steps=sorted(by_case[c.id], key=lambda s: s.position),
                    access_expires_at=grants.get(c.id),
                )
                for c in cases
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def consultation(
        self,
        agent: Agent,
        operator_id: uuid.UUID | None,
        case_id: uuid.UUID,
        action: str,
        resource_id: uuid.UUID | None = None,
    ) -> SupportAccessGrant:
        if operator_id is None:
            raise ForbiddenError(
                "This view is reserved for restricted support sessions.", code="support.restricted"
            )
        await self._case(agent, case_id)
        grant = await self.repo.active_grant(
            agent.agency_id, case_id, operator_id, datetime.now(UTC)
        )
        if grant is None:
            raise ForbiddenError(
                "The agency must authorize access to this case. "
                "The authorization may have expired or been revoked.",
                code="support.authorization_required",
            )
        # Persist the content-free audit BEFORE releasing any content. A failed
        # audit write fails the consultation closed. No customer data in events.
        self.repo.record(grant, operator_id, action, resource_id)
        await self.db.commit()
        return grant

    async def content(
        self, agent: Agent, operator_id: uuid.UUID | None, case_id: uuid.UUID
    ) -> SupportCaseContentResponse:
        grant = await self.consultation(agent, operator_id, case_id, "case_read")
        people = await self.repo.people(case_id)
        documents = await self.repo.documents(case_id)
        notes = await self.repo.notes(case_id)
        messages = await self.repo.messages(case_id)
        return SupportCaseContentResponse(
            case_id=case_id,
            access_expires_at=grant.expires_at,
            people=[
                SupportPersonResponse(
                    name=f"{e.first_name} {e.last_name}".strip() if e else p.full_name or "",
                    email=e.email if e else None,
                    phone=p.phone,
                )
                for p, e in people[:500]
            ],
            documents=[
                SupportDocumentResponse(
                    id=d.id, filename=d.filename, validation_status=d.validation_status
                )
                for d in documents[:500]
            ],
            notes=[
                SupportTextResponse(id=n.id, body=n.body, created_at=n.created_at)
                for n in notes[:500]
            ],
            messages=[
                SupportTextResponse(
                    id=m.id, body=m.body, step_id=m.case_step_progress_id, created_at=m.created_at
                )
                for m in messages[:500]
            ],
            truncated=any(len(rows) > 500 for rows in (people, documents, notes, messages)),
        )

    async def download(
        self,
        agent: Agent,
        operator_id: uuid.UUID | None,
        case_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> tuple[str, bytes]:
        await self.consultation(agent, operator_id, case_id, "document_read", document_id)
        from src.documents.documents_manager import DocumentsManager

        document, content = await DocumentsManager(self.db).download_for_agent(
            agent, case_id, document_id
        )
        return document.filename, content
