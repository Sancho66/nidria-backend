"""V4a (solde CRM) — le moteur d'import REPOINTÉ sur les FICHES.

Créer des fiches client depuis un mapping colonnes→champs person, SANS
parcours (l'assignation reste une étape séparée optionnelle — le wizard
dossiers existant la garde). Dédup email : LIER, pas dupliquer — la
fiche existante (liée ou non) est complétée fill-gap ; le rapport dit
créées / liées / ignorées, ligne par ligne."""

import base64
import uuid
from datetime import date
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

    async def run_import(self, agent: Agent, body: ProfileImportRequest) -> ProfileImportReport:
        if body.csv_text is None and body.file_b64 is None:
            raise ValidationError("Provide csv_text or file_b64.", code="import.source_missing")
        content: bytes | str = (
            base64.b64decode(body.file_b64) if body.file_b64 else (body.csv_text or "")
        )
        parsed = parse_upload(body.filename, content)

        person_defs = await person_scope_definitions(self.db, agent.agency_id)
        person_keys = {d.key for d in person_defs}
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
                self._fill_gaps(profile, values, person_keys)
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
            self._fill_gaps(profile, values, person_keys)
            self.db.add(profile)
            await self.db.flush()
            created.append(ProfileImportRowOutcome(row=index, email=email, profile_id=profile.id))
        await self.db.commit()
        return ProfileImportReport(
            total_rows=len(parsed.rows), created=created, linked=linked, ignored=ignored
        )

    @staticmethod
    def _fill_gaps(profile: ClientProfile, values: dict[str, str], person_keys: set[str]) -> None:
        """LIER, pas dupliquer : les valeurs existantes de la fiche
        GAGNENT toujours — l'import ne comble que les trous."""
        for column in CIVIL_COLUMNS:
            raw = values.get(column)
            if raw is None or not _is_empty(getattr(profile, column, None)):
                continue
            if column == "date_of_birth":
                try:
                    setattr(profile, column, date.fromisoformat(raw))
                except ValueError:
                    continue  # cellule illisible : trou laissé, pas d'échec de ligne
            else:
                setattr(profile, column, raw)
        sack = dict(profile.custom_fields or {})
        changed = False
        for key in person_keys:
            raw = values.get(key)
            if raw is not None and _is_empty(sack.get(key)):
                sack[key] = raw
                changed = True
        if changed:
            profile.custom_fields = sack
