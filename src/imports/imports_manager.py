"""Manager for the import socle (BLOC 1).

Serves the embedded, read-only CRM referential (a static asset loaded in
memory at process start — `crm_catalog`), and the agency's UNIVERSE OF
TARGETS, which does need a session: an agency's declared fields and its
named company keys live in base.

Le `db` est donc OPTIONNEL — les deux lectures de référentiel n'en ont
pas besoin, `targets()` si.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from src.core.exceptions import NotFoundError, ValidationError
from src.core.i18n import DEFAULT_LANG, resolve_i18n
from src.imports import crm_catalog
from src.imports.imports_schema import (
    CrmDetailResponse,
    CrmFieldOut,
    CrmListResponse,
    CrmSummary,
    ImportTarget,
    ImportTargetSection,
    ImportTargetsResponse,
)

#: Les deux faces d'import de fiches. Le parcours parle une autre langue
#: (`custom_field:<clé>`) et a sa propre validation — il n'entre pas ici.
TARGET_ENTITIES = ("person", "company")


class ImportsManager:
    def __init__(self, db: AsyncSession | None = None) -> None:
        self._db = db

    @property
    def db(self) -> AsyncSession:
        if self._db is None:  # pragma: no cover - garde de programmation
            raise RuntimeError("ImportsManager needs a session for this read")
        return self._db

    def list_crms(self) -> CrmListResponse:
        return CrmListResponse(
            crms=[
                CrmSummary(slug=crm.slug, name=crm.name, field_count=len(crm.headers))
                for crm in crm_catalog.list_crms()
            ]
        )

    def get_crm(self, slug: str) -> CrmDetailResponse:
        crm = crm_catalog.get_crm(slug)
        if crm is None:
            raise NotFoundError(
                f"Unknown CRM {slug!r}.", code="import.crm_unknown", params={"slug": slug}
            )
        return CrmDetailResponse(
            slug=crm.slug,
            name=crm.name,
            headers=[
                CrmFieldOut(csv=field.csv, type=field.type, format=field.format, dedup=field.dedup)
                for field in crm.headers
            ],
        )

    async def agency_default_language(self, agency_id: uuid.UUID) -> str:
        stmt = select(Agency.default_language).where(Agency.id == agency_id)
        return (await self.db.execute(stmt)).scalar_one_or_none() or DEFAULT_LANG

    async def targets(self, agency_id: uuid.UUID, entity: str, lang: str) -> ImportTargetsResponse:
        """L'UNIVERS DES CIBLES d'une agence, prêt à afficher.

        Même source que la validation d'import et que l'enregistrement de
        config (`import_targets`) : le front qui se sert ici ne peut plus
        offrir une cible que l'import refuserait — la question ne se pose
        plus, il n'y a qu'un univers.

        Les libellés partent en DOUBLE : `label` résolu pour la langue de
        la requête (le chemin court), `label_i18n` complet (une bascule
        de langue ne redemande rien). La clé, elle, ne se traduit pas.
        """
        from src.imports.import_targets import company_target_catalog, person_target_catalog

        if entity not in TARGET_ENTITIES:
            raise ValidationError(
                f"Unknown import entity {entity!r}.",
                code="import.unknown_entity",
                params={"entity": entity, "allowed": list(TARGET_ENTITIES)},
            )
        agency_default = await self.agency_default_language(agency_id)
        catalog = person_target_catalog if entity == "person" else company_target_catalog
        sections, targets = await catalog(self.db, agency_id)

        def label_of(blob: dict[str, str], scalar: str) -> str:
            return resolve_i18n(blob, lang, agency_default, scalar) or scalar

        return ImportTargetsResponse(
            entity=entity,
            sections=[
                ImportTargetSection(
                    key=section.key,
                    label=label_of(section.label_i18n, section.key),
                    label_i18n=section.label_i18n,
                )
                for section in sections
            ],
            targets=[
                ImportTarget(
                    key=target.key,
                    label=label_of(target.label_i18n, target.label),
                    label_i18n=target.label_i18n,
                    field_type=target.field_type,
                    section=target.section,
                    required=target.required,
                    address_base=target.address_base,
                    address_subfield=target.address_subfield,
                    will_create=target.will_create,
                    agency_named=target.agency_named,
                )
                for target in targets
            ],
        )
