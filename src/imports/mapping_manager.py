"""CRUD for saved CRM import mappings (BLOC 3).

Agency-scoped throughout. On write, the SAME target-membership check as the
import (validate_mapping_targets) is reused — a saved mapping can only target
fields declared in its parcours' Informations tab. csv_column keys are free
(the agency maps whatever its CSV exposes); only the TARGET is constrained.
"""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.custom_fields.custom_fields_manager import CustomFieldsManager
from src.imports import crm_catalog
from src.imports.case_import_repository import CaseImportRepository
from src.imports.mapping_repository import MappingRepository
from src.imports.mapping_schema import MappingListResponse, MappingResponse, MappingUpsertRequest
from src.imports.mapping_validation import validate_mapping_targets


class MappingManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = MappingRepository(db)
        self.import_repo = CaseImportRepository(db)

    async def list(
        self,
        agent: Agent,
        *,
        journey_template_id: uuid.UUID | None = None,
        crm_slug: str | None = None,
    ) -> MappingListResponse:
        rows = await self.repo.list(
            agent.agency_id, journey_template_id=journey_template_id, crm_slug=crm_slug
        )
        return MappingListResponse(mappings=[MappingResponse.model_validate(r) for r in rows])

    async def resolve(
        self, agent: Agent, journey_template_id: uuid.UUID, crm_slug: str
    ) -> MappingResponse:
        """The applicable mapping for (parcours, crm) — to pre-fill an import."""
        row = await self.repo.get_first_for_crm(agent.agency_id, journey_template_id, crm_slug)
        if row is None:
            raise NotFoundError(
                "No saved mapping for this parcours and CRM.",
                code="import.mapping_not_found_for_crm",
            )
        return MappingResponse.model_validate(row)

    async def upsert(self, agent: Agent, payload: MappingUpsertRequest) -> MappingResponse:
        # Parcours must be agency-owned (same scoping as assignment/import).
        template = await self.import_repo.get_agency_template(
            agent.agency_id, payload.journey_template_id
        )
        if template is None:
            raise NotFoundError("Journey template not found.", code="journey.template_not_found")
        # CRM identity: either the "custom" sentinel (Autre / CRM générique,
        # which needs a free label) OR a known referential slug. Custom skips
        # the referential check — its CSV headers come from the uploaded file.
        if payload.crm_slug == crm_catalog.CUSTOM_CRM_SLUG:
            custom_crm_name = (payload.custom_crm_name or "").strip()
            if not custom_crm_name:
                raise ValidationError(
                    "custom_crm_name is required for a custom CRM import.",
                    code="import.custom_crm_name_required",
                )
        elif crm_catalog.get_crm(payload.crm_slug) is None:
            raise ValidationError(
                f"Unknown CRM slug {payload.crm_slug!r}.",
                code="import.crm_unknown",
                params={"slug": payload.crm_slug},
            )
        else:
            custom_crm_name = None  # referenced CRM carries no free label
        # Targets must belong to this parcours (reused import check, 422 if not).
        declared = await self.import_repo.declared_fields(template.id)
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        defs_by_key = {d.key: d for d in definitions}
        validate_mapping_targets(payload.mapping, declared, defs_by_key)

        if payload.id is not None:
            # EDIT: update THIS config by id (agency-scoped).
            row = await self.repo.get(agent.agency_id, payload.id)
            if row is None:
                raise NotFoundError("Mapping not found.", code="import.mapping_not_found")
            if (
                row.journey_template_id != payload.journey_template_id
                or row.crm_slug != payload.crm_slug
            ):
                raise ValidationError(
                    "Mapping belongs to a different parcours or CRM.",
                    code="import.mapping_mismatch",
                )
            # A rename onto ANOTHER config's name is the only edit-time conflict.
            if payload.name != row.name:
                clash = await self.repo.get_by_name(
                    agent.agency_id, payload.journey_template_id, payload.crm_slug, payload.name
                )
                if clash is not None and clash.id != row.id:
                    raise ConflictError(
                        f"A mapping named {payload.name!r} already exists for this CRM.",
                        code="import.mapping_name_taken",
                        params={"name": payload.name},
                    )
            row.name = payload.name
            row.custom_crm_name = custom_crm_name
            row.mapping = payload.mapping
        else:
            # CREATE: a NEW named config. Pre-check the EXACT name conflict so
            # the 409 is accurate (and does not rely on the DB IntegrityError,
            # which an older/stricter constraint could also raise).
            clash = await self.repo.get_by_name(
                agent.agency_id, payload.journey_template_id, payload.crm_slug, payload.name
            )
            if clash is not None:
                raise ConflictError(
                    f"A mapping named {payload.name!r} already exists for this CRM.",
                    code="import.mapping_name_taken",
                    params={"name": payload.name},
                )
            row = self.repo.add(
                agency_id=agent.agency_id,
                journey_template_id=payload.journey_template_id,
                crm_slug=payload.crm_slug,
                custom_crm_name=custom_crm_name,
                name=payload.name,
                mapping=payload.mapping,
            )

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            # The accurate same-name conflict is pre-checked above. Reaching here
            # means a STRICTER/OLDER DB constraint blocked the write — almost
            # always a database that has NOT applied the `name`-in-key migration.
            raise ConflictError(
                "Could not save this mapping — a conflicting mapping already exists for "
                "this CRM. If you just enabled multiple configs per CRM, apply the latest "
                "database migration (alembic upgrade head).",
                code="import.mapping_conflict",
            ) from exc
        await self.db.refresh(row)
        return MappingResponse.model_validate(row)

    async def delete(self, agent: Agent, mapping_id: uuid.UUID) -> None:
        row = await self.repo.get(agent.agency_id, mapping_id)
        if row is None:
            raise NotFoundError("Mapping not found.", code="import.mapping_not_found")
        await self.repo.delete(row)
        await self.db.commit()
