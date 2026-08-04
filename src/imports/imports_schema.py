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


# --- l'univers des cibles servi (GET /imports/targets) ------------------------


class ImportTargetSection(BaseModel):
    """Une section de la taxonomie fiche — l'en-tête d'un groupe du menu."""

    key: str
    #: Le libellé résolu pour la langue de la requête.
    label: str
    #: Les 7 langues du produit, pour un front qui bascule sans refetch.
    label_i18n: dict[str, str]


class ImportTarget(BaseModel):
    """UNE cible d'import — exactement ce que le combobox doit afficher."""

    #: Le jeton envoyé dans le mapping (`residence_address.street`, `tags`…).
    key: str
    label: str
    label_i18n: dict[str, str]
    #: text | select | number | date | boolean | country | address | tags
    field_type: str
    section: str
    #: L'import refuse de partir sans elle (le trio person, la dénomination).
    required: bool
    #: Sous-champ d'adresse : la base qu'il compose, et lequel des 4.
    address_base: str | None
    address_subfield: str | None
    #: Preset du catalogue non déclaré : l'import créera la définition.
    will_create: bool
    #: Clé baptisée par l'agence — son libellé n'a que sa langue d'origine.
    agency_named: bool


class ImportTargetsResponse(BaseModel):
    """L'UNIVERS COMPLET des cibles d'une agence, pour une entité.

    Servi depuis la MÊME source que la validation d'import et que
    l'enregistrement de config (`import_targets`) : ce qui est offert ici
    est accepté là-bas, sans recopie possible entre les deux.
    """

    entity: str
    sections: list[ImportTargetSection]
    targets: list[ImportTarget]
