"""V4a (solde CRM) — le moteur d'import REPOINTÉ sur les FICHES.

Créer des fiches client depuis un mapping colonnes→champs person, SANS
parcours (l'assignation reste une étape séparée optionnelle — le wizard
dossiers existant la garde). Dédup email : LIER, pas dupliquer — la
fiche existante (liée ou non) est complétée fill-gap ; le rapport dit
créées / liées / ignorées, ligne par ligne."""

import base64
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_profile import ClientProfile
from src.client_profiles.backfill import CIVIL_COLUMNS
from src.client_profiles.client_profiles_manager import person_scope_definitions
from src.client_profiles.client_profiles_repository import ClientProfilesRepository
from src.core.exceptions import ValidationError
from src.imports.csv_reader import parse_upload

IDENTITY_TARGETS = ("first_name", "last_name", "email")


class ProfileImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv_text: str | None = None
    file_b64: str | None = None
    filename: str | None = None
    # {csv_column: cible} — cibles : first_name/last_name/email (identité)
    # + colonnes civiles + clés custom scope='person'.
    mapping: dict[str, str] = Field(min_length=1)


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


def _is_empty(value: Any) -> bool:
    return value in (None, "", [], {})


class ProfileImportManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ClientProfilesRepository(db)

    async def run_import(self, agent: Agent, body: ProfileImportRequest) -> ProfileImportReport:  # noqa: C901
        if body.csv_text is None and body.file_b64 is None:
            raise ValidationError("Provide csv_text or file_b64.", code="import.source_missing")
        content: bytes | str = (
            base64.b64decode(body.file_b64) if body.file_b64 else (body.csv_text or "")
        )
        parsed = parse_upload(body.filename, content)

        person_defs = await person_scope_definitions(self.db, agent.agency_id)
        person_keys = {d.key for d in person_defs}
        defs_by_key = {d.key: d for d in person_defs}
        valid_targets = set(IDENTITY_TARGETS) | set(CIVIL_COLUMNS) | person_keys
        bad_targets = sorted(set(body.mapping.values()) - valid_targets)
        if bad_targets:
            raise ValidationError(
                f"Unknown import targets: {', '.join(bad_targets)}.",
                code="import.unknown_targets",
                params={"targets": bad_targets},
            )
        targets_by_column = dict(body.mapping)
        if "email" not in targets_by_column.values():
            raise ValidationError(
                "The mapping must bind one column to 'email' (the dedup key).",
                code="import.email_target_required",
            )
        unknown_columns = sorted(set(targets_by_column) - set(parsed.headers))
        if unknown_columns:
            raise ValidationError(
                f"Mapped columns absent from the file: {', '.join(unknown_columns)}.",
                code="import.unknown_columns",
                params={"columns": unknown_columns},
            )

        created: list[ProfileImportRowOutcome] = []
        linked: list[ProfileImportRowOutcome] = []
        ignored: list[ProfileImportRowOutcome] = []
        for index, row in enumerate(parsed.rows, start=1):
            values: dict[str, str] = {}
            for column, target in targets_by_column.items():
                cell = (row.get(column) or "").strip()
                if cell:
                    values[target] = cell
            email = values.get("email")
            if not email:
                ignored.append(ProfileImportRowOutcome(row=index, reason="no_email"))
                continue
            existing_id = await self.repo.profile_id_for_email(agent.agency_id, email)
            if existing_id is not None:
                profile = await self.repo.get_for_agency(agent.agency_id, existing_id)
                assert profile is not None
                self._fill_gaps(profile, values, person_keys, defs_by_key)
                linked.append(
                    ProfileImportRowOutcome(row=index, email=email, profile_id=existing_id)
                )
                continue
            if not values.get("first_name") or not values.get("last_name"):
                ignored.append(
                    ProfileImportRowOutcome(row=index, email=email, reason="missing_identity")
                )
                continue
            profile = ClientProfile(
                agency_id=agent.agency_id,
                expat_user_id=None,
                first_name=values["first_name"],
                last_name=values["last_name"],
                email=email.lower(),
            )
            self._fill_gaps(profile, values, person_keys, defs_by_key)
            self.db.add(profile)
            await self.db.flush()
            created.append(ProfileImportRowOutcome(row=index, email=email, profile_id=profile.id))
        await self.db.commit()
        return ProfileImportReport(
            total_rows=len(parsed.rows), created=created, linked=linked, ignored=ignored
        )

    @staticmethod
    def _fill_gaps(
        profile: ClientProfile,
        values: dict[str, str],
        person_keys: set[str],
        defs_by_key: dict[str, Any],
    ) -> None:
        """LIER, pas dupliquer : les valeurs existantes de la fiche
        GAGNENT toujours — l'import ne comble que les trous. Les customs
        passent par la coercition TYPÉE du référentiel ; une déf address
        reçoit le texte intégral dans `street` (règle honnête, pas de
        parsing magique) ; une cellule illisible laisse le trou."""
        from pydantic import ValidationError as PydanticValidationError

        from src.cases.cases_schema import PersonUpdateRequest
        from src.core.enums import CustomFieldType
        from src.custom_fields.custom_fields_validation import _coerce_one

        for column in CIVIL_COLUMNS:
            raw = values.get(column)
            if raw is None or not _is_empty(getattr(profile, column, None)):
                continue
            # LA RÈGLE ABSOLUE (debug Teamleader 03/08) : chaque cellule
            # civile passe par la VALIDATION DU CONTRAT PERSON (longueurs,
            # enums, dates — le patron du fulfill expat). Échec = trou
            # laissé ; le batch ne meurt JAMAIS sur une donnée ('unknown'
            # dans sex VARCHAR(1) tuait les 1844 lignes en DataError).
            try:
                validated = PersonUpdateRequest.model_validate({column: raw})
            except PydanticValidationError:
                continue  # cellule illisible : trou laissé
            coerced = validated.model_dump(exclude_unset=True).get(column)
            setattr(profile, column, getattr(coerced, "value", coerced))
        sack = dict(profile.custom_fields or {})
        changed = False
        for key in person_keys:
            raw = values.get(key)
            if raw is None or not _is_empty(sack.get(key)):
                continue
            definition = defs_by_key[key]
            try:
                if definition.field_type == CustomFieldType.ADDRESS.value:
                    coerced = _coerce_one(definition, {"street": raw})
                else:
                    coerced = _coerce_one(definition, raw)
            except ValueError:
                continue  # cellule illisible : trou laissé
            sack[key] = coerced
            changed = True
        if changed:
            profile.custom_fields = sack


# --- IMPORT SOCIÉTÉS (complément B) ---------------------------------------------------


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


class CompanyImportManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def run_import(self, agent: Agent, body: CompanyImportRequest) -> CompanyImportReport:
        from shared.models.company_profile import CompanyProfile
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

        repo = CompanyProfilesRepository(self.db)
        created: list[CompanyImportRowOutcome] = []
        linked: list[CompanyImportRowOutcome] = []
        ignored: list[CompanyImportRowOutcome] = []
        for index, row in enumerate(parsed.rows, start=1):
            values: dict[str, str] = {}
            for column, target in body.mapping.items():
                cell = (row.get(column) or "").strip()
                if cell:
                    values[target] = cell
            name = values.get("name")
            if not name:
                ignored.append(CompanyImportRowOutcome(row=index, reason="no_name"))
                continue
            existing_id = await repo.id_for_name(agent.agency_id, name)
            if existing_id is not None:
                # LIER, pas dupliquer — la logique du 409 souple, en mode
                # import : fill-gap des presets, l'existant gagne.
                company = await repo.get_for_agency(agent.agency_id, existing_id)
                assert company is not None
                self._fill_gaps(company, values)
                linked.append(
                    CompanyImportRowOutcome(row=index, name=name, company_profile_id=existing_id)
                )
                continue
            company = CompanyProfile(agency_id=agent.agency_id, name=name)
            self._fill_gaps(company, values)
            self.db.add(company)
            await self.db.flush()
            created.append(
                CompanyImportRowOutcome(row=index, name=name, company_profile_id=company.id)
            )
        await self.db.commit()
        return CompanyImportReport(
            total_rows=len(parsed.rows), created=created, linked=linked, ignored=ignored
        )

    @staticmethod
    def _fill_gaps(company: Any, values: dict[str, str]) -> None:
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
