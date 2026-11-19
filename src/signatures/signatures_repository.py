"""Accès DB pur du domaine signatures (pattern maison)."""

import uuid
from typing import Any

from sqlalchemy import Select, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.case_person import CasePerson
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.document_template import DocumentTemplate
from shared.models.signature import SignatureRequest, SignatureSigner
from src.core.enums import CasePersonKind, SignatureRequestStatus

# Une demande encore susceptible d'aboutir — les états où un nouveau send
# pour la même (étape, référence, personne) serait un doublon.
LIVE_STATUSES = (
    SignatureRequestStatus.DRAFT.value,
    SignatureRequestStatus.SENT.value,
    SignatureRequestStatus.PARTIALLY_SIGNED.value,
    SignatureRequestStatus.COMPLETED.value,
)


class SignaturesRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    @staticmethod
    def client_signatures(
        case_id: uuid.UUID, expat_id: uuid.UUID, viewing_person: CasePerson | None
    ) -> Select[tuple[SignatureSigner, SignatureRequest]]:
        """Les tâches du signataire connecté, communes à sa liste et à ses étapes."""
        person_filter = (
            CasePerson.id == viewing_person.id
            if viewing_person is not None
            else CasePerson.kind == CasePersonKind.PRINCIPAL.value
        )
        submitted_one_off = exists().where(
            CaseStepRequirement.id == SignatureSigner.case_step_requirement_id,
            CaseStepRequirement.document_template_id == DocumentTemplate.id,
            DocumentTemplate.case_id == case_id,
            DocumentTemplate.case_step_progress_id == CaseStepRequirement.case_step_progress_id,
            DocumentTemplate.person_id == SignatureSigner.case_person_id,
            DocumentTemplate.submitted_at.is_not(None),
        )
        return (
            select(SignatureSigner, SignatureRequest)
            .select_from(SignatureSigner)
            .join(SignatureRequest, SignatureRequest.id == SignatureSigner.signature_request_id)
            .join(CasePerson, CasePerson.id == SignatureSigner.case_person_id)
            .where(
                SignatureRequest.case_id == case_id,
                CasePerson.case_id == case_id,
                CasePerson.expat_user_id == expat_id,
                person_filter,
                SignatureRequest.status.in_(
                    (
                        SignatureRequestStatus.SENT,
                        SignatureRequestStatus.PARTIALLY_SIGNED,
                        SignatureRequestStatus.COMPLETED,
                    )
                ),
                or_(SignatureRequest.step_requirement_id.is_not(None), submitted_one_off),
            )
        )

    async def client_one_off_requirement_ids(
        self, case_id: uuid.UUID, expat_id: uuid.UUID, viewing_person: CasePerson | None
    ) -> frozenset[uuid.UUID]:
        stmt = (
            self.client_signatures(case_id, expat_id, viewing_person)
            .with_only_columns(SignatureSigner.case_step_requirement_id)
            .where(SignatureRequest.step_requirement_id.is_(None))
        )
        return frozenset(row_id for row_id in await self.db.scalars(stmt) if row_id is not None)

    def add_request(self, **kwargs: Any) -> SignatureRequest:
        request = SignatureRequest(**kwargs)
        self.db.add(request)
        return request

    def add_signer(self, **kwargs: Any) -> SignatureSigner:
        signer = SignatureSigner(**kwargs)
        self.db.add(signer)
        return signer

    async def list_requests_for_progress(
        self, case_step_progress_id: uuid.UUID
    ) -> list[SignatureRequest]:
        stmt = select(SignatureRequest).where(
            SignatureRequest.case_step_progress_id == case_step_progress_id
        )
        return list((await self.db.execute(stmt)).scalars())

    async def list_signers(self, request_id: uuid.UUID) -> list[SignatureSigner]:
        stmt = select(SignatureSigner).where(SignatureSigner.signature_request_id == request_id)
        return list((await self.db.execute(stmt)).scalars())

    async def live_signer_person_ids(
        self, case_step_progress_id: uuid.UUID, reference: str
    ) -> set[uuid.UUID]:
        """Les personnes déjà assises sur une demande VIVANTE de cette
        (étape, référence) — la garde anti-doublon des sends (activation
        rejouée, personne tardive re-matérialisée)."""
        stmt = (
            select(SignatureSigner.case_person_id)
            .join(SignatureRequest, SignatureRequest.id == SignatureSigner.signature_request_id)
            .where(
                SignatureRequest.case_step_progress_id == case_step_progress_id,
                SignatureRequest.reference == reference,
                SignatureRequest.status.in_(LIVE_STATUSES),
                # Les sièges AGENCE (contreseing) n'entrent pas dans la
                # garde anti-doublon des personnes.
                SignatureSigner.case_person_id.is_not(None),
            )
        )
        return {pid for pid in (await self.db.execute(stmt)).scalars() if pid is not None}

    async def get_request(self, request_id: uuid.UUID) -> SignatureRequest | None:
        return await self.db.get(SignatureRequest, request_id)

    async def get_request_by_provider_ref(self, provider_ref: str) -> SignatureRequest | None:
        stmt = select(SignatureRequest).where(SignatureRequest.provider_ref == provider_ref)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_signer(self, signer_id: uuid.UUID) -> SignatureSigner | None:
        return await self.db.get(SignatureSigner, signer_id)
