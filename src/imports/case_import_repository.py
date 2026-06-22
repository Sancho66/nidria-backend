"""Data access for the CRM case import (BLOC 2) — read-only lookups the
engine needs before/while creating cases. Case creation itself goes through
CasesManager.create_case (not re-implemented here)."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.journey import (
    JourneyTemplate,
    JourneyTemplateCaseField,
    JourneyTemplateField,
)


@dataclass(frozen=True)
class DeclaredField:
    """A field the parcours' Informations tab collects.

    family ∈ "base_field" | "case_field" | "custom_field"; reference is the
    case_person column / client_case column / custom_field key.
    """

    family: str
    reference: str
    required: bool


class CaseImportRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_agency_template(
        self, agency_id: uuid.UUID, template_id: uuid.UUID
    ) -> JourneyTemplate | None:
        """The template MUST be agency-owned (same scoping as apply_journey:
        library samples are unreachable for assignment)."""
        stmt = select(JourneyTemplate).where(
            JourneyTemplate.id == template_id,
            JourneyTemplate.agency_id == agency_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def declared_fields(self, template_id: uuid.UUID) -> list[DeclaredField]:
        """The Informations-tab fields of the template: person base/custom
        (journey_template_field) + case-level (journey_template_case_field).
        This is the closed set of valid non-identity mapping targets."""
        person_rows = (
            (
                await self.db.execute(
                    select(JourneyTemplateField).where(
                        JourneyTemplateField.template_id == template_id
                    )
                )
            )
            .scalars()
            .all()
        )
        case_rows = (
            (
                await self.db.execute(
                    select(JourneyTemplateCaseField).where(
                        JourneyTemplateCaseField.template_id == template_id
                    )
                )
            )
            .scalars()
            .all()
        )
        fields = [
            DeclaredField(
                family=row.kind, reference=row.reference, required=row.required_at_creation
            )
            for row in person_rows
        ]
        fields += [
            DeclaredField(
                family="case_field", reference=row.case_field, required=row.required_at_creation
            )
            for row in case_rows
        ]
        return fields
