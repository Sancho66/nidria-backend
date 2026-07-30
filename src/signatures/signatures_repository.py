"""Accès DB pur du domaine signatures (pattern maison)."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.signature import SignatureRequest, SignatureSigner
from src.core.enums import SignatureRequestStatus

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
