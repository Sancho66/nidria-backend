"""Manager for the import socle (BLOC 1).

Serves the embedded, read-only CRM referential. No DB: the catalogue is a
static asset loaded into memory at process start (`crm_catalog`), so this
manager takes no session — it shapes the catalogue into response schemas
and raises NotFound on an unknown slug.
"""

from src.core.exceptions import NotFoundError
from src.imports import crm_catalog
from src.imports.imports_schema import (
    CrmDetailResponse,
    CrmFieldOut,
    CrmListResponse,
    CrmSummary,
)


class ImportsManager:
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
