"""Accès DB de la bibliothèque de modèles — requêtes pures."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.document_template import DocumentTemplate
from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from shared.models.step_requirement import StepRequirement
from src.core.enums import DocumentTemplateState


class DocumentTemplatesRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_active_for_agency(self, agency_id: uuid.UUID) -> list[DocumentTemplate]:
        """LA bibliothèque de l'agence — les brouillons n'y sont pas.

        Un brouillon est un modèle que le provider a exigé qu'on matérialise
        avant que l'agence n'ait rien posé (voir DocumentTemplateState) : il
        n'a jamais existé pour elle, il ne doit pas se voir. Le filtre vit ICI,
        dans la seule requête de liste : la page bibliothèque et le sélecteur
        d'étape passent tous deux par elle, donc ils héritent ensemble."""
        stmt = (
            select(DocumentTemplate)
            .where(
                DocumentTemplate.agency_id == agency_id,
                DocumentTemplate.state == DocumentTemplateState.ACTIVE.value,
            )
            .order_by(DocumentTemplate.created_at)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_for_agency(
        self, agency_id: uuid.UUID, template_id: uuid.UUID
    ) -> DocumentTemplate | None:
        stmt = select(DocumentTemplate).where(
            DocumentTemplate.id == template_id, DocumentTemplate.agency_id == agency_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def definition_references(self, template_id: uuid.UUID) -> list[dict[str, str]]:
        """Les exigences signables (définitions de parcours) qui pointent ce
        modèle — la liste nommée du 409 de suppression."""
        stmt = (
            select(
                JourneyTemplate.name.label("journey"),
                JourneyTemplateStep.name.label("step"),
                StepRequirement.reference,
            )
            .join(JourneyTemplateStep, JourneyTemplateStep.id == StepRequirement.step_id)
            .join(JourneyTemplate, JourneyTemplate.id == JourneyTemplateStep.template_id)
            .where(StepRequirement.document_template_id == template_id)
            .order_by(JourneyTemplate.name, JourneyTemplateStep.name)
        )
        rows = (await self.db.execute(stmt)).all()
        return [
            {"journey": journey, "step": step, "reference": reference}
            for journey, step, reference in rows
        ]

    async def pending_row_count(self, template_id: uuid.UUID) -> int:
        """Lignes matérialisées PENDANTES qui référencent le modèle — un
        dossier en vol attend encore sa signature, la suppression refuse."""
        stmt = select(func.count()).where(
            CaseStepRequirement.document_template_id == template_id,
            CaseStepRequirement.status == "pending",
        )
        return int((await self.db.execute(stmt)).scalar_one())
