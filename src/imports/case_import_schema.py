"""Request + report schemas for the CRM case import (BLOC 2).

The mapping is `{csv_column: target_token}` where a token is one of:
  - "email" | "first_name" | "last_name"          (mandatory identity)
  - "base_field:<reference>"                       (a case_person civil field)
  - "case_field:<reference>"                       (a client_case address field)
  - "custom_field:<key>"                           (an agency custom field)
Non-identity targets MUST be declared in the parcours' Informations tab
(validated before the import runs).
"""

import uuid

from pydantic import BaseModel, Field


class CaseImportRequest(BaseModel):
    """Exactly ONE mapping source is used, resolved in this order:
    `mapping` (inline, BLOC 2) → `mapping_id` (a saved mapping) →
    `crm_slug` (the saved mapping for this parcours + CRM). The inline
    field keeps the BLOC 2 signature working unchanged."""

    journey_template_id: uuid.UUID
    csv_text: str
    mapping: dict[str, str] | None = None
    mapping_id: uuid.UUID | None = None
    crm_slug: str | None = None


class ImportFieldError(BaseModel):
    """A non-blocking per-cell failure: the dossier is still created, this
    one field is left unset and reported."""

    row: int
    column: str
    target: str
    reason: str


class ImportCreated(BaseModel):
    row: int
    case_id: uuid.UUID
    # The principal's identity — so the report names the client, not "row N".
    first_name: str
    last_name: str
    field_errors: list[ImportFieldError] = Field(default_factory=list)


class ImportSkipped(BaseModel):
    row: int
    # "duplicate_in_agency" (already a client of THIS agency) |
    # "duplicate_in_file" (same email earlier in the CSV / race).
    # NEVER references another agency — cross-agency existence is not a skip.
    reason: str


class ImportRejected(BaseModel):
    row: int
    # "missing_email" | "invalid_email" | "missing_identity" |
    # "missing_required_fields" | "invalid_row".
    reason: str
    details: list[str] = Field(default_factory=list)


class ImportReport(BaseModel):
    total_rows: int
    created_count: int
    skipped_count: int
    rejected_count: int
    created: list[ImportCreated] = Field(default_factory=list)
    skipped: list[ImportSkipped] = Field(default_factory=list)
    rejected: list[ImportRejected] = Field(default_factory=list)


# --- Dry-run preview (validate + report, ZERO write) -------------------------------


class PreviewCell(BaseModel):
    """One mapped cell of a previewed row: the COERCED value the import would
    store (date normalized, country → ISO-2, …) or, when invalid, the reason."""

    column: str  # the CSV column
    target: str  # the mapping token (email | base_field:phone | …)
    value: str | None = None  # coerced value rendered; None when empty
    reason: str | None = None  # set when the cell is invalid


class PreviewColumn(BaseModel):
    """A previewed column, in mapping order (the table's column set)."""

    column: str
    target: str


class PreviewRow(BaseModel):
    """The PREDICTED outcome of one CSV row — nothing is created."""

    row: int
    # "create" | "create_with_errors" | "skipped" | "rejected"
    status: str
    # For skipped/rejected: the same codes as ImportSkipped/ImportRejected
    # (duplicate_in_agency | duplicate_in_file | missing_email | …). None on
    # a clean create.
    reason: str | None = None
    cells: list[PreviewCell] = Field(default_factory=list)


class ImportPreview(BaseModel):
    """The read-only dry-run: per-row predicted statuses + coerced values, with
    NO dossier created, NO email queued, NO transaction. Dedup follows the SAME
    agency-scoped rule as the real import — cross-agency existence is never
    revealed."""

    total_rows: int
    create_count: int
    create_with_errors_count: int
    skipped_count: int
    rejected_count: int
    columns: list[PreviewColumn] = Field(default_factory=list)
    rows: list[PreviewRow] = Field(default_factory=list)
