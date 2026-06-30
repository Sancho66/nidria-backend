"""CRM case import engine (BLOC 2).

Takes (parcours, mapping, CSV) and creates N principal dossiers — ONE row =
ONE dossier, each in its OWN transaction (a failing row never rolls back the
others). It does NOT re-implement creation: it composes CasesManager.create_case
and the BLOC 1 per-cell validation, and defers invitation emails to the caller.

Frozen rules (see the bloc brief): email is the pivot (missing → reject;
already a client of THIS agency → skip; cross-agency existence is NEVER
revealed); invalid non-required cells are non-blocking (dossier created, field
reported); required parcours fields missing/invalid → row rejected; mapping
targets must be declared in the parcours' Informations tab (validated upfront).
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import date, datetime

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.custom_field import CustomFieldDefinition
from src.cases.cases_manager import CasesManager
from src.cases.cases_repository import CasesRepository
from src.cases.cases_schema import CaseCreateRequest
from src.core.email import PendingEmail
from src.core.exceptions import NidriaError, NotFoundError, ValidationError
from src.custom_fields.custom_fields_manager import CustomFieldsManager
from src.imports.case_import_repository import CaseImportRepository, DeclaredField
from src.imports.case_import_schema import (
    CaseImportRequest,
    ImportCreated,
    ImportFieldError,
    ImportPreview,
    ImportRejected,
    ImportReport,
    ImportSkipped,
    PreviewCell,
    PreviewColumn,
    PreviewRow,
)
from src.imports.cell_validation import (
    BaseFieldTarget,
    CaseFieldTarget,
    CellTarget,
    CustomFieldTarget,
    validate_cell,
)
from src.imports.csv_reader import ParsedCsv, parse_upload
from src.imports.mapping_repository import MappingRepository
from src.imports.mapping_validation import (
    IDENTITY_TARGETS,
    MappingTarget,
    validate_mapping_targets,
)


@dataclass
class _CellValidation:
    """Outcome of validating a row's non-identity cells — shared by the create
    path and the dry-run preview."""

    field_values: dict[str, object]  # base/case reference → coerced value
    custom: dict[str, object]  # custom_field key → coerced value
    field_errors: list[ImportFieldError]
    provided: set[tuple[str, str]]
    cells: list[PreviewCell]  # one per non-identity column (for the preview table)


class CaseImportManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.cases = CasesManager(db)
        self.cases_repo = CasesRepository(db)
        self.repo = CaseImportRepository(db)
        self.mappings = MappingRepository(db)

    @staticmethod
    def _parse_upload(request: CaseImportRequest) -> ParsedCsv:
        """Resolve the request's content (base64 file OR legacy csv_text) and
        route it through the unified reader → {headers, rows}. Same output for
        CSV and XLSX, so the rest of the engine is unchanged."""
        if request.file_b64 is not None:
            try:
                content: bytes | str = base64.b64decode(request.file_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValidationError("File payload is not valid base64.") from exc
            return parse_upload(request.filename, content)
        if request.csv_text is not None:
            return parse_upload(request.filename, request.csv_text)
        raise ValidationError("No file content provided (csv_text or file_b64).")

    async def run_import(
        self, agent: Agent, request: CaseImportRequest
    ) -> tuple[ImportReport, list[PendingEmail]]:
        parsed_csv = self._parse_upload(request)

        template = await self.repo.get_agency_template(agent.agency_id, request.journey_template_id)
        if template is None:
            raise NotFoundError("Journey template not found.")
        declared = await self.repo.declared_fields(template.id)
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        defs_by_key = {d.key: d for d in definitions}

        # The effective mapping is inline (BLOC 2) OR resolved from a saved
        # mapping (BLOC 3, by id or by (parcours, crm)) — agency-scoped.
        mapping = await self._resolve_mapping(agent, request)

        # Pre-flight: refuse the WHOLE import on a structural mapping problem
        # (unknown/duplicate target, unmapped identity, missing CSV column)
        # BEFORE creating anything.
        targets = self._validate_mapping(mapping, parsed_csv.headers, declared, defs_by_key)
        required = [(d.family, d.reference) for d in declared if d.required]
        email_col = self._column_for(targets, "email")
        first_col = self._column_for(targets, "first_name")
        last_col = self._column_for(targets, "last_name")

        report = ImportReport(
            total_rows=len(parsed_csv.rows),
            created_count=0,
            skipped_count=0,
            rejected_count=0,
        )
        pending: list[PendingEmail] = []
        seen_emails: set[str] = set()

        for index, row in enumerate(parsed_csv.rows, start=1):
            await self._process_row(
                agent=agent,
                request=request,
                mapping=mapping,
                row=row,
                index=index,
                targets=targets,
                required=required,
                defs_by_key=defs_by_key,
                email_col=email_col,
                first_col=first_col,
                last_col=last_col,
                seen_emails=seen_emails,
                report=report,
                pending=pending,
            )

        report.created_count = len(report.created)
        report.skipped_count = len(report.skipped)
        report.rejected_count = len(report.rejected)
        return report, pending

    # --- dry-run preview (validate + report, ZERO write) ----------------------------

    async def preview_import(self, agent: Agent, request: CaseImportRequest) -> ImportPreview:
        """Validate (parcours + mapping + CSV) and report per-row WITHOUT
        creating a dossier, queuing an email, or opening a write transaction.
        Reuses the EXACT parse / cell-validation / mapping resolution of the
        real import; only the "create" step is dropped.

        RGPD: a duplicate is reported ONLY when the email is already a client of
        THIS agency. The cross-agency name fallback the creator uses is
        deliberately NOT consulted here — so the preview can never hint that an
        email exists at another agency, neither via a value nor via a status."""
        parsed_csv = self._parse_upload(request)

        template = await self.repo.get_agency_template(agent.agency_id, request.journey_template_id)
        if template is None:
            raise NotFoundError("Journey template not found.")
        declared = await self.repo.declared_fields(template.id)
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        defs_by_key = {d.key: d for d in definitions}

        mapping = await self._resolve_mapping(agent, request)
        targets = self._validate_mapping(mapping, parsed_csv.headers, declared, defs_by_key)
        required = [(d.family, d.reference) for d in declared if d.required]
        email_col = self._column_for(targets, "email")
        first_col = self._column_for(targets, "first_name")
        last_col = self._column_for(targets, "last_name")

        columns = [PreviewColumn(column=column, target=mapping[column]) for column in targets]
        rows: list[PreviewRow] = []
        seen_emails: set[str] = set()

        def _identity(col: str, val: str) -> PreviewCell:
            return PreviewCell(column=col, target=mapping[col], value=val or None)

        for index, raw_row in enumerate(parsed_csv.rows, start=1):
            email = (raw_row.get(email_col) or "").strip()
            first = (raw_row.get(first_col) or "").strip()
            last = (raw_row.get(last_col) or "").strip()
            cv = self._validate_cells(
                row=raw_row, index=index, targets=targets, defs_by_key=defs_by_key, mapping=mapping
            )
            # Cells in mapping order: identity columns carry the CSV value as-is,
            # the rest carry the coerced value / a per-cell reason.
            by_column = {
                email_col: _identity(email_col, email),
                first_col: _identity(first_col, first),
                last_col: _identity(last_col, last),
                **{cell.column: cell for cell in cv.cells},
            }
            ordered_cells = [by_column[column.column] for column in columns]

            status, reason = await self._preview_status(
                agent=agent,
                request=request,
                email=email,
                first=first,
                last=last,
                cv=cv,
                required=required,
                seen_emails=seen_emails,
            )
            rows.append(PreviewRow(row=index, status=status, reason=reason, cells=ordered_cells))

        return ImportPreview(
            total_rows=len(parsed_csv.rows),
            create_count=sum(1 for r in rows if r.status == "create"),
            create_with_errors_count=sum(1 for r in rows if r.status == "create_with_errors"),
            skipped_count=sum(1 for r in rows if r.status == "skipped"),
            rejected_count=sum(1 for r in rows if r.status == "rejected"),
            columns=columns,
            rows=rows,
        )

    async def _preview_status(
        self,
        *,
        agent: Agent,
        request: CaseImportRequest,
        email: str,
        first: str,
        last: str,
        cv: _CellValidation,
        required: list[tuple[str, str]],
        seen_emails: set[str],
    ) -> tuple[str, str | None]:
        """The predicted outcome of a row — mirrors _process_row's decision
        sequence MINUS the cross-agency name fallback (RGPD). On a predicted
        create, adds the email to seen_emails so a later same-email row previews
        as a within-file duplicate, exactly like the real import."""
        if not email:
            return "rejected", "missing_email"
        if email in seen_emails:
            return "skipped", "duplicate_in_file"
        if await self.cases_repo.email_is_agency_client(agent.agency_id, email):
            return "skipped", "duplicate_in_agency"
        if not first or not last:
            return "rejected", "missing_identity"
        missing_required = [
            f"{fam}:{ref}" for (fam, ref) in required if (fam, ref) not in cv.provided
        ]
        if missing_required:
            return "rejected", "missing_required_fields"
        kwargs: dict[str, object] = {
            "email": email,
            "first_name": first,
            "last_name": last,
            "journey_template_id": request.journey_template_id,
            **cv.field_values,
            "custom_fields": cv.custom,
        }
        try:
            CaseCreateRequest(**kwargs)
        except PydanticValidationError as exc:
            locs = {str(err["loc"][0]) for err in exc.errors() if err.get("loc")}
            return "rejected", ("invalid_email" if "email" in locs else "invalid_row")
        seen_emails.add(email)
        return ("create_with_errors" if cv.field_errors else "create"), None

    # --- resolution + pre-flight ----------------------------------------------------

    async def _resolve_mapping(self, agent: Agent, request: CaseImportRequest) -> dict[str, str]:
        """The effective mapping: inline (BLOC 2) OR a saved mapping resolved
        by id / by (parcours, crm) — always agency-scoped (a mapping of
        another agency is never readable)."""
        if request.mapping is not None:
            return request.mapping
        if request.mapping_id is not None:
            saved = await self.mappings.get(agent.agency_id, request.mapping_id)
            if saved is None:
                raise NotFoundError("Saved mapping not found.")
            if saved.journey_template_id != request.journey_template_id:
                raise ValidationError("Saved mapping belongs to a different parcours.")
            return dict(saved.mapping)
        if request.crm_slug is not None:
            saved = await self.mappings.get_first_for_crm(
                agent.agency_id, request.journey_template_id, request.crm_slug
            )
            if saved is None:
                raise NotFoundError("No saved mapping for this parcours and CRM.")
            return dict(saved.mapping)
        raise ValidationError("Provide a mapping, a mapping_id, or a crm_slug.")

    def _validate_mapping(
        self,
        mapping: dict[str, str],
        headers: list[str],
        declared: list[DeclaredField],
        defs_by_key: dict[str, CustomFieldDefinition],
    ) -> dict[str, MappingTarget]:
        # Shared with the save path: unparseable / undeclared / duplicate
        # targets are refused here (cible ∈ parcours).
        targets = validate_mapping_targets(mapping, declared, defs_by_key)
        # Import-only extras: the mapped CSV columns must exist, and identity
        # (email pivot + the NOT NULL names) must be mapped.
        errors: list[str] = []
        missing_columns = [column for column in mapping if column not in headers]
        if missing_columns:
            errors.append(f"columns absent from the CSV: {sorted(missing_columns)}")
        mapped_identity = {t.reference for t in targets.values() if t.family == "identity"}
        missing_identity = [name for name in IDENTITY_TARGETS if name not in mapped_identity]
        if missing_identity:
            errors.append(f"identity targets not mapped: {missing_identity}")
        if errors:
            raise ValidationError("Invalid mapping — " + "; ".join(errors) + ".")
        return targets

    @staticmethod
    def _column_for(targets: dict[str, MappingTarget], identity: str) -> str:
        for column, target in targets.items():
            if target.family == "identity" and target.reference == identity:
                return column
        raise ValidationError(f"identity target {identity!r} not mapped.")  # pragma: no cover

    @staticmethod
    def _cell_target(
        target: MappingTarget, defs_by_key: dict[str, CustomFieldDefinition]
    ) -> CellTarget:
        if target.family == "base_field":
            return BaseFieldTarget(target.reference)
        if target.family == "case_field":
            return CaseFieldTarget(target.reference)
        return CustomFieldTarget(defs_by_key[target.reference])

    @staticmethod
    def _render_value(value: object) -> str:
        """The coerced value as a display string for the preview."""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        if isinstance(value, date | datetime):
            return value.isoformat()
        return str(value)

    def _validate_cells(
        self,
        *,
        row: dict[str, str],
        index: int,
        targets: dict[str, MappingTarget],
        defs_by_key: dict[str, CustomFieldDefinition],
        mapping: dict[str, str],
    ) -> "_CellValidation":
        """Validate + coerce every NON-identity mapped cell of a row. The
        single shared validation path: the import (create) and the preview
        (dry-run) both consume it, so they can never drift. Pure (no DB, no
        write); an invalid cell is reported, never fatal."""
        field_values: dict[str, object] = {}
        custom: dict[str, object] = {}
        field_errors: list[ImportFieldError] = []
        provided: set[tuple[str, str]] = set()
        cells: list[PreviewCell] = []

        for column, target in targets.items():
            if target.family == "identity":
                continue
            result = validate_cell(
                column, self._cell_target(target, defs_by_key), row.get(column) or ""
            )
            if result.error is not None:
                field_errors.append(
                    ImportFieldError(
                        row=index,
                        column=column,
                        target=mapping[column],
                        reason=result.error.reason,
                    )
                )
                cells.append(
                    PreviewCell(
                        column=column,
                        target=mapping[column],
                        value=None,
                        reason=result.error.reason,
                    )
                )
                continue
            rendered = None if result.value is None else self._render_value(result.value)
            cells.append(PreviewCell(column=column, target=mapping[column], value=rendered))
            if result.value is None:
                continue  # empty cell, not provided
            provided.add((target.family, target.reference))
            if target.family == "custom_field":
                custom[target.reference] = result.value
            else:
                field_values[target.reference] = result.value

        return _CellValidation(
            field_values=field_values,
            custom=custom,
            field_errors=field_errors,
            provided=provided,
            cells=cells,
        )

    # --- per row -------------------------------------------------------------------

    async def _process_row(
        self,
        *,
        agent: Agent,
        request: CaseImportRequest,
        mapping: dict[str, str],
        row: dict[str, str],
        index: int,
        targets: dict[str, MappingTarget],
        required: list[tuple[str, str]],
        defs_by_key: dict[str, CustomFieldDefinition],
        email_col: str,
        first_col: str,
        last_col: str,
        seen_emails: set[str],
        report: ImportReport,
        pending: list[PendingEmail],
    ) -> None:
        email = (row.get(email_col) or "").strip()
        if not email:
            report.rejected.append(ImportRejected(row=index, reason="missing_email"))
            return
        if email in seen_emails:
            report.skipped.append(ImportSkipped(row=index, reason="duplicate_in_file"))
            return
        # RGPD-critical: scoped strictly to THIS agency. An email that exists
        # only at another agency is NOT a duplicate and is never reported.
        if await self.cases_repo.email_is_agency_client(agent.agency_id, email):
            report.skipped.append(ImportSkipped(row=index, reason="duplicate_in_agency"))
            return

        # Identity names: fall back to an existing expat's names (so a row
        # that only carries the email of an already-known person still
        # imports); a genuinely new person with no name cannot be created.
        existing = await self.cases_repo.get_expat_by_email(email)
        first_name = (row.get(first_col) or "").strip() or (existing.first_name if existing else "")
        last_name = (row.get(last_col) or "").strip() or (existing.last_name if existing else "")
        missing_identity = [
            label
            for label, value in (("first_name", first_name), ("last_name", last_name))
            if not value
        ]
        if missing_identity:
            report.rejected.append(
                ImportRejected(row=index, reason="missing_identity", details=missing_identity)
            )
            return

        kwargs: dict[str, object] = {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "journey_template_id": request.journey_template_id,
        }
        # Shared validation path (same as the dry-run preview).
        cv = self._validate_cells(
            row=row, index=index, targets=targets, defs_by_key=defs_by_key, mapping=mapping
        )
        kwargs.update(cv.field_values)
        custom = cv.custom
        field_errors = cv.field_errors

        missing_required = [
            f"{fam}:{ref}" for (fam, ref) in required if (fam, ref) not in cv.provided
        ]
        if missing_required:
            report.rejected.append(
                ImportRejected(
                    row=index, reason="missing_required_fields", details=sorted(missing_required)
                )
            )
            return

        kwargs["custom_fields"] = custom
        try:
            payload = CaseCreateRequest(**kwargs)
        except PydanticValidationError as exc:
            locs = {str(err["loc"][0]) for err in exc.errors() if err.get("loc")}
            reason = "invalid_email" if "email" in locs else "invalid_row"
            report.rejected.append(
                ImportRejected(row=index, reason=reason, details=[str(exc.error_count())])
            )
            return

        # ONE transaction per row: create_case commits on success; on any
        # failure we roll back THIS row only and keep going.
        try:
            case = await self.cases.create_case(agent, payload, email_sink=pending)
        except IntegrityError:
            # Race on uq_expat_user_email (concurrent import) → treat as a
            # duplicate, never a 500.
            await self.db.rollback()
            report.skipped.append(ImportSkipped(row=index, reason="duplicate_in_file"))
            return
        except NidriaError as exc:
            await self.db.rollback()
            report.rejected.append(
                ImportRejected(row=index, reason="invalid_row", details=[exc.message])
            )
            return

        seen_emails.add(email)
        report.created.append(
            ImportCreated(
                row=index,
                case_id=case.id,
                first_name=first_name,
                last_name=last_name,
                field_errors=field_errors,
            )
        )
