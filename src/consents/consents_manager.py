import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.consent import ConsentDocument
from shared.models.expat_user import ExpatUser
from src.consents.consents_repository import ConsentsRepository
from src.consents.consents_schema import (
    ConsentAcceptRequest,
    ConsentAcceptResponse,
    ExpatAgencyPendingResponse,
    PendingDocumentResponse,
)
from src.core.enums import (
    AGENT_CONSENT_TYPES,
    EXPAT_CONSENT_TYPES,
    EXTERNAL_CONSENT_TYPES,
    ActorType,
)
from src.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from src.core.rbac.consent_gate import (
    active_documents_by_type,
    expat_agency_ids,
    external_agency_ids,
    is_agency_admin,
    missing_for_agent,
    missing_for_expat,
    missing_for_external,
)


def _resolve(doc: ConsentDocument, agency_name: str) -> PendingDocumentResponse:
    """{agency_name} resolved at READ time; the hash stays that of the
    RAW text (what the acceptance copies)."""
    return PendingDocumentResponse(
        type=doc.type,
        version=doc.version,
        content=doc.content_md.replace("{agency_name}", agency_name),
        content_hash=doc.content_hash,
    )


class ConsentsManager:
    """Pending reads reuse the EXACT gate computation (consent_gate), so
    the blocking 403 and the /pending screen can never disagree."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ConsentsRepository(db)

    # --- pending ---------------------------------------------------------------------

    async def pending_for_agent(self, agent: Agent) -> list[PendingDocumentResponse]:
        missing = await missing_for_agent(self.db, agent)
        if not missing:
            return []
        names = await self.repo.agency_names([agent.agency_id])
        agency_name = names.get(agent.agency_id, "")
        return [_resolve(doc, agency_name) for doc in missing]

    async def pending_for_expat(self, expat: ExpatUser) -> list[ExpatAgencyPendingResponse]:
        pairs = await missing_for_expat(self.db, expat.id)
        if not pairs:
            return []
        names = await self.repo.agency_names(sorted({agency_id for agency_id, _ in pairs}))
        grouped: dict[uuid.UUID, list[ConsentDocument]] = {}
        for agency_id, doc in pairs:
            grouped.setdefault(agency_id, []).append(doc)
        return [
            ExpatAgencyPendingResponse(
                agency_id=agency_id,
                agency_name=names.get(agency_id, ""),
                documents=[_resolve(doc, names.get(agency_id, "")) for doc in docs],
            )
            for agency_id, docs in grouped.items()
        ]

    async def pending_for_external(self, external_agent: Agent) -> list[ExpatAgencyPendingResponse]:
        pairs = await missing_for_external(self.db, external_agent)
        if not pairs:
            return []
        names = await self.repo.agency_names(sorted({agency_id for agency_id, _ in pairs}))
        grouped: dict[uuid.UUID, list[ConsentDocument]] = {}
        for agency_id, doc in pairs:
            grouped.setdefault(agency_id, []).append(doc)
        return [
            ExpatAgencyPendingResponse(
                agency_id=agency_id,
                agency_name=names.get(agency_id, ""),
                documents=[_resolve(doc, names.get(agency_id, "")) for doc in docs],
            )
            for agency_id, docs in grouped.items()
        ]

    # --- accept ----------------------------------------------------------------------

    async def accept_as_agent(
        self, agent: Agent, payload: ConsentAcceptRequest, ip: str | None
    ) -> ConsentAcceptResponse:
        if payload.document_type.value not in AGENT_CONSENT_TYPES:
            raise ValidationError(
                "This document is not an agency document.",
                code="consent.wrong_audience",
                params={"type": payload.document_type.value},
            )
        # The acceptance binds the AGENCY: only its admin may sign.
        if not await is_agency_admin(self.db, agent):
            raise ForbiddenError(
                "Only the agency administrator can accept the agency documents.",
                code="consent.admin_only",
            )
        return await self._accept(
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            payload=payload,
            agency_id=agent.agency_id,
            ip=ip,
        )

    async def accept_as_expat(
        self, expat: ExpatUser, payload: ConsentAcceptRequest, ip: str | None
    ) -> ConsentAcceptResponse:
        if payload.document_type.value not in EXPAT_CONSENT_TYPES:
            raise ValidationError(
                "This document is not a client document.",
                code="consent.wrong_audience",
                params={"type": payload.document_type.value},
            )
        if payload.agency_id is None:
            raise ValidationError(
                "agency_id is required: a client acceptance binds one agency.",
                code="consent.agency_required",
            )
        # Scope: only an agency holding a live case of this client (404,
        # never revealing anything about other agencies).
        if payload.agency_id not in await expat_agency_ids(self.db, expat.id):
            raise NotFoundError("No active case with this agency.", code="consent.agency_not_found")
        return await self._accept(
            actor_type=ActorType.EXPAT,
            actor_id=expat.id,
            payload=payload,
            agency_id=payload.agency_id,
            ip=ip,
        )

    async def accept_as_external(
        self, external_agent: Agent, payload: ConsentAcceptRequest, ip: str | None
    ) -> ConsentAcceptResponse:
        if not external_agent.is_external:
            raise ForbiddenError(
                "This endpoint is reserved for external providers.",
                code="consent.wrong_audience",
            )
        if payload.document_type.value not in EXTERNAL_CONSENT_TYPES:
            raise ValidationError(
                "This document is not a provider document.",
                code="consent.wrong_audience",
                params={"type": payload.document_type.value},
            )
        # A provider acceptance binds ONE agency; default to the provider's
        # own agency, and if a body agency_id is given it must match.
        agencies = external_agency_ids(external_agent)
        agency_id = payload.agency_id or agencies[0]
        if agency_id not in agencies:
            raise NotFoundError(
                "No provider access with this agency.", code="consent.agency_not_found"
            )
        return await self._accept(
            actor_type=ActorType.EXTERNAL,
            actor_id=external_agent.id,
            payload=payload,
            agency_id=agency_id,
            ip=ip,
        )

    async def _accept(
        self,
        actor_type: ActorType,
        actor_id: uuid.UUID,
        payload: ConsentAcceptRequest,
        agency_id: uuid.UUID,
        ip: str | None,
    ) -> ConsentAcceptResponse:
        doc_type = payload.document_type.value
        active = (await active_documents_by_type(self.db, frozenset({doc_type}))).get(doc_type)
        if active is None:
            raise NotFoundError(
                "No active document of this type.",
                code="consent.document_not_found",
                params={"type": doc_type},
            )
        if payload.document_version != active.version:
            raise ConflictError(
                "The accepted version is not the active version of this document.",
                code="consent.version_stale",
                params={
                    "type": doc_type,
                    "requested_version": payload.document_version,
                    "active_version": active.version,
                },
            )
        existing = await self.repo.get_acceptance(
            actor_type.value, actor_id, doc_type, active.version, agency_id
        )
        if existing is not None:
            # Idempotent no-op: the original trace stays untouched.
            return ConsentAcceptResponse(
                document_type=doc_type,
                document_version=active.version,
                accepted_at=existing.accepted_at,
                already_accepted=True,
            )
        row = self.repo.add_acceptance(
            actor_type=actor_type.value,
            actor_id=actor_id,
            document_type=doc_type,
            document_version=active.version,
            content_hash=active.content_hash,
            accepted_at=datetime.now(UTC),  # server clock, never the client's
            ip=ip,
            agency_id=agency_id,
        )
        await self.db.commit()
        return ConsentAcceptResponse(
            document_type=doc_type,
            document_version=active.version,
            accepted_at=row.accepted_at,
            already_accepted=False,
        )
