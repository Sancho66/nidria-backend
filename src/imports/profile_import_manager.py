"""Import FICHES (V4a + lot aperçu) — créer/lier des fiches depuis un
mapping colonnes→champs person, SANS parcours.

LA GARANTIE STRUCTURELLE (lot aperçu) : une SEULE fonction d'analyse
(`_analyze`) décide de chaque ligne — parse, corrections, validation
cellule par cellule (le contrat person), dédup base ET intra-batch.
Le preview la sert en dry-run (ZÉRO écriture) ; l'import réel ÉCRIT ce
qu'elle a décidé. Même fichier → verdicts IDENTIQUES, prouvé par test.

LA RÈGLE ABSOLUE (debug Teamleader 03/08) : une cellule mauvaise = trou
laissé (issue rapportée), une ligne mauvaise = ignorée avec raison, le
batch ne meurt JAMAIS sur une donnée utilisateur."""

import base64
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_profile import ClientProfile
from src.client_profiles.backfill import CIVIL_COLUMNS
from src.client_profiles.client_profiles_manager import person_scope_definitions
from src.client_profiles.client_profiles_repository import ClientProfilesRepository
from src.core.exceptions import ValidationError
from src.imports.csv_reader import parse_upload

IDENTITY_TARGETS = ("first_name", "last_name", "email")


def _is_empty(value: Any) -> bool:
    return value in (None, "", [], {})


class ImportCorrection(BaseModel):
    """Une correction front : appliquée APRÈS parse, AVANT validation —
    la valeur corrigée passe la MÊME moulinette que la cellule d'origine.
    Une correction invalide = issue rapportée / ligne ignorée motivée,
    JAMAIS un 500 (la règle absolue tient)."""

    model_config = ConfigDict(extra="forbid")

    row_index: int = Field(ge=1)
    target: str = Field(min_length=1, max_length=100)
    value: str = Field(max_length=1000)


class ProfileImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv_text: str | None = None
    file_b64: str | None = None
    filename: str | None = None
    # {csv_column: cible} — cibles : first_name/last_name/email (identité)
    # + colonnes civiles + clés custom scope='person'.
    mapping: dict[str, str] = Field(min_length=1)
    corrections: list[ImportCorrection] = Field(default_factory=list)


class ProfileImportPreviewRequest(ProfileImportRequest):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)


class RowIssue(BaseModel):
    column: str
    code: str


class RowVerdict(BaseModel):
    row_index: int
    status: Literal["create", "link", "ignore"]
    reason: str | None = None
    profile_id: uuid.UUID | None = None
    # Les valeurs NORMALISÉES par cible (post-coercition du contrat).
    person: dict[str, Any] = Field(default_factory=dict)
    issues: list[RowIssue] = Field(default_factory=list)


class ProfileImportRowOutcome(BaseModel):
    row: int
    email: str | None = None
    profile_id: uuid.UUID | None = None
    reason: str | None = None


class ProfileImportReport(BaseModel):
    total_rows: int
    created: list[ProfileImportRowOutcome]
    linked: list[ProfileImportRowOutcome]
    ignored: list[ProfileImportRowOutcome]


class ImportPreviewSummary(BaseModel):
    create: int
    link: int
    ignore: int
    ignore_reasons: dict[str, int]


class ProfileImportPreviewResponse(BaseModel):
    total_rows: int
    summary: ImportPreviewSummary
    rows: list[RowVerdict]
    page: int
    page_size: int


def _summarize(verdicts: list[RowVerdict]) -> ImportPreviewSummary:
    reasons: dict[str, int] = {}
    for v in verdicts:
        if v.status == "ignore" and v.reason:
            reasons[v.reason] = reasons.get(v.reason, 0) + 1
    return ImportPreviewSummary(
        create=sum(1 for v in verdicts if v.status == "create"),
        link=sum(1 for v in verdicts if v.status == "link"),
        ignore=sum(1 for v in verdicts if v.status == "ignore"),
        ignore_reasons=reasons,
    )


class ProfileImportManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ClientProfilesRepository(db)

    # --- LA fonction partagée (preview == import, structurel) -------------

    async def _analyze(self, agent: Agent, body: ProfileImportRequest) -> list[RowVerdict]:
        """Parse + corrections + validation + dédup — AUCUNE écriture."""
        if body.csv_text is None and body.file_b64 is None:
            raise ValidationError("Provide csv_text or file_b64.", code="import.source_missing")
        content: bytes | str = (
            base64.b64decode(body.file_b64) if body.file_b64 else (body.csv_text or "")
        )
        parsed = parse_upload(body.filename, content)

        person_defs = await person_scope_definitions(self.db, agent.agency_id)
        defs_by_key = {d.key: d for d in person_defs}
        valid_targets = set(IDENTITY_TARGETS) | set(CIVIL_COLUMNS) | set(defs_by_key)
        bad_targets = sorted(set(body.mapping.values()) - valid_targets)
        if bad_targets:
            raise ValidationError(
                f"Unknown import targets: {', '.join(bad_targets)}.",
                code="import.unknown_targets",
                params={"targets": bad_targets},
            )
        if "email" not in body.mapping.values():
            raise ValidationError(
                "The mapping must bind one column to 'email' (the dedup key).",
                code="import.email_target_required",
            )
        unknown_columns = sorted(set(body.mapping) - set(parsed.headers))
        if unknown_columns:
            raise ValidationError(
                f"Mapped columns absent from the file: {', '.join(unknown_columns)}.",
                code="import.unknown_columns",
                params={"columns": unknown_columns},
            )

        corrections_by_row: dict[int, list[ImportCorrection]] = {}
        for correction in body.corrections:
            corrections_by_row.setdefault(correction.row_index, []).append(correction)
        columns_by_target = {t: c for c, t in body.mapping.items()}

        from src.cases.cases_schema import PersonUpdateRequest
        from src.core.enums import CustomFieldType
        from src.custom_fields.custom_fields_validation import _coerce_one

        verdicts: list[RowVerdict] = []
        seen_emails: dict[str, int] = {}
        for index, row in enumerate(parsed.rows, start=1):
            issues: list[RowIssue] = []
            values: dict[str, str] = {}
            for column, target in body.mapping.items():
                cell = (row.get(column) or "").strip()
                if cell:
                    values[target] = cell
            # CORRECTIONS : après parse, avant validation — même moulinette.
            for correction in corrections_by_row.get(index, ()):
                if correction.target not in valid_targets:
                    issues.append(RowIssue(column="(correction)", code="unknown_target"))
                    continue
                corrected = correction.value.strip()
                if corrected:
                    values[correction.target] = corrected
                else:
                    values.pop(correction.target, None)

            email = (values.get("email") or "").lower() or None
            person: dict[str, Any] = {}
            for target in ("first_name", "last_name"):
                if values.get(target):
                    person[target] = values[target]
            if email:
                person["email"] = email
            for civil in CIVIL_COLUMNS:
                raw = values.get(civil)
                if raw is None:
                    continue
                try:
                    validated = PersonUpdateRequest.model_validate({civil: raw})
                except PydanticValidationError:
                    issues.append(
                        RowIssue(column=columns_by_target.get(civil, civil), code="invalid_value")
                    )
                    continue
                coerced = validated.model_dump(exclude_unset=True).get(civil)
                person[civil] = getattr(coerced, "value", coerced)
            for key, definition in defs_by_key.items():
                raw = values.get(key)
                if raw is None:
                    continue
                try:
                    if definition.field_type == CustomFieldType.ADDRESS.value:
                        person[key] = _coerce_one(definition, {"street": raw})
                    else:
                        person[key] = _coerce_one(definition, raw)
                except ValueError:
                    issues.append(
                        RowIssue(column=columns_by_target.get(key, key), code="invalid_value")
                    )

            if not email:
                verdicts.append(
                    RowVerdict(
                        row_index=index,
                        status="ignore",
                        reason="no_email",
                        person=person,
                        issues=issues,
                    )
                )
                continue
            existing_id = await self.repo.profile_id_for_email(agent.agency_id, email)
            if existing_id is not None:
                verdicts.append(
                    RowVerdict(
                        row_index=index,
                        status="link",
                        profile_id=existing_id,
                        person=person,
                        issues=issues,
                    )
                )
                continue
            if email in seen_emails:
                # Dédup INTRA-BATCH : la 1re occurrence crée, celle-ci LIE.
                verdicts.append(
                    RowVerdict(row_index=index, status="link", person=person, issues=issues)
                )
                continue
            if not values.get("first_name") or not values.get("last_name"):
                verdicts.append(
                    RowVerdict(
                        row_index=index,
                        status="ignore",
                        reason="missing_identity",
                        person=person,
                        issues=issues,
                    )
                )
                continue
            seen_emails[email] = index
            verdicts.append(
                RowVerdict(row_index=index, status="create", person=person, issues=issues)
            )
        return verdicts

    # --- dry-run (ZÉRO écriture) ------------------------------------------

    async def preview(
        self, agent: Agent, body: ProfileImportPreviewRequest
    ) -> ProfileImportPreviewResponse:
        verdicts = await self._analyze(agent, body)
        start = (body.page - 1) * body.page_size
        return ProfileImportPreviewResponse(
            total_rows=len(verdicts),
            summary=_summarize(verdicts),
            rows=verdicts[start : start + body.page_size],
            page=body.page,
            page_size=body.page_size,
        )

    # --- import réel : ÉCRIT ce que l'analyse a décidé --------------------

    async def run_import(self, agent: Agent, body: ProfileImportRequest) -> ProfileImportReport:
        verdicts = await self._analyze(agent, body)
        created: list[ProfileImportRowOutcome] = []
        linked: list[ProfileImportRowOutcome] = []
        ignored: list[ProfileImportRowOutcome] = []
        created_by_email: dict[str, uuid.UUID] = {}
        for verdict in verdicts:
            email = verdict.person.get("email")
            if verdict.status == "ignore":
                ignored.append(
                    ProfileImportRowOutcome(
                        row=verdict.row_index, email=email, reason=verdict.reason
                    )
                )
                continue
            if verdict.status == "create":
                profile = ClientProfile(
                    agency_id=agent.agency_id,
                    expat_user_id=None,
                    first_name=verdict.person["first_name"],
                    last_name=verdict.person["last_name"],
                    email=email,
                )
                self._apply_values(profile, verdict.person)
                self.db.add(profile)
                await self.db.flush()
                assert email is not None
                created_by_email[email] = profile.id
                created.append(
                    ProfileImportRowOutcome(
                        row=verdict.row_index, email=email, profile_id=profile.id
                    )
                )
                continue
            # link — en base, ou vers la fiche créée plus haut dans le batch.
            profile_id = verdict.profile_id or (created_by_email.get(email) if email else None)
            if profile_id is None:
                # La 1re occurrence de cet email a été ignorée (sans
                # identité) : celle-ci n'a rien à lier — même raison.
                ignored.append(
                    ProfileImportRowOutcome(
                        row=verdict.row_index, email=email, reason="missing_identity"
                    )
                )
                continue
            existing = await self.repo.get_for_agency(agent.agency_id, profile_id)
            assert existing is not None
            self._apply_values(existing, verdict.person, fill_gaps_only=True)
            linked.append(
                ProfileImportRowOutcome(row=verdict.row_index, email=email, profile_id=profile_id)
            )
        await self.db.commit()
        return ProfileImportReport(
            total_rows=len(verdicts), created=created, linked=linked, ignored=ignored
        )

    @staticmethod
    def _apply_values(
        profile: ClientProfile, person: dict[str, Any], *, fill_gaps_only: bool = False
    ) -> None:
        """Pose les valeurs NORMALISÉES par l'analyse. `fill_gaps_only`
        (liaison) : l'existant gagne toujours — l'import ne comble que
        les trous."""
        sack = dict(profile.custom_fields or {})
        changed = False
        for target, value in person.items():
            if target in IDENTITY_TARGETS:
                continue
            if target in CIVIL_COLUMNS:
                if fill_gaps_only and not _is_empty(getattr(profile, target, None)):
                    continue
                setattr(profile, target, value)
            else:
                if fill_gaps_only and not _is_empty(sack.get(target)):
                    continue
                sack[target] = value
                changed = True
        if changed:
            profile.custom_fields = sack


# --- IMPORT SOCIÉTÉS (complément B + lot aperçu) --------------------------------------


class CompanyImportRequest(BaseModel):
    """Import de fiches SOCIÉTÉ — endpoint séparé (même verdict que
    l'annuaire : cibles disjointes de l'import personnes). Cibles :
    `name` (dénomination, la clé de dédup, OBLIGATOIRE au mapping) + les
    8 presets company de la taxonomie posée. Les clés libres restent au
    PATCH (pas de référentiel société au MVP — écart nommé)."""

    model_config = ConfigDict(extra="forbid")

    csv_text: str | None = None
    file_b64: str | None = None
    filename: str | None = None
    mapping: dict[str, str] = Field(min_length=1)
    corrections: list[ImportCorrection] = Field(default_factory=list)


class CompanyImportPreviewRequest(CompanyImportRequest):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)


class CompanyImportRowOutcome(BaseModel):
    row: int
    name: str | None = None
    company_profile_id: uuid.UUID | None = None
    reason: str | None = None


class CompanyImportReport(BaseModel):
    total_rows: int
    created: list[CompanyImportRowOutcome]
    linked: list[CompanyImportRowOutcome]
    ignored: list[CompanyImportRowOutcome]


class CompanyImportPreviewResponse(BaseModel):
    total_rows: int
    summary: ImportPreviewSummary
    rows: list[RowVerdict]
    page: int
    page_size: int


class CompanyImportManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _analyze(self, agent: Agent, body: CompanyImportRequest) -> list[RowVerdict]:
        """La même garantie que les personnes : UNE analyse, zéro écriture."""
        from src.client_profiles.profile_sections import COMPANY_PRESET_PROFILE_SECTION
        from src.company_profiles.company_profiles_repository import CompanyProfilesRepository

        if body.csv_text is None and body.file_b64 is None:
            raise ValidationError("Provide csv_text or file_b64.", code="import.source_missing")
        content: bytes | str = (
            base64.b64decode(body.file_b64) if body.file_b64 else (body.csv_text or "")
        )
        parsed = parse_upload(body.filename, content)

        valid_targets = {"name"} | set(COMPANY_PRESET_PROFILE_SECTION)
        bad_targets = sorted(set(body.mapping.values()) - valid_targets)
        if bad_targets:
            raise ValidationError(
                f"Unknown company import targets: {', '.join(bad_targets)}.",
                code="import.unknown_targets",
                params={"targets": bad_targets},
            )
        if "name" not in body.mapping.values():
            raise ValidationError(
                "The mapping must bind one column to 'name' (the dedup key).",
                code="import.name_target_required",
            )
        unknown_columns = sorted(set(body.mapping) - set(parsed.headers))
        if unknown_columns:
            raise ValidationError(
                f"Mapped columns absent from the file: {', '.join(unknown_columns)}.",
                code="import.unknown_columns",
                params={"columns": unknown_columns},
            )

        corrections_by_row: dict[int, list[ImportCorrection]] = {}
        for correction in body.corrections:
            corrections_by_row.setdefault(correction.row_index, []).append(correction)

        repo = CompanyProfilesRepository(self.db)
        verdicts: list[RowVerdict] = []
        seen_names: dict[str, int] = {}
        for index, row in enumerate(parsed.rows, start=1):
            issues: list[RowIssue] = []
            values: dict[str, str] = {}
            for column, target in body.mapping.items():
                cell = (row.get(column) or "").strip()
                if cell:
                    values[target] = cell
            for correction in corrections_by_row.get(index, ()):
                if correction.target not in valid_targets:
                    issues.append(RowIssue(column="(correction)", code="unknown_target"))
                    continue
                corrected = correction.value.strip()
                if corrected:
                    values[correction.target] = corrected
                else:
                    values.pop(correction.target, None)
            name = values.get("name")
            person: dict[str, Any] = dict(values)
            if not name:
                verdicts.append(
                    RowVerdict(
                        row_index=index,
                        status="ignore",
                        reason="no_name",
                        person=person,
                        issues=issues,
                    )
                )
                continue
            key = name.strip().lower()
            existing_id = await repo.id_for_name(agent.agency_id, name)
            if existing_id is not None:
                verdicts.append(
                    RowVerdict(
                        row_index=index,
                        status="link",
                        profile_id=existing_id,
                        person=person,
                        issues=issues,
                    )
                )
                continue
            if key in seen_names:
                verdicts.append(
                    RowVerdict(row_index=index, status="link", person=person, issues=issues)
                )
                continue
            seen_names[key] = index
            verdicts.append(
                RowVerdict(row_index=index, status="create", person=person, issues=issues)
            )
        return verdicts

    async def preview(
        self, agent: Agent, body: CompanyImportPreviewRequest
    ) -> CompanyImportPreviewResponse:
        verdicts = await self._analyze(agent, body)
        start = (body.page - 1) * body.page_size
        return CompanyImportPreviewResponse(
            total_rows=len(verdicts),
            summary=_summarize(verdicts),
            rows=verdicts[start : start + body.page_size],
            page=body.page,
            page_size=body.page_size,
        )

    async def run_import(self, agent: Agent, body: CompanyImportRequest) -> CompanyImportReport:
        from shared.models.company_profile import CompanyProfile
        from src.company_profiles.company_profiles_repository import CompanyProfilesRepository

        verdicts = await self._analyze(agent, body)
        repo = CompanyProfilesRepository(self.db)
        created: list[CompanyImportRowOutcome] = []
        linked: list[CompanyImportRowOutcome] = []
        ignored: list[CompanyImportRowOutcome] = []
        created_by_name: dict[str, uuid.UUID] = {}
        for verdict in verdicts:
            name = verdict.person.get("name")
            key = name.strip().lower() if name else None
            if verdict.status == "ignore":
                ignored.append(
                    CompanyImportRowOutcome(row=verdict.row_index, name=name, reason=verdict.reason)
                )
                continue
            if verdict.status == "create":
                company = CompanyProfile(agency_id=agent.agency_id, name=name)
                self._fill_gaps(company, verdict.person)
                self.db.add(company)
                await self.db.flush()
                assert key is not None
                created_by_name[key] = company.id
                created.append(
                    CompanyImportRowOutcome(
                        row=verdict.row_index, name=name, company_profile_id=company.id
                    )
                )
                continue
            company_id = verdict.profile_id or (created_by_name.get(key) if key else None)
            if company_id is None:
                ignored.append(
                    CompanyImportRowOutcome(row=verdict.row_index, name=name, reason="no_name")
                )
                continue
            existing_company = await repo.get_for_agency(agent.agency_id, company_id)
            assert existing_company is not None
            self._fill_gaps(existing_company, verdict.person)
            linked.append(
                CompanyImportRowOutcome(
                    row=verdict.row_index, name=name, company_profile_id=company_id
                )
            )
        await self.db.commit()
        return CompanyImportReport(
            total_rows=len(verdicts), created=created, linked=linked, ignored=ignored
        )

    @staticmethod
    def _fill_gaps(company: Any, values: dict[str, Any]) -> None:
        from src.client_profiles.profile_sections import COMPANY_PRESET_PROFILE_SECTION

        sack = dict(company.custom_fields or {})
        changed = False
        for key in COMPANY_PRESET_PROFILE_SECTION:
            raw = values.get(key)
            if raw is not None and _is_empty(sack.get(key)):
                sack[key] = raw
                changed = True
        if changed:
            company.custom_fields = sack
