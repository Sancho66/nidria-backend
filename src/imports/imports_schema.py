"""Pydantic in/out schemas for the import socle (BLOC 1).

Only the CRM referential reads are exposed over HTTP here; the CSV parser
and the per-cell validator are pure library functions (csv_reader,
cell_validation) consumed by the later blocs, not endpoints.
"""

from pydantic import BaseModel


class CrmFieldOut(BaseModel):
    """One importable CSV column of a source CRM (a mapping source)."""

    csv: str
    type: str
    format: str
    dedup: bool


class CrmSummary(BaseModel):
    slug: str
    name: str
    field_count: int  # number of importable CSV headers (may be 0)


class CrmListResponse(BaseModel):
    crms: list[CrmSummary]


class CrmDetailResponse(BaseModel):
    slug: str
    name: str
    headers: list[CrmFieldOut]
