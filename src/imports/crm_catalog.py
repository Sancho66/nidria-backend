"""Embedded CRM referential — the read-only catalogue of CSV export
headers per source CRM (BLOC 1, import socle).

A STATIC asset, loaded into memory once at import time (NOT a table): the
single source served by the GET endpoints. The embedded file is the
referential Alexandre exports (`schemas-export-crm`, 29 verified tools,
contact entity only — auth/base_url/mapping_notes already stripped). This
module projects it to the *allégé* shape the API serves: per CRM, only its
importable CSV columns ({csv header, type, format, dedup}).

Projection rules (deliberate, documented):
- only the `contact` entity matters for a "1 row = 1 principal" import;
  company/deal/activity entities are not in the embedded file.
- a field with an EMPTY `csv` header is dropped: it has no column in a CSV
  export, so it can never be a mapping source. A CRM whose contact export
  exposes no CSV header therefore lists zero headers — surfaced honestly
  (field_count = 0), never silently hidden.
"""

import json
from dataclasses import dataclass
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent / "data" / "crm_referentiel.json"

# A CRM with too few importable CSV headers is not worth offering: its
# contact export carries no usable mapping surface (API-only fields in the
# source). Below this threshold the CRM is hidden from the list AND 404s on
# detail — never half-served.
MIN_USABLE_FIELDS = 3


@dataclass(frozen=True)
class CrmField:
    """One importable CSV column of a source CRM's contact export."""

    csv: str  # the CSV column HEADER (the mapping source key)
    type: str  # source type hint: string / number / datetime / enum / boolean / array / object
    format: str  # extra format hint ("email", "ISO8601", "epoch_s", …) or "" when none
    dedup: bool  # the source CRM treats this column as a dedup key (id / email / external id)


@dataclass(frozen=True)
class Crm:
    slug: str  # URL-safe routing key derived from the name ("HubSpot CRM" → "hubspot-crm")
    name: str  # the display name (the referential's `id`)
    headers: tuple[CrmField, ...]


def _slugify(name: str) -> str:
    """Lowercase alphanumeric runs joined by single dashes. Stable for the
    fixed 29-CRM set; a collision would corrupt routing, so we assert
    uniqueness at load."""
    chars: list[str] = []
    prev_dash = False
    for char in name.lower():
        if char.isalnum():
            chars.append(char)
            prev_dash = False
        elif not prev_dash:
            chars.append("-")
            prev_dash = True
    return "".join(chars).strip("-")


def _load() -> dict[str, Crm]:
    raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    catalog: dict[str, Crm] = {}
    for entry in raw.get("crms", []):
        name = str(entry["id"])
        slug = _slugify(name)
        if slug in catalog:
            raise RuntimeError(f"CRM slug collision: {slug!r} ({name!r})")
        headers = tuple(
            CrmField(
                csv=str(field.get("csv", "")).strip(),
                type=str(field.get("type", "")),
                format=str(field.get("format", "")),
                dedup=bool(field.get("dedup", False)),
            )
            for field in entry.get("contact_fields", [])
            if str(field.get("csv", "")).strip() != ""
        )
        catalog[slug] = Crm(slug=slug, name=name, headers=headers)
    return catalog


# Loaded once, at process start — the in-memory referential.
_CATALOG: dict[str, Crm] = _load()


def _is_usable(crm: Crm) -> bool:
    return len(crm.headers) >= MIN_USABLE_FIELDS


def list_crms() -> list[Crm]:
    """Usable CRMs (>= MIN_USABLE_FIELDS headers), sorted by display name."""
    usable = [crm for crm in _CATALOG.values() if _is_usable(crm)]
    return sorted(usable, key=lambda crm: crm.name.lower())


def get_crm(slug: str) -> Crm | None:
    """One usable CRM by slug, or None when unknown OR below the threshold
    (the manager maps None → 404 — a hidden CRM is never half-served)."""
    crm = _CATALOG.get(slug)
    if crm is None or not _is_usable(crm):
        return None
    return crm
